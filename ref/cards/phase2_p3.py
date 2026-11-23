"""EUT phase-2 changes: the cards the EUT team's phase-2 scenarios need.

- Captain America, Super-Soldier: "~ enters with a shield counter on him." (the shield counter's
  replacement and prevention effects are rules, CR 122.1c: engine/triggers.py `_ShieldDestroy`,
  `_ShieldDamage`) and "As long as ~ has a shield counter on him, you and other Heroes you
  control have hexproof." (702.11b-c).
- Ral Zarek, Guest Lecturer and Tinybones Joins Up: "Any number of target players each discard a
  card." (101.4: the choices in APNAP order, then the discards at the same time; rulings
  2026-03-20 / 2024-04-12), "... each mill a card and lose 1 life.", "Flip five coins. Target
  opponent skips their next X turns, ..." (705, 614.10).
- Dancing from Dark to Dawn: "where X is that spell's mana value" (202.3, 202.3e).
- Hardened Academic: "Whenever one or more cards leave your graveyard" (603.2c: once per event;
  zones.move_batch tags each batch of simultaneous zone changes `move_batch`).

Registered through the hook registries like the other card modules; imported last.
"""
import re

from ..engine import effects as fx
from ..engine.effects import Filter, E
from ..engine.ir import Static, RulePart, ReplacementPart, TargetSpec, Seq, Trigger
from ..engine.decisions import Decision, Opt
from .. import Unsupported
from . import triggers as tg
from . import effects_text as et
from . import statics as st
from .nouns import parse_noun, num


# =============================================================================== shield counters

@st.static_parser
def _enters_with_shield_counter(l, cx):
    """"~ enters with a shield counter on him." (CR 614.1c, 122.6; the counter's effects: 122.1c)."""
    m = re.fullmatch(r"~ enters with (a|an|one|two|three) (shield) counters? on (?:it|him|her|them)\.", l)
    if not m:
        return None
    n, kind = num(m.group(1)), m.group(2)

    def apply(g, ev, src, n=n, kind=kind):
        cs = ev["opts"].setdefault("counters", {})
        cs[kind] = cs.get(kind, 0) + n
    p = ReplacementPart("enter_self", lambda g, ev, src: True, apply, text=l)
    p.commutes = True
    return [Static([p], text=l)]


@st.static_parser
def _you_and_others_have_hexproof(l, cx):
    """"You and other Heroes you control have hexproof." (CR 702.11c on the player, 702.11b on the
    permanents; Captain America's, under its "As long as" condition)."""
    m = re.fullmatch(r"You and (other .+? you control) have hexproof\.", l)
    if not m:
        return None
    inner = st.parse_static(m.group(1)[0].upper() + m.group(1)[1:] + " have hexproof.", cx)
    if not inner or any(a.kind != "static" or a.condition is not None for a in inner):
        raise Unsupported(f"hexproof grant {l!r}")
    parts = [RulePart("player_hexproof", "self")]
    for a in inner:
        parts += a.parts
    return [Static(parts, text=l)]


# =============================================================================== each target player

def _apnap(ps, g):
    """Players in APNAP order (CR 101.4): the active player first, then the others in turn order."""
    a = g.s.active
    return sorted(set(ps), key=lambda p: (p - a) % len(g.s.players))


def discard_together(g, chosen):
    """[(player, card)]: the cards are discarded at the same time (CR 101.4, 701.9a): one move
    batch, then a discard event per card (zones.discard's bookkeeping)."""
    chosen = [(p, o) for p, o in chosen if o in g.s.objs and g.s.objs[o].zone == "hand"]
    if not chosen:
        return []
    for p, o in chosen:
        fl = g.s.players[p].flags
        fl["discarded"] = tuple(fl.get("discarded", ())) + (g.s.objs[o].card,)
    res = g.move_batch([(o, "graveyard", {"cause": "discard"}) for _, o in chosen])
    for (p, o), nid in zip(chosen, res):
        ts = g.s.players[p].turn_stats
        ts["discards"] = ts.get("discards", 0) + 1
        g.emit({"type": "discard", "player": p, "obj": nid, "cause": "effect"})
    return res


class EachDiscardsOne(E):
    """"<players> each discard a card." Each player, in APNAP order, chooses a card in their hand
    (hidden, CR 101.4a); then all the chosen cards are discarded at the same time (101.4; Ral
    Zarek, Guest Lecturer ruling 2026-03-20, Tinybones Joins Up ruling 2024-04-12). A player
    with an empty hand discards nothing (701.9a)."""

    def __init__(self, who, text=""):
        self.who = who
        self.text = text

    def run(self, g, ctx):
        chosen = []
        for p in _apnap(fx.players_of(self.who, g, ctx), g):
            hand = list(g.s.players[p].hand)
            if not hand:
                continue
            opts = [Opt(f"choose:{g.ref(o)}", o, sig=("disc", g.obj_sig(o))) for o in hand]
            chosen.append((p, g.ask(Decision(p, "Discard", opts, prompt="Choose a card to discard."))))
        ctx.setdefault("vars", {})["discarded"] = [x for x in discard_together(g, chosen) if x]


class EachMillsAndLoses(E):
    """"<players> each mill N cards and lose L life." The players mill at the same time (CR 101.4,
    701.17a: one event), then each loses the life (608.2c)."""

    def __init__(self, who, n, life, text=""):
        self.who = who
        self.n = n
        self.life = life
        self.text = text

    def run(self, g, ctx):
        ps = _apnap(fx.players_of(self.who, g, ctx), g)
        moves = []
        for p in ps:
            lib = g.s.players[p].library
            k = min(self.n, len(lib))
            if k > 0:
                g.top_hook(p, k, "graveyard")
                moves += [(o, "graveyard", {"cause": "mill"}) for o in lib[:k]]
        res = g.move_batch(moves) if moves else []
        ctx.setdefault("vars", {})["milled"] = [x for x in res if x]
        for p in ps:
            g.lose_life(p, self.life, source=ctx.get("source"))


def _each_discard(m, sc):
    who = sc.tc.add(TargetSpec("player", n=(0, 2), text="any number of target players"))
    return EachDiscardsOne(who, text=m.group(0))


def _each_mill_lose(m, sc):
    who = sc.tc.add(TargetSpec("player", n=(0, 2), text="any number of target players"))
    return EachMillsAndLoses(who, num(m.group(1)), int(m.group(2)), text=m.group(0))


# =============================================================================== coins, skipped turns

class FlipCoins(E):
    """"Flip N coins." (CR 705.1-2: each flip is heads or tails at random); the number of heads is
    kept for "the number of coins that came up heads"."""

    def __init__(self, n, text=""):
        self.n = n
        self.text = text

    def run(self, g, ctx):
        heads = sum(1 for _ in range(self.n) if g.s.rng.random() < 0.5)
        g.emit({"type": "coins_flipped", "player": ctx["controller"], "n": self.n, "heads": heads})
        ctx.setdefault("vars", {})["heads"] = heads


class SkipTurns(E):
    """"Target opponent skips their next X turns." (CR 614.10: a replacement effect on that
    player's next X turns; 614.10a: anything scheduled for them waits). Kept as ("skip", p) entries in
    `extra_turns` (turn.next_turn), so effects whose durations count turns refuse loudly while
    any are pending (effects.py, round2_*: Unsupported with extra turns pending)."""

    def __init__(self, who, n, text=""):
        self.who = who
        self.n = n
        self.text = text

    def run(self, g, ctx):
        k = self.n(g, ctx)
        for p in fx.players_of(self.who, g, ctx):
            g.s.extra_turns = list(g.s.extra_turns) + [("skip", p)] * max(0, k)
        g.s.touch()


def _flip_coins(m, sc):
    return FlipCoins(num(m.group(1)), text=m.group(0))


def _skip_turns(m, sc):
    who = sc.tc.add(TargetSpec("opponent", text="target opponent"))
    heads = fx.Amount(lambda g, ctx: (ctx.get("vars") or {}).get("heads", 0), "heads")
    return SkipTurns(who, heads, text=m.group(0))


# =============================================================================== that spell's mana value

def _spell_mv(g, ctx):
    """The triggering spell's mana value (CR 202.3; {X} as chosen while it's on the stack, 202.3e;
    the face it was cast as, 202.3d / 715.3b / 720.3; as it last existed if it left the stack,
    608.2h). The same reading as Thor's (round2_b2 `_dmg_spell_mv`)."""
    from ..engine.mana import mana_value
    ev = ctx.get("event") or {}
    oid = ev.get("obj")
    o = g.s.objs.get(oid)
    if o is not None and o.zone == "stack":
        return mana_value(g.chars(oid).mana_cost, o.x or 0)
    c = ev.get("chars") or g.s.lki.get(oid)
    lo = g.s.lki_obj.get(oid)
    if c is None:
        return 0
    return mana_value(c.mana_cost, (lo.x if lo is not None else 0) or 0)


def _amount_hook(t, sc):
    if t == "that spell's mana value":
        return fx.Amount(_spell_mv, t)
    return None


et.AMOUNT_HOOKS.append(_amount_hook)


# =============================================================================== leave your graveyard

@tg._nc
class CardsLeaveYourGraveyard(Trigger):
    """"Whenever one or more [artifact and/or creature] cards leave your graveyard [during your
    turn]": once per event (CR 603.2c), however many cards left at the same time (one
    zones.move_batch). The cards as they last existed in the graveyard (603.10: not a look-back
    trigger; the source is on the battlefield)."""
    events = ("zone",)
    lookback = False

    def __init__(self, filt=None, your_turn=False):
        self.filt = filt
        self.your_turn = your_turn
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev.get("from") != "graveyard" or ev.get("owner") != ctrl:
            return None
        if self.your_turn and g.s.active != ctrl:
            return None
        if self.filt is not None:
            lki = ev.get("lki")
            if lki is None or not self.filt(lki):
                return None
        batch = ev.get("move_batch")
        for t in g.s.pending:
            if t.source == src and getattr(t.ability, "trigger", None) is self and t.event is not ev \
                    and isinstance(t.event, dict) and batch is not None and t.event.get("move_batch") == batch:
                return None  # this event already triggered it
        return {"obj": ev.get("new"), "old": ev.get("old"), "player": ctrl}


def _trigger_hook(line, cx):
    m = re.match(r"^Whenever one or more (cards|artifact and/or creature cards) leave your graveyard( during your turn)?, (.+)$", line)
    if m:
        f = None
        if m.group(1) != "cards":
            f = lambda c: bool({"Artifact", "Creature"} & set(c.types))  # noqa: E731
        return CardsLeaveYourGraveyard(f, bool(m.group(2))), m.group(3)
    return None


tg.TRIGGER_HOOKS.append(_trigger_hook)


et.PATTERNS[0:0] = [
    (r"Any number of target players each discard a card\.", _each_discard),
    (r"Any number of target players each mill (a|one|two|three) cards? and lose (\d+) life\.", _each_mill_lose),
    (r"Flip (two|three|four|five) coins\.", _flip_coins),
    (r"Target opponent skips their next X turns, where X is the number of coins that came up heads\.", _skip_turns),
]
