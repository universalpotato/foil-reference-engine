"""Card-support round 2: conditions, life, counters and triggers.

Cards: The Ooze, Case of the Uneaten Feast, Amalia
Benavides Aguirre, Lunar Convocation, Aunt May, Essence Channeler, Ingris Stingerquill, Master
of Barbs, Gev, Scaled Scorch, Valley Flamecaller, Fireglass Mentor, Stadium Headliner, Enduring
Innocence, Scheming Silvertongue, Inevitable Defeat, Plasma Bolt, Ghitu Lavarunner, Hydro-Man,
Tragic Trajectory, Beza, Mightform Harmonizer, Mossborn Hydra, Meltstrider's Resolve,
Hard-Hitting Question, Bristly Bill, Dyadrine, Bloodthirsty Conqueror, Mephitic Draught, Callous
Sell-Sword, Full Bore, Ancestral Anger, Emberheart Challenger, Dispelling Exhale, Kutzil's Flanker,
Boomerang Basics, Krang, Gumdrop Poisoner (and Chandra, Chill of Compliance).

Mechanics, each from its rule:
- turn history: "you've gained N or more life this turn", "you gained and lost life this turn",
  "you've lost life this turn", "the amount of life you gained this turn" (CR 119.3; the
  rulings: whether life was gained/lost, not how the total changed), "an opponent lost life this
  turn" per opponent (Gev), creatures that died / left the battlefield under your control this
  turn, a nonland permanent left the battlefield this turn and a spell was warped this turn
  (void, an ability word, 207.2c; 702.185c) (zones.after_move / altcast.after_cast keep the
  counts);
- "At the beginning of your second main phase" (505.1, 505.1b: the engine has no additional
  phases, so it's the Main2 step);
- triggers: "whenever a <creature> leaves the battlefield" (603.6c, 603.10a), "whenever an
  opponent loses life" (119.3, 120.3a: one trigger per life-loss event, "that much"), "whenever
  one or more opponents are dealt noncombat damage" (603.2c: once per simultaneous damage event;
  zones.deal_damage tags the event's "batch"), "one or more ... enter" with "This ability
  triggers only once each turn" (603.2c, 603.2i-style limit: the first such event), "when ~
  enters or is put into a graveyard from the battlefield", valiant "becomes the target of a
  spell or ability you control for the first time each turn" (603.2e; zones.emit records each
  targeting);
- replacement effects on entering (614.1c, 614.12): "other creatures you control enter with an
  additional +1/+1 counter for each opponent who lost life this turn" (Gev; one that enters at
  the same time as Gev gets none, ruling 2024-07-26), "~ enters with a +1/+1 counter for each
  creature that died under your control this turn", "~ enters with ... counters equal to the
  amount of mana spent to cast it" (601.2h; Dyadrine rulings 2025-07-25);
- damage modification "it deals that much damage plus 1 instead" for a source by creature
  type (614.1a, 109.2: a permanent you control);
- doubling a creature's power and the number of counters (701.10b-c, 701.10e);
- fight with "enchanted creature" and "up to one target" (701.14a-b), "deals damage equal to its
  power to" (608.2h; the first target is the source), "can't be blocked by more than one
  creature" (509.1b);
- self-replacement "instead" of void (614.15: the amount is chosen as the spell resolves);
- "Solved — <activated ability>" (702.169b) and a granted "You may cast this card from your
  graveyard" until end of turn (611.2c: the creature cards in the graveyard at resolution;
  a graveyard play-permission record, effects.play_permission_valid);
- "becomes a land" until your next turn (205.1a, 613.1d, 611.2a; a land is never summoning sick,
  302.6);
- comparisons with an opponent (Beza ruling 2024-07-26: each checked as its part resolves).

Registered into the hook registries (compiler.py above KEYWORD_HOOKS, triggers.py, statics.py
EXTRA_STATICS / STATIC_CONDITION_HOOKS, effects_text.py PATTERNS / MERGE_PAIRS). Strict like the
rest of the compiler: text these patterns don't match exactly still raises Unsupported.
"""
import re

from ..engine import effects as fx
from ..engine.effects import Filter, E, Sel
from ..engine.ir import (Triggered, Static, LayerPart, RulePart, ReplacementPart, TargetSpec, Seq,
                         Activated, Cost, Trigger, ManaProduce, SpellAbility, Noop)
from ..engine.decisions import Decision, Opt
from .. import Unsupported
from . import compiler as C
from . import triggers as tg
from . import effects_text as et
from . import statics as st
from .nouns import parse_noun, num
from .model import token_def


def _nc(cls):
    cls.__deepcopy__ = lambda self, memo: self
    cls.__copy__ = lambda self: self
    return cls


def _src_obj(g, ctx):
    src = ctx.get("source")
    return g.s.objs.get(src) or g.s.lki_obj.get(src)


def _opp(p):
    return 1 - p  # two-player games (the engine's only mode)


def _on_bf(g, oid):
    return isinstance(oid, int) and oid in g.s.objs and g.s.objs[oid].zone == "battlefield"


# =============================================================================== turn history

def _gained(g, p):
    return g.s.players[p].life_gained


def _lost(g, p):
    return g.s.players[p].life_lost


def _creatures_left(g, p):
    return g.s.turn_flags.get(f"creatures_left_p{p}", 0)


def _creatures_died(g, p):
    return g.s.turn_flags.get(f"creatures_died_p{p}", 0)


def _void(g):
    """Void (an ability word, CR 207.2c): "if a nonland permanent left the battlefield this turn or
    a spell was warped this turn" (702.185c)."""
    tf = g.s.turn_flags
    return bool(tf.get("nonland_left")) or bool(tf.get("spell_warped"))


def _condition_hook(t, cx):
    """Conditions of triggered abilities and effects (CR 603.4, 608.2c): predicate(g, ctx)."""
    if t == "you gained and lost life this turn":
        # Lunar Convocation rulings 2024-07-26: whether life was gained and lost, not the total.
        return lambda g, ctx: _gained(g, ctx["controller"]) > 0 and _lost(g, ctx["controller"]) > 0
    if t in ("you've lost life this turn", "you lost life this turn"):
        return lambda g, ctx: _lost(g, ctx["controller"]) > 0
    m = re.fullmatch(r"you have fewer than (\w+) cards in hand", t)
    if m:
        k = num(m.group(1))
        return lambda g, ctx, k=k: len(g.s.players[ctx["controller"]].hand) < k
    if t in ("~ is a creature",):
        # The source as it is now (CR 603.4: checked as it triggers and as it resolves).
        return lambda g, ctx: _on_bf(g, ctx.get("source")) and "Creature" in g.chars(ctx["source"]).types
    m = re.fullmatch(r"it's an? ([A-Z][a-z]+)", t)
    if m and isinstance(getattr(cx, "trigger", None), tg.Enters):
        # "If it's a Spider" (Aunt May): the entering creature, as it is as the ability resolves.
        sub = m.group(1)

        def cond(g, ctx, sub=sub):
            for o in fx.event_obj("new").get(g, ctx):
                if _on_bf(g, o):
                    return sub in g.chars(o).subtypes
            return False
        return cond
    if t == "a Dragon was beheld":
        # CR 701.4b: the optional behold cost was paid, whatever the beheld object is now.
        return lambda g, ctx: "behold" in (ctx.get("paid_additional") or ())
    return None


def _static_condition_hook(t, cx):
    """"As long as <condition>" (CR 611.3a): predicate(g, src)."""
    if t == "you've lost life this turn":
        # Essence Channeler rulings 2024-07-26: whether you lost life, not how the total changed.
        return lambda g, src: _lost(g, g.ctrl(src)) > 0
    m = re.fullmatch(r"there are (\w+) or more instant and/or sorcery cards in your graveyard", t)
    if m:
        n = num(m.group(1))
        # Cards in a graveyard read without the layer system (as effects.card_types_among: no
        # recursion while characteristics are being computed).
        return lambda g, src, n=n: sum(1 for o in g.s.players[g.ctrl(src)].graveyard
                                       if {"Instant", "Sorcery"} & g.base_chars(g.s.objs[o]).types) >= n
    return None


# =============================================================================== triggers

@_nc
class LeavesBattlefield(tg.Dies):
    """"Whenever a creature you control with a +1/+1 counter on it leaves the battlefield" (CR
    603.6c, 603.10a: looks back; the creature as it last existed, counters included: The Ooze
    ruling 2026-01-27)."""


@_nc
class OpponentLosesLife(Trigger):
    """"Whenever an opponent loses life" (CR 119.3, 120.3a: damage causes loss of life; paying
    life is losing life, 119.4). One trigger per life-loss event; "that much" is its amount."""
    events = ("lifeloss",)

    def __init__(self):
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev.get("player") == ctrl or ev.get("n", 0) <= 0:
            return None
        return {"player": ev["player"], "n": ev["n"]}


@_nc
class OpponentsDealtNoncombat(Trigger):
    """"Whenever one or more opponents are dealt noncombat damage" (CR 603.2c: once for each
    simultaneous damage event; the event id is zones.deal_damage's "batch")."""
    events = ("damage",)

    def __init__(self):
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        t = ev.get("target")
        if ev.get("combat") or not (isinstance(t, tuple) and t[0] == "player" and t[1] != ctrl):
            return None
        k = ("dealt_noncombat_seen", src, ev.get("batch"))
        if g.s.turn_flags.get(k):
            return None
        g.s.turn_flags[k] = True
        return {"player": t[1]}


@_nc
class EntersOrLeaves(Trigger):
    """"When ~ enters or is put into a graveyard from the battlefield" (Mephitic Draught, to=
    "graveyard") and "When ~ enters or leaves the battlefield" (Mouser Foundry, Cryogen Relic,
    round2_c2; to=None): one ability with two trigger events. The leaving part looks back in time
    (CR 603.10a), the entering part looks at the battlefield after the event (603.6a); any type of
    permanent. (Round 2 merge: a2's EntersOrToGraveyard and c2's EntersOrLeaves, one class.)"""
    events = ("zone",)
    lookback = False

    def __init__(self, to="graveyard"):
        self.zones = ("battlefield",)
        self.to = to
        self._enters = tg.Enters(self_=True)
        self._leaves = tg.Dies(self_=True, to=to)
        self._leaves.any_type = True

    def lookback_for(self, ev):
        return ev.get("from") == "battlefield" and ev.get("to") != "battlefield"

    def match(self, g, ev, src, c, ctrl):
        if self.lookback_for(ev):
            return self._leaves.match(g, ev, src, c, ctrl)
        return self._enters.match(g, ev, src, c, ctrl)


@_nc
class Valiant(Trigger):
    """Valiant: "Whenever ~ becomes the target of a spell or ability you control for the first time
    each turn" (CR 603.2e; rulings 2024-07-26: a changed target or a copy's target counts, if the
    creature hasn't been the target of a spell or ability you control yet this turn). Whether it
    has is recorded by zones.emit after each "targeted" event."""
    events = ("targeted",)

    def __init__(self):
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev.get("target") != src or ev.get("controller") != ctrl:
            return None
        if (src, ctrl) in (g.s.turn_flags.get("targeted_by") or {}):
            return None
        return {"obj": src, "item": ev.get("item"), "player": ctrl}


@_nc
class UnlockThisDoor(Trigger):
    """"When you unlock this door" (CR 709.5h): this half of the Room gets its unlocked designation,
    by the unlock special action (altcast.do_special, event "unlocked") or as the Room enters
    because that half was cast (709.5d; zones._do_move sets flags['unlocked'])."""
    events = ("unlocked", "zone")

    def __init__(self, half):
        self.half = half
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev["type"] == "unlocked":
            if ev.get("obj") == src and ev.get("half") == self.half:
                return {"obj": src, "player": ev.get("player")}
            return None
        if ev.get("to") == "battlefield" and ev.get("new") == src and ev.get("from") == "stack":
            o = g.s.objs.get(src)
            if o is not None and self.half in (o.flags.get("unlocked") or ()):
                return {"obj": src, "player": ctrl}
        return None


def _trigger_hook(line, cx):
    m = re.match(r"^When you unlock this door, (.+)$", line)
    if m:
        if cx.face.card.layout != "split" or "Room" not in cx.face.subtypes:
            raise Unsupported("'unlock this door' outside a Room")
        return UnlockThisDoor(cx.face.index), m.group(1)
    m = re.match(r"^Whenever (a|an) (.+?) leaves the battlefield, (.+)$", line)
    if m:
        return LeavesBattlefield(filt=parse_noun(m.group(2)), to=None), m.group(3)
    m = re.match(r"^Whenever an opponent loses life, (.+)$", line)
    if m:
        return OpponentLosesLife(), m.group(1)
    m = re.match(r"^Whenever one or more opponents are dealt noncombat damage, (.+)$", line)
    if m:
        return OpponentsDealtNoncombat(), m.group(1)
    m = re.match(r"^Whenever one or more (other )?(.+?) enter, (.+ This ability triggers only once each turn\.)$", line)
    if m:
        # "One or more ... enter" (one trigger per event, 603.2c) limited to once each turn: the
        # first event with such a creature triggers it, as the first such creature does (the
        # power is checked as it enters: Enduring Innocence ruling 2024-09-20).
        f = parse_noun(m.group(2))
        if m.group(1):
            f.other = True
        return tg.Enters(filt=f), m.group(3)
    m = re.match(r"^At the beginning of your second main phase, (.+)$", line)
    if m:
        # CR 505.1, 505.1b: the second main phase of the turn; without additional phases (the
        # engine has none) it is the postcombat main phase.
        return tg.Step("Main2", "you"), m.group(1)
    m = re.match(r"^When ~ enters or is put into a graveyard from the battlefield, (.+)$", line)
    if m:
        return EntersOrLeaves(to="graveyard"), m.group(1)
    m = re.match(r"^Whenever ~ becomes the target of a spell or ability you control for the first time each turn, (.+)$", line)
    if m:
        return Valiant(), m.group(1)
    return None


# =============================================================================== effects

class TokensPerCounter(E):
    """"Create a Mutagen token for each +1/+1 counter on it." (The Ooze): the counters on the
    creature as it last existed (CR 603.10a, 608.2h)."""

    def __init__(self, token_fn, kind, text=""):
        self.token_fn = token_fn
        self.kind = kind
        self.text = text

    def run(self, g, ctx):
        _, o = fx.event_obj_chars(g, ctx)
        n = (o.counters or {}).get(self.kind, 0) if o is not None else 0
        if n > 0:
            g.create_token(self.token_fn(), ctx["controller"], n)


def _tokens_per_counter(m, sc):
    if not isinstance(sc.trigger, tg.Dies):
        raise Unsupported(f"counters on 'it' without a leaving object: {m.group(0)!r}")
    return TokensPerCounter(et.parse_token(m.group(1)), m.group(2), text=m.group(0))


class GraveyardCastPermission(E):
    """"Creature cards in your graveyard gain "You may cast this card from your graveyard" until end
    of turn." (Case of the Uneaten Feast): the cards there as it resolves (CR 611.2c); each keeps
    the permission while it stays in the graveyard (400.7), until the turn ends (514.2). Written
    as the one play-permission record (effects.play_permission_valid: zone "graveyard", spells
    only; round 2 merge, it was a flag of its own), so casting.castable_candidates offers it like
    the other permissions. A card that already may be played from there this turn keeps that
    record (it covers casting too)."""

    def __init__(self, filt, text=""):
        self.filt = filt
        self.text = text

    def run(self, g, ctx):
        from ..engine.effects import play_permission_valid
        p = ctx["controller"]
        for o in list(g.s.players[p].graveyard):
            if self.filt(g, o) and not play_permission_valid(g, g.s.objs[o], p):
                g.s.objs[o].flags["may_play"] = {"player": p, "until": (g.s.turn, None), "any_type": False,
                                                 "lands": False, "zone": "graveyard"}
        g.s.touch()


def _gy_cast(m, sc):
    noun = m.group(1)
    if noun != "Creature":
        raise Unsupported(f"graveyard cast permission for {noun!r} cards")
    return GraveyardCastPermission(lambda g, o: "Creature" in g.chars(o).types, text=m.group(0))


def _amalia(m, sc):
    """"Then destroy all other creatures if its power is exactly 20." Its power as the ability
    resolves, after exploring; if it left the battlefield, as it last existed (rulings
    2023-11-10)."""
    n = int(m.group(1))

    def cond(g, ctx, n=n):
        c = fx.self_chars(g, ctx)
        return c is not None and c.power == n
    f = Filter(types=["Creature"], other=True, text="other creatures")
    return fx.If(cond, fx.Destroy(fx.all_matching(f)), text=m.group(0))


def _attacker_source():
    """The attacking creature of an "attacks" trigger, as a damage source: the creature, or its last
    known information if it left the battlefield (CR 608.2h)."""
    def fn(g, ctx):
        x = ((ctx.get("event") or {}).get("obj"))
        if x is None:
            return []
        return [x] if (x in g.s.objs or x in g.s.lki) else []
    return Sel(fn, "attacker")


def _attacker_deals(m, sc):
    if not isinstance(sc.trigger, tg.Attacks) or sc.last is not None:
        raise Unsupported(f"'that creature deals' without an attacker: {m.group(0)!r}")
    return fx.Damage(int(m.group(1)), fx.opponent(), source=_attacker_source(), text=m.group(0))


def _named_token(m, sc):
    """"Create a 2/2 colorless Wizard Soldier creature token named Cadet." (CR 111.4: the named
    token's name)."""
    p, t = int(m.group(1)), int(m.group(2))
    if m.group(3) != "colorless":
        raise Unsupported(f"named token colors {m.group(3)!r}")
    subs = tuple(m.group(4).split())
    name = m.group(5)
    sc.last = fx.var("token")
    return fx.CreateToken(lambda: token_def(name, ("Creature",), subs, (), p, t, "", named=name), 1,
                          text=m.group(0), remember="token")


class ExileTopTwoChooseOne(E):
    """"Exile the top two cards of your library. Choose one of them. Until end of turn, you may play
    that card." (Fireglass Mentor; rulings 2024-07-26: normal costs and timing). Only the chosen
    card may be played (601.3, 305.1)."""

    def __init__(self, n, text=""):
        self.n = n
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        pl = g.s.players[p]
        n = min(self.n, len(pl.library))
        if n <= 0:
            return
        if g.s.extra_turns:
            raise Unsupported("play-permission durations with extra turns pending")
        g.top_hook(p, n, "exile")
        top = pl.library[:n]
        res = g.move_batch([(o, "exile", {"cause": "effect"}) for o in top])
        kept = [r for r in res if r is not None and g.s.objs[r].zone == "exile"]
        if not kept:
            return
        if len(kept) == 1:
            pick = kept[0]
        else:
            opts = [Opt(f"choose:{g.ref(o)}", o, sig=("choose", g.obj_sig(o))) for o in kept]
            pick = g.ask(Decision(p, "ChooseCard", opts, prompt="Choose one of them: you may play it until end of turn."))
        g.s.objs[pick].flags["may_play"] = {"player": p, "until": (g.s.turn, None), "any_type": False, "lands": True}
        g.s.touch()


def _damage_equal_count(m, sc):
    """"It deals damage equal to the number of creatures you control to target creature." (Stadium
    Headliner, sacrificed as the cost: "it" is the source, as it last existed, CR 608.2h; the
    creatures are counted as the ability resolves, ruling 2025-04-04)."""
    if sc.last is not None or sc.trigger is not None:
        raise Unsupported(f"'it deals' with another antecedent: {m.group(0)!r}")
    n = et.parse_amount_expr(m.group(1), sc)
    to = et.target_noun(m.group(2), sc)
    return fx.Damage(n, to, text=m.group(0))


def _its_controller_drain(m, sc):
    """"Exile target nonland permanent. Its controller loses 3 life and you gain 3 life." Its
    controller as it last existed on the battlefield (CR 608.2h)."""
    if sc.last is None:
        raise Unsupported("'its controller' without antecedent")
    who = fx.controller_of(sc.last)
    return Seq(fx.LoseLife(int(m.group(1)), who), fx.GainLife(int(m.group(2))))


@_nc
class Instead(E):
    """Void's "... instead if <condition>" (CR 614.15, a self-replacement effect): folded into the
    preceding sentence of the spell by _fold_instead; never run by itself."""

    def __init__(self, kind, value, cond, text=""):
        self.kind = kind
        self.value = value
        self.cond = cond
        self.text = text

    def run(self, g, ctx):
        raise Unsupported(f"unfolded 'instead': {self.text!r}")


def _void_cond(t):
    if t == "a nonland permanent left the battlefield this turn or a spell was warped this turn":
        return lambda g, ctx: _void(g)
    raise Unsupported(f"void condition {t!r}")


def _dmg_instead(m, sc):
    return Instead("damage", int(m.group(1)), _void_cond(m.group(2)), text=m.group(0))


def _pump_instead(m, sc):
    return Instead("pump", (int(m.group(1)), int(m.group(2))), _void_cond(m.group(3)), text=m.group(0))


def _fold_instead(abilities, cx):
    """Join "<effect>. / Void — <effect> instead if <condition>." into one effect (CR 614.15)."""
    out = []
    for a in abilities:
        eff = getattr(a, "effect", None)
        if isinstance(a, SpellAbility) and isinstance(eff, Seq) and any(isinstance(e, Instead) for e in eff.effects):
            effs = list(eff.effects)
            new = []
            for e in effs:
                if not isinstance(e, Instead):
                    new.append(e)
                    continue
                prev = new.pop() if new else None
                if e.kind == "damage" and isinstance(prev, fx.Damage):
                    alt = fx.Damage(e.value, prev.to, source=prev.source, text=e.text)
                elif e.kind == "pump" and isinstance(prev, fx.Pump) and prev.p is not None and not prev.keywords:
                    alt = fx.Pump(prev.sel, e.value[0], e.value[1], duration=prev.duration, text=e.text)
                else:
                    raise Unsupported(f"'instead' after {prev!r}: {e.text!r}")
                new.append(fx.If(e.cond, alt, prev, text=e.text))
            a = SpellAbility(Seq(*new), a.targets, modes=a.modes, text=a.text)
        elif isinstance(eff, Instead) or (isinstance(eff, Seq) and any(isinstance(e, Instead) for e in eff.effects)):
            raise Unsupported("'instead' outside a spell")
        out.append(a)
    return out


def _he_pumps(m, sc):
    if "Legendary" not in sc.cx.face.supertypes:
        raise Unsupported(f"pronoun {m.group(1)!r} for a nonlegendary object")
    sc.last = fx.self_permanent()
    return fx.Pump(fx.self_permanent(), int(m.group(2)), int(m.group(3)), text=m.group(0))


def _becomes_land(m, sc):
    """"Until your next turn, he becomes a land and gains "{T}: Add {U}."" (Hydro-Man): layer 4 sets
    its card type to land (CR 205.1a: the creature type and creature subtypes go away; it's not a
    creature), layer 6 adds the mana ability (613.1d, 613.1f); until its controller's next turn
    begins (611.2a; ruling 2025-09-19)."""
    if m.group(1) in ("he", "she") and "Legendary" not in sc.cx.face.supertypes:
        raise Unsupported(f"pronoun {m.group(1)!r} for a nonlegendary object")
    color = m.group(2)
    ab = Activated(Cost(tap=True), effect=None, text="{T}: Add {%s}." % color, mana=ManaProduce([(color,)]))

    def types(g, oid, c, ce):
        fx.set_card_types(c, {"Land"})

    def grant(g, oid, c, ce, ab=ab):
        c.abilities.append(ab)
    return fx.AddEffect(fx.self_permanent(), {4: types, 6: grant}, duration="until_your_next_turn", text=m.group(0))


def _comparison(what):
    """"an opponent controls more lands than you" etc. (Beza ruling 2024-07-26: compared as that
    part of the ability resolves)."""
    def lands(g, p):
        return sum(1 for o in g.s.battlefield if g.ctrl(o) == p and "Land" in g.chars(o).types)

    def creatures(g, p):
        return sum(1 for o in g.s.battlefield if g.ctrl(o) == p and "Creature" in g.chars(o).types)
    f = {"controls more lands": lands, "controls more creatures": creatures,
         "has more life": lambda g, p: g.s.players[p].life,
         "has more cards in hand": lambda g, p: len(g.s.players[p].hand)}.get(what)
    if f is None:
        raise Unsupported(f"comparison {what!r}")
    return lambda g, ctx, f=f: f(g, _opp(ctx["controller"])) > f(g, ctx["controller"])


def _trailing_if(m, sc):
    cond = _comparison(m.group(2))
    then = et.parse_one(m.group(1) + ".", sc)
    return fx.If(cond, then, text=m.group(0))


def _double_power(m, sc):
    """CR 701.10b-c: the creature gets +X/+0, X its power as the ability resolves (negative power:
    it gets -X/-0, the same sum)."""
    sel = et.obj_ref(m.group(1), sc)
    sc.last = sel

    def pw(g, ctx, sel=sel):
        objs = [o for o in fx.objs_of(sel, g, ctx) if _on_bf(g, o)]
        return (g.chars(objs[0]).power or 0) if objs else 0
    return fx.Pump(sel, fx.Amount(pw, "its power"), 0, text=m.group(0))


class DoubleCounters(E):
    """CR 701.10e: put on each permanent as many counters of that kind as it already has."""

    def __init__(self, sel, kind, text=""):
        self.sel = sel
        self.kind = kind
        self.text = text

    def run(self, g, ctx):
        for o in fx.objs_of(self.sel, g, ctx):
            if _on_bf(g, o):
                n = g.s.objs[o].counters.get(self.kind, 0)
                if n > 0:
                    g.add_counters(o, self.kind, n, source=ctx.get("source"))


def _double_counters(m, sc):
    kind = m.group(1)
    what = m.group(2)
    if what == "~":
        sel = fx.self_permanent()
    elif what == "each creature you control":
        sel = fx.all_matching(parse_noun("creature you control"))
    else:
        raise Unsupported(f"double the counters on {what!r}")
    return DoubleCounters(sel, kind, text=m.group(0))


def _enchanted_creature():
    """"Enchanted creature" in an ability of an Aura: the creature it's attached to; if the Aura
    has left the battlefield, the one it was attached to as it last existed (CR 608.2h)."""
    def fn(g, ctx):
        src = ctx.get("source")
        o = g.s.objs.get(src)
        if o is None or o.zone != "battlefield":
            o = g.s.lki_obj.get(src)
        a = getattr(o, "attached_to", None) if o is not None else None
        return [a] if _on_bf(g, a) else []
    return Sel(fn, "enchanted creature")


def _enchanted_fights(m, sc):
    if "Aura" not in sc.cx.face.subtypes:
        raise Unsupported("'enchanted creature' outside an Aura")
    b = et.target_noun(m.group(1), sc)
    return fx.Fight(_enchanted_creature(), b, text=m.group(0))


def _bite(m, sc):
    """"Target creature you control deals damage equal to its power to <target>." The first target
    is the source, its power as the spell resolves (608.2h); an illegal first target deals no
    damage (Burn Together rulings 2023-09-01)."""
    src = et.target_noun("target creature you control", sc)
    first = len(sc.tc.specs) - 1
    t = m.group(1)
    if t == "any other target":
        to = sc.tc.add(TargetSpec("any", text="any other target", distinct_from=[first]))
    else:
        to = et.target_noun(t, sc)
    sc.last = src

    def pw(g, ctx, src=src):
        objs = [o for o in fx.objs_of(src, g, ctx) if _on_bf(g, o)]
        return max(0, g.chars(objs[0]).power or 0) if objs else 0
    return fx.Damage(fx.Amount(pw, "its power"), to, source=src, text=m.group(0))


class SacrificeThose(E):
    """"Then sacrifice it." (CR 701.21a: only a permanent you control; a target that became
    illegal is unaffected, 608.2b)."""

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        for o in fx.objs_of(self.sel, g, ctx):
            if _on_bf(g, o) and g.ctrl(o) == ctx["controller"]:
                g.sacrifice(o)


def _sacrifice_it(m, sc):
    if sc.last is None:
        raise Unsupported("'sacrifice it' without antecedent")
    return SacrificeThose(sc.last, text=m.group(0))


class RemoveFromEachOfTwo(E):
    """"Remove a +1/+1 counter from each of two creatures you control." (Dyadrine): two different
    creatures you control, each with such a counter (an action that can't be done otherwise,
    608.2d; MayThen asks only when it can)."""
    reports_done = True

    def __init__(self, kind, text=""):
        self.kind = kind
        self.text = text

    def _cands(self, g, ctx):
        p = ctx["controller"]
        return [o for o in g.s.battlefield if g.ctrl(o) == p and "Creature" in g.chars(o).types
                and g.s.objs[o].counters.get(self.kind, 0) > 0]

    def possible(self, g, ctx):
        return len(self._cands(g, ctx)) >= 2

    def run(self, g, ctx):
        p = ctx["controller"]
        cands = self._cands(g, ctx)
        if len(cands) < 2:
            return False
        chosen = []
        if len(cands) == 2:
            chosen = list(cands)  # no choice: exactly two such creatures
        while len(chosen) < 2:
            left = [o for o in cands if o not in chosen]
            opts = [Opt(f"choose:{g.ref(o)}", o, sig=("rm", g.obj_sig(o))) for o in left]
            chosen.append(g.ask(Decision(p, "ChooseCard", opts, prompt=f"Remove a {self.kind} counter from a creature you control.")))
        for o in chosen:
            g.remove_counters(o, self.kind, 1)
        return True


class GainThatMuch(E):
    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        n = ((ctx.get("data") or {}).get("n")) or 0
        if n > 0:
            g.gain_life(ctx["controller"], n, source=ctx.get("source"))


def _gain_that_much(m, sc):
    if not isinstance(sc.trigger, OpponentLosesLife):
        raise Unsupported(f"'that much' without an amount: {m.group(0)!r}")
    return GainThatMuch(text=m.group(0))


def _warp_keywords(m, sc):
    """"If that creature was cast for its warp cost, it also gains trample and haste until end of
    turn." (Full Bore): the permanent's spell was cast for its warp cost (702.185a; the cast record
    stays with the permanent it became)."""
    if sc.last is None:
        raise Unsupported("'that creature' without antecedent")
    sel = sc.last
    kws = [k.strip() for k in re.split(r",? and |, ", m.group(1))]
    for k in kws:
        if not re.fullmatch(et.KW, k):
            raise Unsupported(f"keyword {k!r}")

    def cond(g, ctx, sel=sel):
        for o in fx.objs_of(sel, g, ctx):
            if _on_bf(g, o):
                cast = g.s.objs[o].cast or {}
                return cast.get("how") == "warp"
        return False
    return fx.If(cond, fx.Pump(sel, None, None, keywords=kws), text=m.group(0))


def _amount_hook(t, sc):
    """"1 plus the number of cards named ~ in your graveyard" (Ancestral Anger: it isn't in the
    graveyard yet as it resolves, rulings), bound by effects_text's "where X is". "The amount of
    life you gained this turn" (Gumdrop Poisoner ruling 2023-09-01: life gained, whatever was
    lost) is round2_c2's AMOUNT_HOOKS entry (round 2 merge: one parser per amount; this module's
    own "where X is" pattern went)."""
    if t == "1 plus the number of cards named ~ in your graveyard":
        name = sc.cx.name
        return fx.Amount(lambda g, ctx, name=name: 1 + sum(1 for o in g.s.players[ctx["controller"]].graveyard
                                                            if g.chars(o).name == name), t)
    return None


et.AMOUNT_HOOKS.append(_amount_hook)


def _neg(x):
    return fx.Amount(lambda g, ctx, x=x: -x(g, ctx), f"-({getattr(x, 'text', x)})")


def _pump_minus_x(m, sc):
    sel = et.obj_ref(m.group(1), sc)
    sc.last = sel
    x = et.amount_of("X", sc)
    if not isinstance(x, fx.Amount):
        raise Unsupported("-X/-X without a bound X")
    return fx.Pump(sel, _neg(x), _neg(x), text=m.group(0))


def _gains_and_gets_x(m, sc):
    sel = et.obj_ref(m.group(1), sc)
    sc.last = sel
    kws = [k.strip() for k in re.split(r",? and |, ", m.group(2))]
    for k in kws:
        if not re.fullmatch(et.KW, k):
            raise Unsupported(f"keyword {k!r}")
    x = et.amount_of("X", sc)
    if not isinstance(x, fx.Amount):
        raise Unsupported("+X/+0 without a bound X")
    return fx.Pump(sel, x, 0, keywords=kws, text=m.group(0))


def _beheld_counter(m, sc):
    """Dispelling Exhale: "Counter target spell unless its controller pays {2}. If a Dragon was
    beheld, counter that spell unless its controller pays {4} instead." (CR 701.4b, 614.15)."""
    from ..engine.mana import parse_cost
    sel = et.target_noun("target " + m.group(1), sc)
    base = fx.CounterUnlessPays(parse_cost(m.group(2)), 0, sel=sel)
    more = fx.CounterUnlessPays(parse_cost(m.group(4)), 0, sel=sel)
    cond = _condition_hook(m.group(3), sc.cx)
    if cond is None:
        raise Unsupported(f"condition {m.group(3)!r}")
    return fx.If(cond, more, base, text=m.group(0))


def _counters_per_left(m, sc):
    """"Put a +1/+1 counter on ~ for each creature that left the battlefield under your control
    this turn." (Kutzil's Flanker; tokens count, the controller as each left; zones.after_move)."""
    n = fx.Amount(lambda g, ctx: _creatures_left(g, ctx["controller"]), "creatures that left")
    return fx.PutCounters(fx.self_permanent(), m.group(1), n, text=m.group(0))


def _if_you_control_otherwise(m, sc):
    """"If you control a Demon, each opponent loses 2 life and you gain 2 life. Otherwise, you lose 2
    life." (Unholy Annex; checked as the ability resolves, 608.2c)."""
    f = parse_noun(m.group(1) + " you control")
    cond = lambda g, ctx, f=f: any(f.match(g, o, ctx.get("source"), ctx) for o in g.s.battlefield)
    a = m.group(2)
    b = m.group(3)
    then = et.parse_one(a[0].upper() + a[1:] + ".", sc)
    other = et.parse_one(b[0].upper() + b[1:] + ".", sc)
    return fx.If(cond, then, other, text=m.group(0))


def _if_you_controlled(m, sc):
    """"If you controlled that permanent, draw a card." (Boomerang Basics): its controller as it
    last existed on the battlefield (CR 608.2h)."""
    if sc.last is None:
        raise Unsupported("'that permanent' without antecedent")
    sel = sc.last

    def cond(g, ctx, sel=sel):
        for o in sel.get(g, ctx):
            if not isinstance(o, tuple):
                return g.ctrl(o) == ctx["controller"]
        return False
    rest = m.group(1)
    then = et.parse_one(rest[0].upper() + rest[1:] + ".", sc)
    return fx.If(cond, then, text=m.group(0))


class ExilePlayersGraveyard(E):
    """"Exile target player's graveyard." (all the cards in it, one event)."""

    def __init__(self, who, text=""):
        self.who = who
        self.text = text

    def run(self, g, ctx):
        for p in fx.players_of(self.who, g, ctx):
            cards = list(g.s.players[p].graveyard)
            if cards:
                g.move_batch([(o, "exile", {"cause": "effect"}) for o in cards])


def _exile_players_graveyard(m, sc):
    who = et.target_noun("target player", sc)
    sc.last_player = who
    return ExilePlayersGraveyard(who, text=m.group(0))


def _put_its_counters(m, sc):
    """"When ~ dies, put its counters on target creature you control." (Essence Channeler rulings
    2024-07-26: the same number of each kind of counter it had as it last existed, -1/-1 ones
    included, put on the target)."""
    tr = sc.trigger
    if not (isinstance(tr, tg.Dies) and tr.self_):
        raise Unsupported(f"'its counters' outside a dies trigger: {m.group(0)!r}")
    sel = et.target_noun(m.group(1), sc)
    sc.last = sel
    return fx.PutLKICountersOn(sel, text=m.group(0))


# =============================================================================== Chandra, Chill of Compliance

class SurveilNoncreatureToHand(E):
    """"Surveil 1. If you put a noncreature, nonland card into your graveyard this way, put that card
    into your hand." (CR 701.25a; the card as it is in the graveyard, 608.2c)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        fx.Scry(1, surveil=True).run(g, ctx)
        for o in (ctx.get("vars") or {}).get("surveiled") or []:
            if o in g.s.objs and g.s.objs[o].zone == "graveyard":
                c = g.chars(o)
                if "Creature" not in c.types and "Land" not in c.types:
                    g.move(o, "hand", cause="effect")


class AddRestrictedMana(E):
    """"Add {U}. Spend this mana only to cast a noncreature spell." from a loyalty ability (not a mana
    ability: it uses the stack, CR 605.1a); the restriction stays with the mana (106.6)."""

    def __init__(self, colors, restrict, text=""):
        self.colors = colors
        self.restrict = restrict
        self.text = text

    def run(self, g, ctx):
        from ..engine.mana import Unit
        p = ctx["controller"]
        for c in self.colors:
            g.s.players[p].pool.append(Unit(c, source=ctx.get("source"), restrict=self.restrict))
        g.s.touch()


def _restricted_mana(m, sc):
    syms = re.findall(r"\{([WUBRGC])\}", m.group(1))
    if m.group(2) != "a noncreature spell":
        raise Unsupported(f"spend restriction {m.group(2)!r}")
    restrict = lambda g, spend: spend.get("kind") == "spell" and "Creature" not in spend["chars"].types
    return AddRestrictedMana(syms, restrict, text=m.group(0))


def _loyalty_x_line(line, cx):
    """"−X: <effect>" (CR 606.4, 107.3a): a loyalty ability whose cost removes X loyalty counters, X
    announced as it's activated (casting.activate reads cost.loyalty_x)."""
    m = re.fullmatch(r"[−-]X: (.+)", line)
    if not m or "Planeswalker" not in cx.types:
        return None
    eff, specs, modes = C.compile_effect(m.group(1), cx, ability=True)
    cost = Cost(loyalty=0)
    cost.loyalty_x = True
    return [Activated(cost, eff, specs, timing="sorcery", text=line, loyalty=0, modes=modes)]


# =============================================================================== sentences

et.PATTERNS[0:0] = [
    (r"Create (an? (?:Mutagen|Treasure|Food|Clue) token) for each ([+\-]1/[+\-]1) counter on it\.", _tokens_per_counter),
    (r"(Creature) cards in your graveyard gain \"You may cast ~ from your graveyard\" until end of turn\.", _gy_cast),
    (r"Destroy all other creatures if its power is exactly (\d+)\.", _amalia),
    (r"That creature deals (\d+) damage to each opponent\.", _attacker_deals),
    (r"Create a (\d+)/(\d+) (colorless) ((?:[A-Z][a-z]+ )+)creature token named ([A-Z][a-z]+)\.", _named_token),
    (r"Exile the top two cards of your library\. Choose one of them\. Until end of turn, you may play that card\.",
     lambda m, sc: ExileTopTwoChooseOne(2, text=m.group(0))),
    (r"Exile the top card of your library\. Until end of turn, you may play that card\.",
     lambda m, sc: fx.ExileTopMayPlay(1, "this turn", text=m.group(0))),
    (r"It deals damage equal to (the number of .+?) to (target .+?)\.", _damage_equal_count),
    (r"Its controller loses (\d+) life and you gain (\d+) life\.", _its_controller_drain),
    (r"~ deals (\d+) damage instead if (.+)\.", _dmg_instead),
    (r"That creature gets (-\d+)/(-\d+) until end of turn instead if (.+)\.", _pump_instead),
    (r"(He|She) gets \+(\d+)/\+(\d+) until end of turn\.", _he_pumps),
    (r"Until your next turn, (he|she|it) becomes a land and gains \"\{T\}: Add \{([WUBRG])\}\.\"", _becomes_land),
    (r"(.+?) if an opponent (controls more lands|controls more creatures|has more life|has more cards in hand) than you\.", _trailing_if),
    (r"Double the power of (target creature you control) until end of turn\.", _double_power),
    (r"Double the number of ([+\-]1/[+\-]1) counters on (~|each creature you control)\.", _double_counters),
    (r"Enchanted creature fights (up to one target creature an opponent controls|target creature an opponent controls)\.", _enchanted_fights),
    (r"Target creature you control deals damage equal to its power to (any other target|target .+?)\.", _bite),
    (r"Sacrifice it\.", _sacrifice_it),
    (r"Remove a (\+1/\+1) counter from each of two creatures you control\.", lambda m, sc: RemoveFromEachOfTwo(m.group(1), text=m.group(0))),
    (r"You gain that much life\.", _gain_that_much),
    (r"If that creature was cast for its warp cost, it also gains (.+?) until end of turn\.", _warp_keywords),
    (r"(Up to one target creature|Target creature) gets -X/-X until end of turn\.", _pump_minus_x),
    (r"(Target creature) gains ((?:flying|first strike|double strike|deathtouch|haste|hexproof|indestructible|lifelink|menace|reach|trample|vigilance)) and gets \+X/\+0 until end of turn\.", _gains_and_gets_x),
    (r"Counter target (spell) unless its controller pays ((?:\{[^}]+\})+)\. If (a Dragon was beheld), counter that spell unless its controller pays ((?:\{[^}]+\})+) instead\.", _beheld_counter),
    (r"Put an? (\+1/\+1) counter on ~ for each creature that left the battlefield under your control this turn\.", _counters_per_left),
    (r"If you controlled that permanent, (.+)\.", _if_you_controlled),
    (r"Surveil 1\. If you put a noncreature, nonland card into your graveyard this way, put that card into your hand\.",
     lambda m, sc: SurveilNoncreatureToHand(text=m.group(0))),
    (r"Exile target player's graveyard\.", _exile_players_graveyard),
    (r"Put its counters on (target creature you control)\.", _put_its_counters),
    (r"Add ((?:\{[WUBRGC]\})+)\. Spend this mana only to cast (a noncreature spell)\.", _restricted_mana),
    (r"If you control an? ([A-Z][a-z]+), (.+?)\. Otherwise, (.+)\.", _if_you_control_otherwise),
]

et.MERGE_PAIRS.extend([
    (r"^Add (?:\{[WUBRGC]\})+\.$", r"^Spend this mana only to cast a noncreature spell\.$"),
    (r"^Counter target spell unless its controller pays (?:\{[^}]+\})+\.$", r"^If a Dragon was beheld, counter that spell unless its controller pays (?:\{[^}]+\})+ instead\.$"),
    (r"^Surveil 1\.$", r"^If you put a noncreature, nonland card into your graveyard this way, put that card into your hand\.$"),
])


# =============================================================================== lines and statics

_SOLVED_ACTIVATED = re.compile(r"Solved — (.+)")


def _solved_activated(line, cx):
    """CR 702.169b: "Solved — [activated ability]" means "[ability]. Activate only if this Case is
    solved." (the solved designation, 719.3b). Triggered and static "Solved" abilities stay with
    ref/cards/batch8.py."""
    m = _SOLVED_ACTIVATED.fullmatch(line)
    if not m or "Case" not in cx.face.subtypes:
        return None
    act = C.split_activated(m.group(1))
    if act is None:
        return None
    ab = C.compile_activated(act[0], act[1], line, cx)
    inner = ab.condition

    def cond(g, src, p, inner=inner):
        o = g.s.objs.get(src)
        if o is None or o.zone != "battlefield" or not o.flags.get("solved"):
            return False
        return inner is None or inner(g, src, p)
    ab.condition = cond
    return [ab]


def _krang_line(line, cx):
    """"When ~ enters, if you have fewer than four cards in hand, draw cards equal to the
    difference." (CR 603.4: checked as it triggers and as it resolves, Krang ruling 2026-01-27; the
    difference as it resolves)."""
    m = re.fullmatch(r"When ~ enters, if you have fewer than (\w+) cards in hand, draw cards equal to the difference\.", line)
    if not m:
        return None
    k = num(m.group(1))
    cond = lambda g, ctx, k=k: len(g.s.players[ctx["controller"]].hand) < k
    n = fx.Amount(lambda g, ctx, k=k: max(0, k - len(g.s.players[ctx["controller"]].hand)), "the difference")
    return [Triggered(tg.Enters(self_=True), fx.Draw(n), [], condition=cond, text=line)]


@st.static_parser
def _round2_a2_statics(l, cx):
    m = re.fullmatch(r"Other creatures you control enter with an additional \+1/\+1 counter on them for each opponent who lost life this turn\.", l)
    if m:
        # Gev, Scaled Scorch (CR 614.1c, 614.12): not Gev itself, nor a creature entering at the
        # same time as Gev (ruling 2024-07-26); "each opponent who lost life this turn" counts
        # whether they lost life, not their totals.
        def count(g, src):
            return 1 if _lost(g, _opp(g.ctrl(src))) > 0 else 0

        def applies(g, ev, src):
            if ev.get("to") != "battlefield" or ev["opts"].get("controller", ev.get("owner")) != g.ctrl(src):
                return False
            if ev.get("old") == src or src in (ev.get("entering") or ()):
                return False
            return "Creature" in st.entering_chars(g, ev).types and count(g, src) > 0

        def apply(g, ev, src):
            cs = ev["opts"].setdefault("counters", {})
            cs["+1/+1"] = cs.get("+1/+1", 0) + count(g, src)
        return [Static([ReplacementPart("zone", applies, apply, text=l)], text=l)]
    m = re.fullmatch(r"If an? ((?:[A-Z][a-z]+, )+(?:or )?[A-Z][a-z]+) you control would deal damage to a permanent or player, it deals that much damage plus (\d+) instead\.", l)
    if m:
        # Valley Flamecaller (CR 614.1a): a permanent of those types you control is the source
        # (109.2); the damage it would deal to any recipient, after division (ruling 2024-07-26).
        f = parse_noun(m.group(1) + " you control")
        k = int(m.group(2))

        kinds = set(re.findall(r"[A-Z][a-z]+", m.group(1)))

        def source_pred(g, ev, src, f=f, kinds=kinds):
            s_ = ev.get("source")
            if not _on_bf(g, s_) and getattr(getattr(g, "hooks", None), "o3_lki_damage_source", False):
                # Opt-in alignment for lockstep runs (Hooks.o3_lki_damage_source; off by default): a
                # source that left the battlefield before its ability dealt the damage counts by its
                # last known information (CR 113.7a, 608.2h): a Lizard its controller controlled.
                sc = ev.get("source_chars")
                if sc is not None and sc.controller == g.ctrl(src) and kinds & set(sc.subtypes):
                    g.hooks.o3_lki_damage_hit = True
                    return True
                return False
            return _on_bf(g, s_) and f.match(g, s_, src, {"controller": g.ctrl(src)})
        return [Static([st.damage_mod_part(source_pred, None, None, lambda g, ev, src, k=k: ev["n"] + k, l)], text=l)]
    m = re.fullmatch(r"~ enters with an? (\+1/\+1) counter on it for each creature that died under your control this turn\.", l)
    if m:
        kind = m.group(1)

        def apply(g, ev, src, kind=kind):
            p = ev["opts"].get("controller", ev.get("owner"))
            k = _creatures_died(g, p)
            if k:
                cs = ev["opts"].setdefault("counters", {})
                cs[kind] = cs.get(kind, 0) + k
        p = ReplacementPart("enter_self", lambda g, ev, src: True, apply, text=l)
        p.commutes = True
        return [Static([p], text=l)]
    m = re.fullmatch(r"~ enters with a number of (\+1/\+1) counters on it equal to the amount of mana spent to cast it\.", l)
    if m:
        kind = m.group(1)

        def apply(g, ev, src, kind=kind):
            # CR 601.2h: the mana actually spent on the total cost (Dyadrine rulings 2025-07-25: a
            # copy had none spent; cost increases and reductions count).
            lo = ev.get("lki_obj")
            k = sum((lo.mana_spent or {}).values()) if lo is not None and ev.get("from") == "stack" and lo.cast else 0
            if k:
                cs = ev["opts"].setdefault("counters", {})
                cs[kind] = cs.get(kind, 0) + k
        p = ReplacementPart("enter_self", lambda g, ev, src: True, apply, text=l)
        p.commutes = True
        return [Static([p], text=l)]
    m = re.fullmatch(r"Enchanted creature gets \+(\d+)/\+(\d+) and can't be blocked by more than one creature\.", l)
    if m:
        dp, dt = int(m.group(1)), int(m.group(2))

        def fn(g, oid, c, src, dp=dp, dt=dt):
            if c.power is not None:
                c.power += dp
                c.toughness += dt
        # CR 509.1b: a blocking restriction (combat.py rule "cant_be_blocked_by_more_than_one").
        return [Static([LayerPart("7c", "attached", fn, l),
                        RulePart("cant_be_blocked_by_more_than_one", "attached")], text=l)]
    m = re.fullmatch(r"~ gets \+(\d+)/\+(\d+) for each (other artifact you control)\.", l)
    if m:
        per = st._amount_per(m.group(3), cx)
        dp, dt = int(m.group(1)), int(m.group(2))

        def fn(g, oid, c, src, per=per, dp=dp, dt=dt):
            if c.power is not None:
                k = per(g, src)
                c.power += dp * k
                c.toughness += dt * k
        return [Static([LayerPart("7c", "self", fn, l)], text=l)]
    m = re.fullmatch(r"~ has ((?:flying|vigilance|haste|trample|lifelink|deathtouch|menace|reach)(?:(?:,? and |, )(?:flying|vigilance|haste|trample|lifelink|deathtouch|menace|reach))*)\.", l)
    if m:
        from ..engine.ir import Keyword
        ks = [Keyword(k) for k in re.split(r",? and |, ", m.group(1))]

        def grant(g, oid, c, src, ks=ks):
            c.abilities.extend(ks)
        return [Static([LayerPart(6, "self", grant, l)], text=l)]
    return None


# =============================================================================== registration

C.LINE_HOOKS[0:0] = [_solved_activated]  # before batch8's "Solved —" (triggered abilities only)
C.LINE_HOOKS.append(_krang_line)
C.LINE_HOOKS.append(_loyalty_x_line)
C.POST_HOOKS.append(_fold_instead)
tg.TRIGGER_HOOKS.append(_trigger_hook)
tg.CONDITION_HOOKS.append(_condition_hook)
st.STATIC_CONDITION_HOOKS.append(_static_condition_hook)

# Case of the Uneaten Feast: "To solve — You've gained 5 or more life this turn." (CR 719.3a).
from . import batch8 as _b8  # noqa: E402
_b8._SOLVE_CONDITIONS["You've gained 5 or more life this turn"] = lambda g, ctx: _gained(g, ctx["controller"]) >= 5
