"""Card-support round 2: keyword mechanics and new subsystems.

Mechanics, each from its rule (cited where implemented): tokens with quoted abilities and named
tokens (CR 111.4, 111.10), Heartwood tokens (111.10x), sneak (702.190), start your engines! and
speed (702.179, 702.178), storied (702.195), tiered (702.183), harness and infinity (701.64,
702.186), station (702.184, 721), craft (702.167), Roles (303.7), "the next spell you cast this
turn can't be countered", mana spend restrictions (106.6), enters-or-leaves triggers.

Registered into the compiler's hooks (compiler.py KEYWORD_HOOKS / LINE_HOOKS, triggers.py
TRIGGER_HOOKS / CONDITION_HOOKS, statics.py EXTRA_STATICS, effects_text.py PATTERNS and
TOKEN_HOOKS). Strict like the rest of the compiler: a sentence these patterns don't match exactly
still raises Unsupported.
"""
import re

from ..engine import effects as fx
from ..engine.effects import Filter, E, Sel
from ..engine.ir import (Keyword, Triggered, Static, RulePart, LayerPart, ReplacementPart, TargetSpec,
                         Seq, Activated, Cost, Trigger, Effect)
from ..engine.decisions import Decision, Opt
from .. import Unsupported
from . import compiler as C
from . import triggers as tg
from . import effects_text as et
from . import statics as st
from .model import token_def
from .nouns import num, COLORS
from ..engine.mana import parse_cost

_COST = r"((?:\{[^}]+\})+)"


def _nc(cls):
    cls.__deepcopy__ = lambda self, memo: self
    cls.__copy__ = lambda self: self
    return cls


# =============================================================================== tokens (CR 111)

_COLOR_WORDS = r"((?:(?:white|blue|black|red|green|colorless)(?:,? and |, | )?)+)"
_TOKEN_RE = (r"(\d+)/(\d+) " + _COLOR_WORDS + r"((?:[A-Z][a-z]+ )+)((?:artifact )?creature) tokens?"
             r"(?: named ([A-Z][A-Za-z']+(?: [A-Z][A-Za-z']+)*))?(?: with (.+))?")


def _checked(factory, desc):
    """Compile the token's text now (the card is supported only if its tokens' abilities are)."""
    cd = factory()
    for f in cd.faces:
        try:
            f.abilities()
        except Unsupported as e:
            raise Unsupported(f"token {desc!r}: {e}")
    return factory


def _token_text(rest):
    """'flying and "<ability>"' / '"<ability>"' / 'prowess' -> the token's rules text, one
    ability per line (CR 111.4: the abilities the effect gives it)."""
    lines = []
    q = re.fullmatch(r'(?:(.+?),? and )?"(.+)"', rest)
    if q:
        if q.group(1):
            lines.append(", ".join(k.strip().capitalize() for k in re.split(r",? and |, ", q.group(1))))
        lines.append(q.group(2))
        return "\n".join(lines)
    if '"' in rest:
        raise Unsupported(f"token abilities {rest!r}")
    kws = [k.strip() for k in re.split(r",? and |, ", rest)]
    return ", ".join(k.capitalize() if i == 0 else k for i, k in enumerate(kws))


def _token_hook(d):
    """Creature tokens with a name (CR 111.4: "named Cadet") and/or quoted abilities ("with
    \\"Whenever this token attacks, you gain 1 life.\\"") or keywords the base parser doesn't
    take (prowess); Heartwood tokens (111.10x)."""
    m = re.fullmatch(r"Heartwood tokens?", d)
    if m:
        # CR 111.10x: a red and green Heartwood artifact token with "{T}: Add {R} or {G}."
        return _checked(lambda: token_def("Heartwood", ("Artifact",), ("Heartwood",), ("R", "G"),
                                          text="{T}: Add {R} or {G}."), d)
    m = re.fullmatch(_TOKEN_RE, d)
    if not m:
        return None
    rest = m.group(7)
    named = m.group(6)
    if named is None and (rest is None or re.fullmatch(
            r"(flying|haste|vigilance|reach|trample|deathtouch|lifelink|menace|defender)"
            r"(( and |, )(flying|haste|vigilance|reach|trample|deathtouch|lifelink|menace|defender))*", rest)):
        return None  # the base parser's tokens
    p, t = int(m.group(1)), int(m.group(2))
    cols = tuple(COLORS[c] for c in re.findall(r"white|blue|black|red|green", m.group(3)))
    subs = tuple(m.group(4).split())
    types = ("Artifact", "Creature") if m.group(5).startswith("artifact") else ("Creature",)
    text = _token_text(rest) if rest else ""
    return _checked(lambda: token_def(named or " ".join(subs), types, subs, cols, p, t, text, named=named), d)


et.TOKEN_HOOKS.append(_token_hook)


et.PATTERNS[0:0] = [
    # 'Create a 1/1 ... token with "<ability>"' (the sentence ends with the closing quote) and
    # 'Create a 2/2 ... token named Cadet.' (CR 111.4).
    (r'Create (a|an|one|two|three|four) ((?:tapped )?.+? tokens? with ".+")', et._token2),
    (r"Create (a|an|one|two|three|four) ((?:tapped )?.+? tokens? named [A-Z][A-Za-z']+(?: [A-Z][A-Za-z']+)*)\.", et._token2),
]


# =============================================================================== amounts

def _amount_hook(t, sc):
    if t == "the amount of life you gained this turn":
        # Moseo: life gained this turn by the ability's controller (CR 119.3, 608.2h: as the
        # ability resolves). Player.life_gained counts every gain this turn (zones.gain_life).
        return fx.Amount(lambda g, ctx: g.s.players[ctx["controller"]].life_gained, t)
    if t == "the number of artifacts you control":
        return fx.count_amount(Filter(types=["Artifact"], controller="you", text="artifact you control"))
    return None


et.AMOUNT_HOOKS.append(_amount_hook)


def _return_upto_one_mv_x(m, sc):
    """Moseo: "Return up to one target creature card with mana value X or less from your
    graveyard to the battlefield, where X is ..." (X from the "where" clause, bound by
    effects_text._where_x; it's computed as the target is chosen and again as the ability
    resolves, CR 603.3d, 608.2b)."""
    x = getattr(sc, "x_expr", None)
    if x is None:
        raise Unsupported("mana value X without a 'where X is' clause")
    sc.x_used = True
    f = Filter(types=["Creature"], owner="you", mv=("<=", lambda g, src, ctx, x=x: x(g, ctx)),
               text="creature card with mana value X or less")
    spec = TargetSpec("card", filt=f, zone="graveyard", n=(0, 1), text=m.group(0))
    sel = sc.tc.add(spec)
    sc.last = sel
    return fx.MoveTo(sel, "battlefield", text=m.group(0), controller="you")


et.PATTERNS[0:0] = [
    (r"Return up to one target creature card with mana value X or less from your graveyard to the battlefield\.",
     _return_upto_one_mv_x),
]


# =============================================================================== Dalkovan Encampment

def _attack_this_turn_tokens(m, sc):
    """Dalkovan Encampment: "Whenever you attack this turn, create two 1/1 red Warrior creature
    tokens that are tapped and attacking. Sacrifice them at the beginning of the next end step."
    A delayed triggered ability (CR 603.7a, 603.7b: for the rest of the turn) that triggers each
    time you declare one or more attackers (ruling 2025-04-04; 508.1m); its effect is mobilize's
    (702.181a): each token's controller chooses what it attacks as it enters (508.4), and a
    delayed trigger sacrifices them at the next end step (603.7)."""
    from .recent import _Mobilize
    ab = Triggered(tg.YouAttack(), _Mobilize(num(m.group(1))), text=m.group(0))
    return et._DelayedThisTurn(ab, text=m.group(0))


et.MERGE_PAIRS.append((r"^Whenever you attack this turn, create \w+ 1/1 red Warrior creature tokens that are tapped and attacking\.$",
                       r"^Sacrifice them at the beginning of the next end step\.$"))
et.PATTERNS[0:0] = [
    (r"Whenever you attack this turn, create (two) 1/1 red Warrior creature tokens that are tapped and attacking\. "
     r"Sacrifice them at the beginning of the next end step\.", _attack_this_turn_tokens),
]


# =============================================================================== sneak (702.190)

def _unblocked_attackers(g, p):
    """Unblocked creatures p controls (CR 509.1h: attacking creatures no creature was declared to
    block, once blockers have been declared)."""
    comb = g.s.combat
    if comb is None or not comb.blockers_declared:
        return []
    return [a for a in comb.attackers if a in g.s.objs and g.ctrl(a) == p and a not in comb.blocked
            and "Creature" in g.chars(a).types]


@_nc
class SneakReturn(fx.CostPart):
    """CR 702.190a: "... you may cast this spell by paying [cost] and returning an unblocked
    creature you control to its owner's hand rather than paying this spell's mana cost" (an
    alternative cost, 118.9, 601.2b, 601.2f-h). The creature is chosen before the mana payment,
    as the EUT asks it (a PayCost decision `choose:<ref>`), and returned as the costs are paid
    (601.2h): it stays on the battlefield through 601.2g, so a chosen untapped (vigilant) creature
    may still be tapped for mana. What it was attacking is recorded on the spell for 702.190b."""
    name = "sneak"

    def castable(self, g, p, oid, cost, spend):
        return bool(_unblocked_attackers(g, p)) and g.mana_payable(p, cost, 0, spend)

    def payable(self, g, p, oid):
        return bool(_unblocked_attackers(g, p))

    def pre_mana(self, g, p, oid, ctx, cost, x):
        """Called by casting.cast before the mana payment."""
        cands = _unblocked_attackers(g, p)
        if not cands:
            from ..engine.decisions import IllegalAction
            raise IllegalAction("no unblocked creature to return for sneak")
        opts = [Opt(f"choose:{g.ref(c)}", c, sig=("sneak", g.obj_sig(c))) for c in cands]
        ctx["sneak_return"] = g.ask(Decision(p, "PayCost", opts, subject=g.ref(oid),
                                             prompt="Sneak: return an unblocked creature you control to its owner's hand."))

    def pay(self, g, p, oid, ctx):
        x = ctx.get("sneak_return")
        if x is None:
            self.pre_mana(g, p, oid, ctx, [], 0)
            x = ctx["sneak_return"]
        if x not in _unblocked_attackers(g, p):
            from ..engine.decisions import IllegalAction
            raise IllegalAction("the creature chosen for sneak is no longer an unblocked creature you control")
        dfd = g.s.combat.attackers.get(x)
        o = g.s.objs.get(oid)
        if o is not None:
            o.flags["sneak_attacking"] = dfd if dfd is not None else ("none",)
        ctx.setdefault("paid", {}).setdefault("returned", []).append((x, dfd))
        g.move(x, "hand", cause="cost")


def _sneak_defender_ok(g, d, p):
    """CR 508.4a, 506.3c: the player or planeswalker it would attack is still there and still a
    planeswalker controlled by a defending player."""
    if d is None or d == ("none",):
        return False
    if isinstance(d, tuple):
        return d[0] == "player" and d[1] != p
    o = g.s.objs.get(d)
    if o is None or o.zone != "battlefield":
        return False
    c = g.chars(d)
    return "Planeswalker" in c.types and c.controller != p


def _sneak(p, cx):
    """CR 702.190a: "Sneak [cost]": cast during your declare blockers step for [cost] and
    returning an unblocked creature you control (altcast.alt_timing "sneak"); 702.190b: a
    permanent spell whose sneak cost was paid enters tapped and attacking what the returned
    creature was attacking (506.3a-c, 508.4, 508.4d: unblocked). `cast:<ref>#sneak` (the EUT's
    option string)."""
    m = re.fullmatch(r"sneak " + _COST, p, re.I)
    if not m:
        return None
    k = Keyword("sneak", value=parse_cost(m.group(1)), text=p)
    k.alt_zone = "hand"
    k.extra = {"parts": [SneakReturn()]}

    def applies(g, ev, src):
        lo = ev.get("lki_obj")
        return (ev.get("from") == "stack" and lo is not None and (lo.cast or {}).get("how") == "sneak"
                and "sneak_attacking" in lo.flags)

    def enters(g, ev, src):
        lo = ev["lki_obj"]
        ev["opts"]["tapped"] = True
        d = lo.flags.get("sneak_attacking")
        ctrl = ev["opts"].get("controller", lo.controller)
        if g.s.combat is not None and g.s.active == ctrl and _sneak_defender_ok(g, d, ctrl):
            ev["opts"]["attacking"] = d  # 506.3b: only under an attacking player's control
    rep = ReplacementPart("enter_self", applies, enters, text=p + " (enters tapped and attacking)")
    rep.commutes = True
    return [k, Static([rep], text=p + " (enters tapped and attacking)")]


C.KEYWORD_HOOKS.append(_sneak)


# =============================================================================== speed (702.178, 702.179)

def speed_of(g, p):
    """CR 702.179f: a player without speed has speed 0 for effects that refer to it."""
    return g.s.players[p].speed


@_nc
class SpeedLifeLoss(Trigger):
    """CR 702.179d: "Whenever one or more opponents lose life during your turn, if your speed is
    less than 4, your speed increases by 1. This ability triggers only once each turn." An
    inherent triggered ability of a player with speed: no source, controlled by that player.
    The intervening "if" (603.4) is checked here as it triggers and again as it resolves
    (Triggered.condition); "only once each turn" is kept in turn_flags."""
    events = ("lifeloss",)
    lookback = False

    def __init__(self):
        self.zones = ()

    def match(self, g, ev, src, c, ctrl):
        s = g.s
        if ev["player"] == ctrl or s.active != ctrl or s.players[ctrl].speed >= 4:
            return None
        k = ("speed_trigger", ctrl)
        if s.turn_flags.get(k):
            return None
        s.turn_flags[k] = True
        return {"player": ctrl}


def _speed_up(g, ctx):
    pl = g.s.players[ctx["controller"]]
    if pl.speed < 4:
        pl.speed += 1  # 702.179c-d
        g.s.touch()


SPEED_ABILITY = Triggered(SpeedLifeLoss(), fx.Fn_(_speed_up, "your speed increases by 1"),
                          condition=lambda g, ctx: g.s.players[ctx["controller"]].speed < 4,
                          text="Whenever one or more opponents lose life during your turn, if your speed is less than 4, "
                               "your speed increases by 1. This ability triggers only once each turn.")


def speed_sba(g):
    """CR 702.179a (a state-based action): a player who controls a permanent with start your
    engines! and has no speed gets speed 1. Every player with speed has the 702.179d trigger
    (kept as a permanent entry among the delayed triggers: no source, never expires)."""
    s = g.s
    done = False
    for p in (0, 1):
        pl = s.players[p]
        if pl.speed == 0 and any(g.ctrl(x) == p and g.chars(x).has_kw("start your engines!") for x in s.battlefield):
            pl.speed = 1
            s.touch()
            done = True
        if pl.speed > 0 and not any(d.get("ability") is SPEED_ABILITY and d["controller"] == p for d in s.delayed):
            s.delayed.append({"trigger": SPEED_ABILITY.trigger, "ability": SPEED_ABILITY, "controller": p,
                              "source": None, "source_chars": None, "once": False, "data": {}})
    return done


def _start_your_engines(p, cx):
    if p.lower() != "start your engines!":
        return None
    return [Keyword("start your engines!", text=p)]


C.KEYWORD_HOOKS.append(_start_your_engines)

_KWS = r"(?:flying|first strike|double strike|deathtouch|haste|hexproof|indestructible|lifelink|menace|reach|trample|vigilance)"


def _max_speed(line, cx):
    """CR 702.178a: "Max speed — [Ability]" means "As long as your speed is 4, this object has
    '[Ability]'" (its controller's speed on the battlefield; its owner's elsewhere, glossary
    "Max Speed"). The ability is granted in layer 6 while the condition holds."""
    m = re.fullmatch(r"Max speed — (.+)", line)
    if not m:
        return None
    inner = m.group(1)
    k = re.fullmatch(r"~ has (" + _KWS + r"(?:(?:,| and|, and) " + _KWS + r")*)\.", inner)
    if k:
        granted = [Keyword(x.strip()) for x in re.split(r",? and |, ", k.group(1))]
    else:
        res = C.compile_line(inner, cx)
        if res == "spell" or not res or any(a.kind not in ("activated", "triggered", "keyword") for a in res):
            raise Unsupported(f"max speed ability {inner!r}")
        if any(tuple(a.zones) != ("battlefield",) for a in res):
            raise Unsupported(f"max speed ability functioning outside the battlefield {inner!r} (CR 702.178b)")
        granted = list(res)

    def grant(g, oid, c, src, granted=tuple(granted)):
        c.abilities.extend(granted)

    def cond(g, src):
        return speed_of(g, g.ctrl(src)) >= 4
    return [Static([LayerPart(6, "self", grant, text=line)], text=line, condition=cond)]


C.LINE_HOOKS.append(_max_speed)


# =============================================================================== storied (702.195)

def _story_count(g, p):
    """Permanents p controls that are artifacts, Sagas and/or legendary, each counted once
    (702.195a; ruling 2026-06-29)."""
    n = 0
    for x in g.s.battlefield:
        c = g.chars(x)
        if c.controller == p and ("Artifact" in c.types or "Saga" in c.subtypes or "Legendary" in c.supertypes):
            n += 1
    return n


def _check_story(g, p):
    """CR 702.195a: "Any time you control three or more permanents that are artifacts, Sagas,
    and/or legendary and you don't have an enduring story, you have an enduring story for the rest
    of the game" (only while p controls a permanent with storied, ruling 2026-06-29)."""
    s = g.s
    pl = s.players[p]
    if pl.flags.get("enduring_story"):
        return False
    if not any(g.ctrl(x) == p and g.chars(x).has_kw("storied") for x in s.battlefield):
        return False
    if _story_count(g, p) >= 3:
        pl.flags["enduring_story"] = True  # 702.195b: a designation on the player
        s.touch()
        return True
    return False


@_nc
class StoryWatch(Trigger):
    """Not a triggered ability: storied applies "any time" (702.195a), so the designation is
    checked after every zone change or control change (and at each state-based action check,
    `designations_sba`), not only between steps. It never triggers."""
    events = ("zone", "token_created", "transformed")
    lookback = False

    def __init__(self):
        self.zones = ()

    def match(self, g, ev, src, c, ctrl):
        _check_story(g, ctrl)
        return None


STORY_WATCH = Triggered(StoryWatch(), fx.Fn_(lambda g, ctx: None, "none"), text="(storied watch)")


def designations_sba(g):
    """At each state-based action check (before the actions themselves, CR 704.3, so a third
    permanent that the legend rule then removes still counts: ruling 2026-06-29): speed
    (702.179a) and the enduring story (702.195a)."""
    done = speed_sba(g)
    s = g.s
    for p in (0, 1):
        if s.players[p].flags.get("enduring_story"):
            continue
        if any(g.ctrl(x) == p and g.chars(x).has_kw("storied") for x in s.battlefield):
            if not any(d.get("ability") is STORY_WATCH and d["controller"] == p for d in s.delayed):
                s.delayed.append({"trigger": STORY_WATCH.trigger, "ability": STORY_WATCH, "controller": p,
                                  "source": None, "source_chars": None, "once": False, "data": {}})
            _check_story(g, p)
    return done


def _storied(p, cx):
    if p.lower() != "storied":
        return None
    return [Keyword("storied", text=p)]


C.KEYWORD_HOOKS.append(_storied)


def _free_first_equip_static(l, cx):
    """Kíli the Resourceful: "As long as you have an enduring story, you may pay {0} rather than
    pay the equip cost of the first equip ability you activate each turn." An alternative cost
    (CR 118.9) of equip abilities (702.6a) the controller activates, while the condition holds."""
    if l != "As long as you have an enduring story, you may pay {0} rather than pay the equip cost of the first equip ability you activate each turn.":
        return None
    return [Static([RulePart("free_first_equip", "you")], text=l,
                   condition=lambda g, src: bool(g.s.players[g.ctrl(src)].flags.get("enduring_story")))]


st.EXTRA_STATICS[0:0] = [_free_first_equip_static]


def free_equip_available(g, p, oid, ab):
    if getattr(ab, "keyword", None) != "equip":
        return False
    if g.s.turn_flags.get(("equip_activated", p)):
        return False  # only the first equip ability p activates each turn
    return any(not isinstance(src, int) or g.ctrl(src) == p for _, src in g.rule_parts("free_first_equip"))


def choose_free_equip(g, p, oid, ab):
    """As an equip ability is activated (602.2b, 601.2b): pay {0} instead of its cost? An
    OptionalCost decision `cost:free` / `no` (proposed option string), forced to `cost:free` when
    the equip cost itself can't be paid. Every equip activation counts for "the first equip
    ability you activate each turn"."""
    if getattr(ab, "keyword", None) != "equip":
        return False
    avail = free_equip_available(g, p, oid, ab)
    g.s.turn_flags[("equip_activated", p)] = True
    if not avail:
        return False
    cost = g.ability_cost_mana(p, oid, ab)
    spend = {"kind": "ability", "obj": oid, "ability": ab}
    if not g.mana_payable(p, cost, 0, spend, exclude=g._own_cost_exclusion(oid, ab)):
        return True
    return g.ask(Decision(p, "OptionalCost", [Opt("cost:free", True), Opt("no", False)], subject=g.ref(oid),
                          prompt="Pay {0} rather than the equip cost?"))


# =============================================================================== the next spell can't be countered

@_nc
class NextSpellWatch(Trigger):
    """Not a triggered ability: applies "The next spell you cast this turn can't be countered"
    (CR 611.2a, a continuous effect from a resolved ability that waits for that spell; 101.2)
    to the spell as it becomes cast (601.2i). Every such effect waiting applies to that same
    spell; all of them end with the turn (turn_flags)."""
    events = ("cast",)
    lookback = False

    def __init__(self):
        self.zones = ()

    def match(self, g, ev, src, c, ctrl):
        s = g.s
        k = ("next_spell_uncounterable", ctrl)
        if ev.get("player") == ctrl and s.turn_flags.get(k):
            for it in s.stack:
                if it.id == ev.get("item"):
                    it.uncounterable = True
            s.turn_flags[k] = 0
            s.touch()
        return None


NEXT_SPELL_WATCH = Triggered(NextSpellWatch(), fx.Fn_(lambda g, ctx: None, "none"), text="(next spell watch)")


class NextSpellCantBeCountered(E):
    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        s = g.s
        p = ctx["controller"]
        s.turn_flags[("next_spell_uncounterable", p)] = 1
        if not any(d.get("ability") is NEXT_SPELL_WATCH and d["controller"] == p and d.get("this_turn") == s.turn
                   for d in s.delayed):
            s.delayed.append({"trigger": NEXT_SPELL_WATCH.trigger, "ability": NEXT_SPELL_WATCH, "controller": p,
                              "source": None, "source_chars": None, "once": False, "data": {},
                              "this_turn": s.turn})
        s.touch()


et.PATTERNS[0:0] = [
    (r"The next spell you cast this turn can't be countered\.", lambda m, sc: NextSpellCantBeCountered(text=m.group(0))),
]


# =============================================================================== tiered (702.183)

def _tiered_header(line, cx):
    """CR 702.183a: the "Tiered" line heads the modes (compiler.compile_modes reads each
    bullet's "Name — {cost} — effect"); the spell's Modes need no further change."""
    if line == "Tiered":
        return lambda modes: modes
    return None


C.MODAL_HEADER_HOOKS.append(_tiered_header)


# =============================================================================== harness and infinity (701.64, 702.186)

def harnessed(g, oid):
    o = g.s.objs.get(oid)
    return o is not None and o.zone == "battlefield" and bool(o.flags.get("harnessed"))


class Harness(E):
    """CR 701.64a: "Harness [this permanent]" means "If this permanent isn't harnessed, it becomes
    harnessed." 701.64b: a designation (not copiable) kept until it leaves the battlefield (a new
    object has no flags, 400.7)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        o = g.s.objs.get(ctx.get("source"))
        if o is not None and o.zone == "battlefield" and not o.flags.get("harnessed"):
            o.flags["harnessed"] = True
            g.s.touch()


et.PATTERNS[0:0] = [
    (r"Harness ~\.", lambda m, sc: Harness(text=m.group(0))),
]


def _infinity(line, cx):
    """CR 702.186a-b: "∞ — [Ability]" means "As long as this permanent is harnessed, it has
    [ability]" (granted in layer 6 while the condition holds)."""
    m = re.fullmatch(r"∞ — (.+)", line)
    if not m:
        return None
    res = C.compile_line(m.group(1), cx)
    if res == "spell" or not res or any(a.kind not in ("activated", "triggered", "keyword") for a in res):
        raise Unsupported(f"infinity ability {m.group(1)!r}")

    def grant(g, oid, c, src, granted=tuple(res)):
        c.abilities.extend(granted)
    return [Static([LayerPart(6, "self", grant, text=line)], text=line,
                   condition=lambda g, src: harnessed(g, src))]


C.LINE_HOOKS.append(_infinity)


def _flicker_upto(m, sc):
    """"Exile up to one other target nonland permanent you control, then return that card to the
    battlefield under its owner's control" (The Mind Stone): fx.Flicker on an optional target
    (CR 115.1d "up to one"; 400.7: a new object; 111.8: a token doesn't return)."""
    sel = et.target_noun(m.group(1), sc)
    sc.last = fx.var("returned")
    return fx.Flicker(sel, controller="you" if m.group(2) == "your" else "owner", text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Exile (up to one (?:other )?target .+?), then return that card to the battlefield under (your|its owner's) control\.", _flicker_upto),
]


# =============================================================================== man-lands, animated artifacts (613.1d-f)

def _becomes_colored_eot(m, sc):
    """"~ becomes a 2/2 red and white Ox creature until end of turn. It's still a land."
    (Restless Bivouac, Restless Cottage): as recent._becomes_colored (Restless Reef's "Until end
    of turn, ~ becomes ..."): layer 4 adds the creature type and subtype and keeps the land type
    (205.1b), layer 5 sets the colors (105.3), layer 7b sets P/T."""
    from .recent import _becomes_colored
    return _becomes_colored(m, sc)


def _becomes_artifact_creature(m, sc):
    """"This artifact becomes a 4/4 artifact creature until end of turn." (Firdoch Core): an
    object that becomes an "artifact creature" keeps all its prior card types and subtypes (CR
    205.1b: Kindred, Shapeshifter and its changeling creature types stay); P/T set in layer 7b
    (613.4b)."""
    sel = fx.self_permanent()
    sc.last = sel
    parts = et.parse_becomes(m.group(1), sc)
    return fx.AddEffect(sel, parts, duration="eot", text=m.group(0))


from .recent import COLOR_LIST as _COLOR_LIST  # noqa: E402

et.PATTERNS[0:0] = [
    (r"(~|It|Target .+?) becomes an? (\d+/\d+) " + _COLOR_LIST + r"(.+?) until end of turn\. It's still an? (?:land|artifact|enchantment)\.",
     _becomes_colored_eot),
    (r"~ becomes (an? \d+/\d+ artifact creature) until end of turn\.", _becomes_artifact_creature),
]


# =============================================================================== triggers

@_nc
class ActivateLoyalty(Trigger):
    """"Whenever you activate a loyalty ability" (CR 606.1, 602.2: it triggers as the ability
    becomes activated, 602.2i)."""
    events = ("activated",)
    lookback = False

    def __init__(self):
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        ab = ev.get("ability")
        if ev.get("player") != ctrl or ab is None or getattr(ab, "loyalty", None) is None:
            return None
        return {"player": ctrl, "obj": ev.get("source")}


@tg.trigger_parser
def _r2c2_triggers(line, cx):
    m = re.match(r"^When ~ enters or leaves the battlefield, (.+)$", line)
    if m:
        from .round2_a2 import EntersOrLeaves  # one class for both wordings (round 2 merge)
        return EntersOrLeaves(to=None), m.group(1)
    m = re.match(r"^Whenever you activate a loyalty ability, (.+)$", line)
    if m:
        return ActivateLoyalty(), m.group(1)
    return None


# =============================================================================== effects

def _counter_on_each(m, sc):
    """"Put a loyalty counter on each planeswalker you control." (Way of the Mentor)."""
    from .nouns import parse_noun
    sel = fx.all_matching(parse_noun(m.group(2)))
    sc.last = sel
    return fx.PutCounters(sel, m.group(1), 1, text=m.group(0))


def _pump_x_0(m, sc):
    sel = et.obj_ref(m.group(1), sc)
    sc.last = sel
    x = et.amount_of("X", sc)  # bound by "where X is ..."
    return fx.Pump(sel, x, 0, text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Put a (loyalty) counter on each (planeswalker you control)\.", _counter_on_each),
    # The noun stops before "gains ... and" (Ancestral Anger's "gains trample and gets +X/+0" is
    # round2_a2's sentence; a broader noun shadowed it, round 2 merge).
    (r"(Target (?:(?! and | gains? ).)+?|~) gets \+X/\+0 until end of turn\.", _pump_x_0),
]


def _extra_land_this_turn(g, ctx):
    k = ("extra_land_plays", ctx["controller"])
    g.s.turn_flags[k] = g.s.turn_flags.get(k, 0) + 1  # CR 305.2 (casting.land_plays)
    g.s.touch()


et.PATTERNS[0:0] = [
    (r"You may play an additional land this turn\.",
     lambda m, sc: fx.Fn_(_extra_land_this_turn, "you may play an additional land this turn")),
]


# =============================================================================== Great Divide Guide

def _lands_and_allies_have(l, cx):
    """Great Divide Guide: "Each land and Ally you control has "{T}: Add one mana of any color.""
    A layer-6 ability-adding effect (CR 613.1f) on each permanent its controller controls that is
    a land or an Ally (itself included: ruling 2025-10-02); the granted mana ability follows
    302.6 on a creature."""
    m = re.fullmatch(r'Each land and Ally you control has "(.+)"', l)
    if not m:
        return None
    res = C.compile_line(m.group(1), cx)
    if res == "spell" or not res or any(a.kind != "activated" for a in res):
        raise Unsupported(f"granted ability {m.group(1)!r}")

    def aff(g, oid, c, src):
        return ("Land" in c.types or "Ally" in c.subtypes) and c.controller == g.ctrl(src)

    def grant(g, oid, c, src, res=tuple(res)):
        c.abilities.extend(res)
    return [Static([LayerPart(6, aff, grant, l)], text=l)]


st.EXTRA_STATICS[0:0] = [_lands_and_allies_have]


def _that_many_counters_on_it(m, sc):
    """Drake Hatcher: "Whenever this creature deals combat damage to a player, put that many
    incubation counters on it." "That many" is the damage dealt (the triggering event's amount);
    "it" is the source as long as it's still on the battlefield (400.7)."""
    if not isinstance(sc.trigger, tg.CombatDamagePlayer) or not getattr(sc.trigger, "self_", False):
        raise Unsupported(f"'that many' without a damage trigger: {m.group(0)!r}")
    sel = fx.self_permanent()
    sc.last = sel
    return fx.PutCounters(sel, m.group(1), fx.event_amount(), text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Put that many (incubation) counters on (?:it|~)\.", _that_many_counters_on_it),
]


# =============================================================================== blocking restrictions

def _cant_block_or_be_blocked_by_non(l, cx):
    """"~ can't block or be blocked by non-Spirit creatures." (Realm of Koh's Spirit token): two
    blocking restrictions (CR 509.1b): as a blocker it can block only Spirits; as an attacker
    only Spirits can block it."""
    m = re.fullmatch(r"~ can't block or be blocked by non-([A-Z][a-z]+) creatures\.", l)
    if not m:
        return None
    sub = m.group(1)

    def is_sub(g, oid, sub=sub):
        return sub in g.chars(oid).subtypes
    return [Static([RulePart("can_block_only", "self", filter=lambda g, a, f=is_sub: f(g, a)),
                    RulePart("cant_be_blocked_by", "self", filter=lambda g, b, a, f=is_sub: not f(g, b))],
                   text=l)]


st.EXTRA_STATICS[0:0] = [_cant_block_or_be_blocked_by_non]


def _self_gets_per(l, cx):
    """"~ gets +1/+1 for each artifact you control." (Simulacrum Synthesizer's Construct token,
    counting itself: ruling 2024-04-12): a layer-7c effect (CR 613.4c) of its own static ability,
    counted from its controller's permanents whenever its characteristics are determined."""
    m = re.fullmatch(r"~ gets \+(\d+)/\+(\d+) for each (.+?)\.", l)
    if not m:
        return None
    per = st._amount_per(m.group(3), cx)
    dp, dt = int(m.group(1)), int(m.group(2))

    def fn(g, oid, c, src, per=per, dp=dp, dt=dt):
        if c.power is not None:
            k = per(g, src)
            c.power += dp * k
            c.toughness += dt * k
    return [Static([LayerPart("7c", "self", fn, l)], text=l)]


st.EXTRA_STATICS[0:0] = [_self_gets_per]


# =============================================================================== craft (702.167)

@_nc
class CraftMaterials(fx.CostPart):
    """CR 702.167a-b: "Exile [materials] from among permanents you control and/or cards in your
    graveyard": a material named by a card type alone is a permanent of that type you control or
    a card of that type in your graveyard (702.167b); tokens may be exiled (rulings 2023-11-10);
    never the crafting permanent itself (it's exiled by "Exile this permanent"). Chosen one at a
    time (PayCost `choose:<ref>`, `done` once the minimum is met for "N or more"), after the mana
    is paid (as the EUT asks it). The exiled cards are the ones "used to craft" (702.167c)."""
    name = "craft"

    def __init__(self, filt, n_min, n_max):
        self.filt = filt
        self.n_min = n_min
        self.n_max = n_max  # None: any number ("four or more")

    def _cands(self, g, p, oid, chosen=()):
        s = g.s
        out = [x for x in s.battlefield if x != oid and x not in chosen and g.ctrl(x) == p
               and self.filt.match(g, x, oid, {"controller": p})]
        # "cards in your graveyard": a token isn't a card (CR 111.6) and can't move from there
        # (111.8): a Treasure sacrificed for this activation's mana is in the graveyard until
        # state-based actions (704.5d) but isn't a material (campaign13 834382).
        out += [x for x in s.players[p].graveyard if x not in chosen and not s.objs[x].token
                and self.filt.match(g, x, oid, {"controller": p})]
        return out

    def payable(self, g, p, oid):
        return len(self._cands(g, p, oid)) >= self.n_min

    def pay(self, g, p, oid, ctx):
        chosen = []
        while self.n_max is None or len(chosen) < self.n_max:
            cands = self._cands(g, p, oid, chosen)
            if not cands:
                break
            opts = [Opt("done", None)] if len(chosen) >= self.n_min else []
            opts += [Opt(f"choose:{g.ref(x)}", x, sig=("craft", g.obj_sig(x))) for x in cands]
            x = g.ask(Decision(p, "PayCost", opts, subject=g.ref(oid), prompt="Craft: choose a material to exile."))
            if x is None:
                break
            chosen.append(x)
        sf = ctx.get("_o16_exile_self_first")
        if sf is not None:
            # Opt-in lockstep alignment (ref/engine/casting.py): "exile this
            # permanent" first. Tagged when it can matter: a battlefield material
            # with a leaves-the-battlefield trigger.
            if any(x in g.s.battlefield and any("leaves the battlefield" in (getattr(a, "text", "") or "")
                                                for a in g.chars(x).abilities) for x in chosen):
                g.hooks.o16_craft_self_first_hit = True
            sf()
        res = g.move_batch([(x, "exile", {"cause": "cost"}) for x in chosen])
        return {"crafted_with": [r for r in res if r is not None]}


class CraftReturn(E):
    """"Return this card to the battlefield transformed under its owner's control" (702.167a):
    the card the cost exiled (a new object, 400.7); a card that isn't a transforming
    double-faced card stays in exile (712.14a; ruling 2023-11-10). The cards used to craft it
    are linked to the permanent (702.167c)."""

    def __init__(self, text=""):
        self.text = text
        self.put = fx.PutOntoBattlefield(Sel(lambda g, ctx: [x for x in [((ctx.get("paid") or {}).get("exiled_self"))]
                                                              if x is not None and x in g.s.objs]),
                                         ("exile",), controller="owner", transformed=True, text=text)

    def run(self, g, ctx):
        self.put.run(g, ctx)
        new = (ctx.get("vars") or {}).get("returned") or []
        used = [x for x in (ctx.get("paid") or {}).get("crafted_with", []) if x in g.s.objs and g.s.objs[x].zone == "exile"]
        for n in new:
            if used:
                g.s.linked[(n, "crafted")] = list(used)
                g.s.touch()


# A material named by a card type alone: a permanent you control or a card in your graveyard of
# that type (702.167b). "creature": Tithing Blade (round2_d2's craft wording, folded in here).
_MATERIAL_WORDS = {"artifact": Filter(types=["Artifact"], text="artifact"),
                   "creature": Filter(types=["Creature"], text="creature")}


def _craft(p, cx):
    """CR 702.167a: "Craft with [materials] [cost]" means "[Cost], Exile this permanent, Exile
    [materials] from among permanents you control and/or cards in your graveyard: Return this
    card to the battlefield transformed under its owner's control. Activate only as a sorcery."
    `activate:<ref>:n` (the EUT's numbering: its place among the card's activated abilities)."""
    m = re.fullmatch(r"Craft with (.+?) " + _COST, p)
    if not m:
        return None
    mat = m.group(1)
    n_min, n_max = 1, 1
    k = re.fullmatch(r"(one|two|three|four|five|six) or more (.+)", mat)
    if k:
        n_min, n_max = num(k.group(1)), None
        mat = k.group(2)
    if mat in _MATERIAL_WORDS:
        f = _MATERIAL_WORDS[mat]
    elif mat == "nonlands with activated abilities":
        def has_act(g, c, o, src, ctx):
            return any(a.kind == "activated" for a in c.abilities)
        f = Filter(not_types=["Land"], custom=has_act, text="nonland with activated abilities")
    else:
        raise Unsupported(f"craft materials {mat!r}")
    cost = Cost(mana=parse_cost(m.group(2)), exile_self=True, text=p)
    cost.extra_part = CraftMaterials(f, n_min, n_max)
    a = Activated(cost, CraftReturn(text=p), [], timing="sorcery", text=p)
    a.keyword = "craft"
    return [a]


C.KEYWORD_HOOKS.append(_craft)


# =============================================================================== Vehicles, Pilot

def _becomes_artifact_creature_no_pt(m, sc):
    """"This Vehicle becomes an artifact creature until end of turn." (Bladewheel Chariot): as
    crew's effect (702.122a; ref/cards/batch8.py), layer 4; it keeps its printed P/T (301.7b)."""
    from .batch8 import _becomes_artifact_creature
    return fx.AddEffect(fx.self_permanent(), {4: _becomes_artifact_creature}, duration="eot", text=m.group(0))


et.PATTERNS[0:0] = [
    (r"~ becomes an artifact creature until end of turn\.", _becomes_artifact_creature_no_pt),
]


def _crews_as_though(l, cx):
    """Defend the Rider's Pilot token: "This token saddles Mounts and crews Vehicles as though its
    power were 2 greater." Crew (702.122a) counts it with +2 (casting.crew_power). Saddle
    (702.171a) isn't implemented: no card with saddle compiles, so the saddle half can't come
    up; a saddle implementation must read rule crew_power_bonus too."""
    m = re.fullmatch(r"~ saddles Mounts and crews Vehicles as though its power were (\d+) greater\.", l)
    if not m:
        return None
    return [Static([RulePart("crew_power_bonus", "self", n=int(m.group(1)))], text=l)]


st.EXTRA_STATICS[0:0] = [_crews_as_though]


def _bounce_spell_or_permanent(m, sc):
    """Jeskai Revelation: "Return target spell or permanent to its owner's hand": one target word
    whose candidates are spells on the stack and permanents (CR 115.1; as recent.py's "spell or
    creature"). A spell returned to a hand isn't countered (rulings 2025-04-04)."""
    def ok(g, c, o, src, ctx):
        return o is not None and o.zone in ("stack", "battlefield")
    spec = TargetSpec("object", filt=Filter(custom=ok, text="spell or permanent"), zone="stack|battlefield",
                      text="target spell or permanent")
    sel = sc.tc.add(spec)
    sc.last = sel
    return fx.MoveTo(sel, "hand", text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Return target spell or permanent to its owner's hand\.", _bounce_spell_or_permanent),
]


# =============================================================================== Roles (303.7, 111.10)

# The Royal Role token (111.10m) and "Create a Royal Role token attached to that creature" are
# round2_b2.py's Role code (ROLES, CreateRoleAttached; round 2 merge: one implementation, with
# 303.4i: no token when the creature is gone). Its static ability is parsed here:


def _enchanted_gets_and_ward(l, cx):
    """"Enchanted creature gets +1/+1 and has ward {1}." (the Royal Role): layer 7c (613.4c) and
    a granted ward ability in layer 6 (702.21a)."""
    m = re.fullmatch(r"(Enchanted|Equipped) creature gets \+(\d+)/\+(\d+) and has ward \{(\d+)\}\.", l)
    if not m:
        return None
    dp, dt = int(m.group(2)), int(m.group(3))
    ward = C.ward_ability([int(m.group(4))], 0)

    def pt(g, oid, c, src, dp=dp, dt=dt):
        if c.power is not None:
            c.power += dp
            c.toughness += dt

    def grant(g, oid, c, src, ward=ward):
        c.abilities.append(ward)
    return [Static([LayerPart("7c", "attached", pt, l), LayerPart(6, "attached", grant, l)], text=l)]


st.EXTRA_STATICS[0:0] = [_enchanted_gets_and_ward]


# =============================================================================== "When it dies this turn" (603.7)

@_nc
class _DiesThisTurn(Trigger):
    """The creature `oid` dies (a creature put into a graveyard from the battlefield, 700.4):
    looks back (603.10a). One object: the same creature as it was when the effect resolved
    (603.7c)."""
    events = ("zone",)
    lookback = True

    def __init__(self, oid):
        self.oid = oid
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev.get("old") != self.oid or ev.get("from") != "battlefield" or ev.get("to") != "graveyard":
            return None
        lki = ev.get("lki")
        if lki is None or "Creature" not in lki.types:
            return None
        return {"obj": ev.get("new"), "old": self.oid}


class WhenItDiesThisTurn(E):
    """"When it dies this turn, <effect>" (Turn Inside Out): a delayed triggered ability created
    as the spell resolves (603.7a), for the object "it" names then; it triggers once (603.7c) and
    only this turn (603.7b)."""

    def __init__(self, sel, ability, text=""):
        self.sel = sel
        self.ability = ability
        self.text = text

    def run(self, g, ctx):
        for oid in fx.objs_of(self.sel, g, ctx):
            o = g.s.objs.get(oid)
            if o is None or o.zone != "battlefield":
                continue
            ab = Triggered(_DiesThisTurn(oid), self.ability.effect, text=self.text)
            g.s.delayed.append({"trigger": ab.trigger, "ability": ab, "controller": ctx["controller"],
                                "source": ctx.get("source"), "source_chars": ctx.get("source_chars") or ctx.get("chars"),
                                "once": True, "this_turn": g.s.turn, "data": {}})
            g.s.touch()


def _when_it_dies_this_turn(m, sc):
    if sc.last is None:
        raise Unsupported("'When it dies this turn' without antecedent")
    eff = et.parse_one(m.group(1)[0].upper() + m.group(1)[1:], sc)
    return WhenItDiesThisTurn(sc.last, Triggered(None, eff, text=m.group(0)), text=m.group(0))


et.PATTERNS[0:0] = [
    (r"When it dies this turn, (.+\.)", _when_it_dies_this_turn),
]


# =============================================================================== Unidentified Hovership

def _link_exiled_card(line, cx):
    """A face whose text refers to "the exiled card" (607.2a: the card its own exile ability
    exiled, Unidentified Hovership) links its exile effects like "exiled with ~" does
    (effects_text._exile reads cx.links_exile). Never compiles the line itself."""
    if not getattr(cx, "links_exile", False) and "the exiled card's owner" in (cx.face.text or ""):
        cx.links_exile = True
    return None


C.LINE_HOOKS[0:0] = [_link_exiled_card]


class ExiledCardsOwnersManifestDread(E):
    """"The exiled card's owner manifests dread." (Unidentified Hovership): the owner of each
    card its linked ability exiled that's still in exile (607.2a; 400.7) manifests dread
    (701.62a), in APNAP order; nothing if there's no such card (ruling 2024-09-20)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        s = g.s
        ids = [x for x in s.linked.get((ctx.get("source"), "exiled"), ()) if x in s.objs and s.objs[x].zone == "exile"]
        owners = {s.objs[x].owner for x in ids}
        for p in (s.active, 1 - s.active):
            if p in owners:
                fx.Manifest(dread=True).run(g, dict(ctx, controller=p))


et.PATTERNS[0:0] = [
    (r"The exiled card's owner manifests dread\.", lambda m, sc: ExiledCardsOwnersManifestDread(text=m.group(0))),
]


# =============================================================================== station (702.184, 721)

class StationCounters(E):
    """CR 702.184a: "Put a number of charge counters on this permanent equal to the tapped
    creature's power": its power as the ability resolves, or as it last existed on the
    battlefield (rulings 2025-07-25); none for a negative power."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        tapped = ((ctx.get("paid") or {}).get("tapped") or [None])[0]
        if tapped is None:
            return
        if tapped in g.s.objs and g.s.objs[tapped].zone == "battlefield":
            pw = g.chars(tapped).power
        else:
            c = g.s.lki.get(tapped)
            pw = c.power if c is not None else None
        src = ctx.get("source")
        o = g.s.objs.get(src)
        if pw is None or pw <= 0 or o is None or o.zone != "battlefield":
            return
        g.add_counters(src, "charge", pw, source=src)


def _station(p, cx):
    """CR 702.184a: "Station" means "Tap another untapped creature you control: Put a number of
    charge counters on this permanent equal to the tapped creature's power. Activate only as a
    sorcery." The tapped creature is chosen as the cost is paid (PayCost `choose:<ref>`)."""
    if p.lower() != "station":
        return None
    f = Filter(types=["Creature"], controller="you", text="creature you control")
    f.other = True
    cost = Cost(tap_others=(f, 1), text="Tap another untapped creature you control")
    a = Activated(cost, StationCounters(text=p), [], timing="sorcery", text=p)
    a.keyword = "station"
    return [a]


C.KEYWORD_HOOKS.append(_station)


def _station_symbol(line, cx):
    """CR 721.2a: "{N+}[abilities]" means "As long as this permanent has N or more charge counters
    on it, it has [abilities]" (the Oracle text writes the symbol "N+ | "). A P/T box (721.2b)
    isn't supported here."""
    m = re.fullmatch(r"(\d+)\+ \| (.+)", line)
    if not m:
        return None
    if cx.face.power is not None or cx.face.toughness is not None:
        # A station card with a P/T box (Spacecraft: "It's an artifact creature at N+"): 721.2b
        # (a creature with that base P/T at N+) and 721.2c (no P/T off the battlefield) aren't
        # implemented.
        raise Unsupported("station card with a power/toughness box (CR 721.2b-c)")
    n = int(m.group(1))
    res = C.compile_line(m.group(2), cx)
    if res == "spell" or not res or any(a.kind not in ("activated", "triggered", "keyword") for a in res):
        raise Unsupported(f"station symbol ability {m.group(2)!r}")

    def grant(g, oid, c, src, res=tuple(res)):
        c.abilities.extend(res)

    def cond(g, src, n=n):
        o = g.s.objs.get(src)
        return o is not None and o.counters.get("charge", 0) >= n
    return [Static([LayerPart(6, "self", grant, text=line)], text=line, condition=cond)]


C.LINE_HOOKS.append(_station_symbol)


def _copy_token_legendary(m, sc):
    """"Create a token that's a copy of target artifact or enchantment you control, except it's
    legendary." (Adagia): the exception is part of the token's copiable values (CR 707.9b)."""
    sel = et.target_noun(m.group(1), sc)
    sc.last = fx.var("token")

    def legendary(cop):
        cop.supertypes = set(cop.supertypes) | {"Legendary"}
    return fx.CopyToken(sel, text=m.group(0), extra=legendary)


et.PATTERNS[0:0] = [
    (r"Create a token that's a copy of (target .+?), except it's legendary\.", _copy_token_legendary),
]


# =============================================================================== The Irencrag

def _have_become_named(m, sc):
    """The Irencrag: "you may have ~ become a legendary Equipment artifact named Everflame,
    Heroes' Legacy" (indefinitely: ruling 2023-09-01). Type change in layer 4 (613.1d: legendary
    artifact with the artifact type Equipment, 205.1a: the new artifact subtype replaces the old
    ones); the new name isn't a copy effect, so it isn't part of the copiable values (707.2)."""
    name = m.group(1)
    sel = fx.self_permanent()
    sc.last = sel

    def typ(g, oid, c, ce, name=name):
        c.types = {"Artifact"}
        c.supertypes = set(c.supertypes) | {"Legendary"}
        from .compiler import subtypes_of
        c.subtypes = (set(c.subtypes) - subtypes_of("Artifact")) | {"Equipment"}
        c.name = name
    return fx.AddEffect(sel, {4: typ}, duration="forever", text=m.group(0))


def _gains_equip_and_loses_others(m, sc):
    """"... it gains equip {3} and "Equipped creature gets +3/+3" and loses all other abilities."
    One layer-6 effect (613.1f): every ability it had is removed, then these two are its
    abilities (the abilities it gains aren't "other" abilities)."""
    eq = C.equip_ability(parse_cost(m.group(1)))
    inner = m.group(2) if m.group(2).endswith(".") else m.group(2) + "."
    res = C.compile_line(inner, sc.cx)
    if res == "spell" or not res or any(a.kind != "static" for a in res):
        raise Unsupported(f"granted ability {m.group(2)!r}")
    sel = sc.last if sc.last is not None else fx.self_permanent()

    def abil(g, oid, c, ce, new=tuple([eq] + list(res))):
        c.abilities = list(new)
    return fx.AddEffect(sel, {6: abil}, duration="forever", text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Have ~ become a legendary Equipment artifact named ([A-Z][A-Za-z', ]+)\.", _have_become_named),
    (r"It gains equip ((?:\{[^}]+\})+) and \"(.+)\" and loses all other abilities\.", _gains_equip_and_loses_others),
]


# =============================================================================== Locus of Enlightenment (runtime guard)

_LOCUS_LINES = (
    "~ has each activated ability of the exiled cards used to craft it. You may activate each of those abilities only once each turn.",
    "Whenever you activate an ability that isn't a mana ability, copy it. You may choose new targets for the copy.",
)


def _locus(line, cx):
    """The Enigma Jewel's back face, Locus of Enlightenment, isn't implemented (the crafted cards'
    activated abilities, each once each turn per instance, mana abilities included, CR 702.167c,
    and their `activate:` numbering; copying every activated ability, 707.10). The front face
    plays exactly; once a crafted Locus is on the battlefield the engine raises Unsupported."""
    if line not in _LOCUS_LINES:
        return None
    return [st.runtime_guard(lambda g, o: o.zone == "battlefield",
                             "Locus of Enlightenment (abilities of the cards used to craft it, CR 702.167c)", line)]


C.LINE_HOOKS[0:0] = [_locus]
