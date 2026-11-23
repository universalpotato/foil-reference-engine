"""Trigger conditions (CR 603) parsed from Oracle text."""
import re

from ..engine.ir import Trigger
from ..engine.effects import Filter
from .nouns import parse_noun, num
from .. import Unsupported


def _nc(cls):
    cls.__deepcopy__ = lambda self, memo: self
    return cls


@_nc
class Enters(Trigger):
    events = ("zone",)
    lookback = False

    def __init__(self, filt=None, self_=False, or_self=False, zones=("battlefield",)):
        self.filt = filt
        self.self_ = self_
        self.or_self = or_self
        self.zones = zones

    def match(self, g, ev, src, c, ctrl):
        if ev.get("to") != "battlefield":
            return None
        obj = ev.get("new")
        if obj is None or obj not in g.s.objs:
            return None
        if self.self_ or (self.or_self and obj == src):
            return {"obj": obj} if obj == src else None
        if self.filt is not None and self.filt.match(g, obj, src, {"controller": ctrl}):
            return {"obj": obj}
        return None


@_nc
class Dies(Trigger):
    """Leaves-the-battlefield to a graveyard: looks back (CR 603.10a)."""
    events = ("zone",)
    lookback = True

    def __init__(self, filt=None, self_=False, to="graveyard", or_self=False):
        self.filt = filt
        self.self_ = self_
        self.to = to
        self.or_self = or_self
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev.get("from") != "battlefield":
            return None
        if self.to is not None and ev.get("to") != self.to:
            return None
        old = ev["old"]
        lki = ev.get("lki")
        if self.self_ or (self.or_self and old == src):
            if old != src:
                return None
            if self.to == "graveyard" and "Creature" not in lki.types and not getattr(self, "any_type", False):
                return None  # "dies" = creature put into a graveyard from the battlefield (700.4)
            return {"obj": ev.get("new"), "old": old, "lki": lki}
        if self.filt is not None and self.filt.match_chars(g, lki, ev["lki_obj"], src, {"controller": ctrl}):
            if self.filt.other and old == src:
                return None
            return {"obj": ev.get("new"), "old": old, "lki": lki, "player": lki.controller}
        return None


@_nc
class Attacks(Trigger):
    events = ("attacks",)

    def __init__(self, filt=None, self_=False):
        self.filt = filt
        self.self_ = self_
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        obj = ev["obj"]
        if self.self_:
            return {"obj": obj, "defender": ev["defender"]} if obj == src else None
        if self.filt.match(g, obj, src, {"controller": ctrl}):
            return {"obj": obj, "defender": ev["defender"]}
        return None


@_nc
class YouAttack(Trigger):
    events = ("attackers_declared",)

    def __init__(self):
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        return {"player": ev["player"]} if ev["player"] == ctrl else None


@_nc
class Blocks(Trigger):
    events = ("blocks",)

    def __init__(self, self_=True):
        self.self_ = self_
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        return {"obj": ev["attacker"]} if ev["obj"] == src else None


@_nc
class CombatDamagePlayer(Trigger):
    events = ("damage",)

    def __init__(self, filt=None, self_=False, combat=True):
        self.filt = filt
        self.self_ = self_
        self.combat = combat
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if self.combat and not ev.get("combat"):
            return None
        t = ev["target"]
        if not isinstance(t, tuple):
            return None
        s = ev["source"]
        if self.self_:
            return {"player": t[1], "n": ev["n"]} if s == src else None
        sc = ev.get("source_chars")
        o = g.s.objs.get(s)
        if sc is not None and self.filt.match_chars(g, sc, o, src, {"controller": ctrl}):
            return {"player": t[1], "n": ev["n"], "obj": s}
        return None


@_nc
class Step(Trigger):
    events = ("step",)

    def __init__(self, step, whose):
        self.step = step
        self.whose = whose
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev["step"] != self.step:
            return None
        if self.whose == "you" and ev["active"] != ctrl:
            return None
        if self.whose == "opp" and ev["active"] == ctrl:
            return None
        return {"player": ev["active"]}


@_nc
class CastSpell(Trigger):
    events = ("cast",)

    def __init__(self, filt=None, who="you", nth=None):
        self.filt = filt
        self.who = who
        self.nth = nth
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if self.who == "you" and ev["player"] != ctrl:
            return None
        if self.who == "opp" and ev["player"] == ctrl:
            return None
        if self.filt is not None and not self.filt.match_chars(g, ev["chars"], g.s.objs.get(ev["obj"]), src, {"controller": ctrl}):
            return None
        if self.nth is not None and len(g.s.players[ev["player"]].spells_cast) != self.nth:
            return None
        return {"obj": ev["obj"], "player": ev["player"], "item": ev["item"]}


@_nc
class GainLife(Trigger):
    events = ("lifegain",)

    def __init__(self, who="you"):
        self.who = who
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if self.who == "you" and ev["player"] != ctrl:
            return None
        return {"player": ev["player"], "n": ev["n"]}


@_nc
class DrawCard(Trigger):
    events = ("draw",)

    def __init__(self, who="you", nth=None):
        self.who = who
        self.nth = nth
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if self.who == "you" and ev["player"] != ctrl:
            return None
        if self.who == "opp" and ev["player"] == ctrl:
            return None
        if self.nth is not None and ev.get("nth") != self.nth:
            return None
        return {"player": ev["player"]}


@_nc
class Sacrificed(Trigger):
    """"Whenever you sacrifice a/an ...": a leaves-the-battlefield ability (sacrificing moves the
    permanent from the battlefield to the graveyard, CR 701.21a, 603.6c), so it looks back in
    time (603.10a): a source sacrificed with other permanents for one cost or effect sees them
    leave (Biotech Specialist sacrificed with two artifacts for Rottenmouth Viper), and a
    sacrificed permanent's own ability triggers (a The Sackville-Bagginses token, campaign12
    812531)."""
    events = ("sacrificed",)
    lookback = True

    def __init__(self, filt, who="you"):
        self.filt = filt
        self.who = who
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if self.who == "you" and ev["player"] != ctrl:
            return None
        lki = ev.get("lki")
        if lki is None:
            return None
        if self.filt is not None and not self.filt.match_chars(g, lki, g.s.lki_obj.get(ev["old"]), src, {"controller": ctrl}):
            return None
        return {"player": ev["player"], "old": ev["old"]}


@_nc
class BecomesTarget(Trigger):
    events = ("targeted",)

    def __init__(self, by_opponent=True):
        self.by_opponent = by_opponent
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev["target"] != src:
            return None
        if self.by_opponent and ev["controller"] == ctrl:
            return None
        return {"item": ev["item"], "player": ev["controller"]}


@_nc
class WardTrigger(BecomesTarget):
    """Ward's trigger condition (CR 702.21a). "Ward abilities of those creatures don't trigger"
    (Nowhere to Run, rule `ward_doesnt_trigger`) is checked as the event happens only: a ward
    ability that already triggered still resolves (603.2; ruling 2024-09-20)."""

    def match(self, g, ev, src, c, ctrl):
        d = super().match(g, ev, src, c, ctrl)
        if d and src in g.s.objs and g.rule_applies(src, "ward_doesnt_trigger"):
            return None
        return d


@_nc
class CounterPutOn(Trigger):
    events = ("counter_added",)

    def __init__(self, kind, self_=True):
        self.kind = kind
        self.self_ = self_
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev["obj"] != src or (self.kind and ev["kind"] != self.kind):
            return None
        return {"n": ev["n"]}


@_nc
class EntersOrAttacks(Trigger):
    events = ("zone", "attacks")

    def __init__(self):
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev["type"] == "zone":
            return {"obj": src} if ev.get("to") == "battlefield" and ev.get("new") == src else None
        return {"obj": src, "defender": ev["defender"]} if ev["obj"] == src else None


@_nc
class AnyCastSpell(Trigger):
    events = ("cast",)

    def __init__(self, filt):
        self.filt = filt
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if self.filt is not None and not self.filt.match_chars(g, ev["chars"], g.s.objs.get(ev["obj"]), src, {"controller": ctrl}):
            return None
        return {"obj": ev["obj"], "player": ev["player"]}


@_nc
class IfTrigger(Trigger):
    """A trigger condition plus its intervening "if" clause (CR 603.4), checked as the event
    occurs."""

    def __init__(self, inner, cond, zones):
        self.inner = inner
        self.cond = cond
        self.events = inner.events
        self.lookback = inner.lookback
        self.zones = zones

    def match(self, g, ev, src, c, ctrl):
        data = self.inner.match(g, ev, src, c, ctrl)
        if not data:
            return data
        ctx = {"source": src, "controller": ctrl, "event": ev, "data": data}
        return data if self.cond(g, ctx) else None


@_nc
class StateCounters(Trigger):
    """CR 603.8 state trigger: "When there are N or more <kind> counters on ~"."""
    events = ()
    state = True

    def __init__(self, kind, n):
        self.kind = kind
        self.n = n
        self.zones = ("battlefield",)

    def holds(self, g, src):
        o = g.s.objs.get(src)
        return o is not None and o.counters.get(self.kind, 0) >= self.n

    def match(self, g, ev, src, c, ctrl):
        return None


# =========================================================================== parsing

STEP_WORDS = {"upkeep": "Upkeep", "end step": "End", "draw step": "Draw",
              "precombat main phase": "Main1", "combat": "BeginCombat"}


@_nc
class TurnedFaceUp(Trigger):
    """"When ~ is turned face up" (CR 708.8: not an enters-the-battlefield event)."""
    events = ("turned_face_up",)

    def __init__(self):
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        return {"obj": src, "player": ev.get("player")} if ev.get("obj") == src else None


@_nc
class CommitCrime(Trigger):
    """"Whenever you commit a crime" (CR 700.13): you cast a spell, activate an ability or put
    a triggered ability on the stack that targets an opponent, something an opponent controls,
    or a card in an opponent's graveyard. One crime per spell or ability, however many such
    targets it has."""
    events = ("targeted",)

    def __init__(self):
        self.zones = ("battlefield",)

    def _criminal(self, g, ev, ctrl):
        t = ev["target"]
        if isinstance(t, tuple):
            if t[0] == "player":
                return t[1] != ctrl
            it = next((x for x in g.s.stack if x.id == t[1]), None)
            return it is not None and it.controller != ctrl
        o = g.s.objs.get(t)
        if o is None:
            return False
        if o.zone in ("battlefield", "stack"):
            return g.ctrl(t) != ctrl
        if o.zone == "graveyard":
            return o.owner != ctrl
        return False

    def match(self, g, ev, src, c, ctrl):
        if ev.get("controller") != ctrl or ev.get("copy") or not self._criminal(g, ev, ctrl):
            # A copy's targets (Leyline of Resonance's copy given new targets, 707.10c) aren't a
            # crime: nobody cast, activated or put a triggered ability on the stack (campaign12
            # 814091: Magda, the Hoardmaster).
            return None
        key = ("crime", src, ev.get("item"))
        if g.s.turn_flags.get(key):
            return None
        g.s.turn_flags[key] = True
        return {"player": ctrl, "item": ev.get("item")}


@_nc
class AnyOf(Trigger):
    """"Whenever A and whenever B, ...": one ability with two trigger conditions; an event
    meeting both triggers it once."""

    def __init__(self, triggers):
        self.triggers = list(triggers)
        self.events = tuple(sorted({e for t in self.triggers for e in t.events}))
        self.lookback = False
        if any(t.lookback for t in self.triggers):
            raise Unsupported("look-back trigger in a combined trigger condition")
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        for t in self.triggers:
            if ev["type"] in t.events:
                d = t.match(g, ev, src, c, ctrl)
                if d:
                    return d
        return None


# Extension hooks (call order: see the note above compiler.KEYWORD_HOOKS): this file's own
# parsers (@trigger_parser, @condition_parser below), then ref/cards/recent.py, then
# ref/cards/batch7.py. The first non-None result wins.
# TRIGGER_HOOKS fn(line, cx) -> (Trigger, effect text) | None: tried first in parse_trigger;
# CONDITION_HOOKS fn(text, cx) -> predicate(g, ctx) | None: tried after the built-in conditions.
TRIGGER_HOOKS = []
CONDITION_HOOKS = []


def parse_trigger(line, cx):
    """Returns (Trigger, effect text)."""
    for h in TRIGGER_HOOKS:
        r = h(line, cx)
        if r is not None:
            return r
    m = re.match(r"^(Whenever .+?) and (whenever .+?), (.+)$", line)
    if m and not m.group(1).startswith("Whenever you ") and " and " not in m.group(1):
        # Each part is a complete trigger condition: "Whenever X enters and whenever Y enters".
        try:
            a, _ = parse_trigger(m.group(1) + ", X", cx)
            b, _ = parse_trigger("W" + m.group(2)[1:] + ", X", cx)
        except Unsupported:
            a = None
        if a is not None:
            return AnyOf([a, b]), m.group(3)
    m = re.match(r"^When there are (\w+) or more (\w+) counters on ~, (.+)$", line)
    if m:
        rest = re.sub(r"^exile it\.", "exile ~.", m.group(3))
        return StateCounters(m.group(2), num(m.group(1))), rest
    m = re.match(r"^When(?:ever)? ~ enters(?: the battlefield)?, (.+)$", line)
    if m:
        return Enters(self_=True), m.group(1)
    m = re.match(r"^When(?:ever)? ~ enters(?: the battlefield)? or attacks, (.+)$", line)
    if m:
        return EntersOrAttacks(), m.group(1)
    m = re.match(r"^Whenever ~ or another (.+?) enters(?: the battlefield)?, (.+)$", line)
    if m:
        f = parse_noun(m.group(1))
        f.other = True
        return Enters(filt=f, or_self=True), m.group(2)
    m = re.match(r"^Whenever (a|an|another|one or more) (.+?) enters(?: the battlefield)?( under your control)?, (.+)$", line)
    if m:
        noun = m.group(2) + (" you control" if m.group(3) else "")
        f = parse_noun(noun)
        if m.group(1) == "another":
            f.other = True
        if m.group(1) == "one or more":
            raise Unsupported("one-or-more enters trigger (batching)")
        return Enters(filt=f), m.group(4)
    m = re.match(r"^Whenever you commit a crime, (.+)$", line)
    if m:
        return CommitCrime(), m.group(1)
    m = re.match(r"^When ~ is turned face up, (.+)$", line)
    if m:
        return TurnedFaceUp(), m.group(1)
    m = re.match(r"^When ~ dies, (.+)$", line)
    if m:
        return Dies(self_=True), m.group(1)
    m = re.match(r"^Whenever ~ or another (.+?) dies, (.+)$", line)
    if m:
        f = parse_noun(m.group(1))
        f.other = True
        return Dies(filt=f, or_self=True), m.group(2)
    m = re.match(r"^Whenever (a|an|another) (.+?) dies, (.+)$", line)
    if m:
        f = parse_noun(m.group(2))
        if m.group(1) == "another":
            f.other = True
        return Dies(filt=f), m.group(3)
    m = re.match(r"^When ~ leaves the battlefield, (.+)$", line)
    if m:
        d = Dies(self_=True, to=None)
        d.any_type = True
        return d, m.group(1)
    m = re.match(r"^When ~ is put into a graveyard from the battlefield, (.+)$", line)
    if m:
        d = Dies(self_=True)
        d.any_type = True
        return d, m.group(1)
    m = re.match(r"^Whenever ~ attacks, (.+)$", line)
    if m:
        return Attacks(self_=True), m.group(1)
    m = re.match(r"^Whenever ~ blocks, (.+)$", line)
    if m:
        return Blocks(), m.group(1)
    m = re.match(r"^Whenever you attack, (.+)$", line)
    if m:
        return YouAttack(), m.group(1)
    m = re.match(r"^Whenever (a|an|another) (.+?) attacks, (.+)$", line)
    if m:
        f = parse_noun(m.group(2))
        if m.group(1) == "another":
            f.other = True
        return Attacks(filt=f), m.group(3)
    m = re.match(r"^Whenever ~ deals combat damage to a player, (.+)$", line)
    if m:
        return CombatDamagePlayer(self_=True), m.group(1)
    m = re.match(r"^Whenever (a|an) (.+?) deals combat damage to a player, (.+)$", line)
    if m:
        return CombatDamagePlayer(filt=parse_noun(m.group(2))), m.group(3)
    m = re.match(r"^At the beginning of (your|each|each opponent's) (upkeep|end step|draw step|precombat main phase), (.+)$", line)
    if m:
        whose = {"your": "you", "each": "each", "each opponent's": "opp"}[m.group(1)]
        return Step(STEP_WORDS[m.group(2)], whose), m.group(3)
    m = re.match(r"^At the beginning of combat on your turn, (.+)$", line)
    if m:
        return Step("BeginCombat", "you"), m.group(1)
    m = re.match(r"^Whenever you cast your (first|second) spell each turn, (.+)$", line)
    if m:
        return CastSpell(None, "you", nth={"first": 1, "second": 2}[m.group(1)]), m.group(2)
    m = re.match(r"^Whenever you cast a spell, (.+)$", line)
    if m:
        return CastSpell(None, "you"), m.group(1)
    m = re.match(r"^Whenever you cast (a|an) (.+?) spell, (.+)$", line)
    if m:
        return CastSpell(parse_spell_noun(m.group(2)), "you"), m.group(3)
    m = re.match(r"^Whenever an opponent casts (a|an) (.+?) spell, (.+)$", line)
    if m:
        return CastSpell(parse_spell_noun(m.group(2)), "opp"), m.group(3)
    m = re.match(r"^Whenever a player casts (a|an) (.+?) spell, (.+)$", line)
    if m:
        return AnyCastSpell(parse_spell_noun(m.group(2))), m.group(3)
    m = re.match(r"^Whenever you gain life, (.+)$", line)
    if m:
        return GainLife("you"), m.group(1)
    m = re.match(r"^Whenever you draw your second card each turn, (.+)$", line)
    if m:
        return DrawCard("you", nth=2), m.group(1)
    m = re.match(r"^Whenever you draw a card, (.+)$", line)
    if m:
        return DrawCard("you"), m.group(1)
    m = re.match(r"^Whenever an opponent draws a card, (.+)$", line)
    if m:
        return DrawCard("opp"), m.group(1)
    m = re.match(r"^Whenever you sacrifice (a|an|another) (.+?), (.+)$", line)
    if m:
        noun = m.group(2)
        f = Filter(text="permanent") if noun == "permanent" else parse_noun(noun)
        return Sacrificed(f, "you"), m.group(3)
    m = re.match(r"^Whenever one or more \+1/\+1 counters are put on ~, (.+)$", line)
    if m:
        return CounterPutOn("+1/+1"), m.group(1)
    raise Unsupported(f"trigger: {line!r}")


def parse_spell_noun(t):
    t = t.strip()
    if t in ("noncreature",):
        return Filter(not_types=["Creature"], text=t)
    if t in ("instant or sorcery",):
        return Filter(any_types=["Instant", "Sorcery"], text=t)
    if t == "creature":
        return Filter(types=["Creature"], text=t)
    if t == "multicolored":
        return Filter(multicolored=True, text=t)
    f = parse_noun(t)
    return f


def parse_condition(t, cx):
    """Intervening-if conditions (CR 603.4). Returns predicate(g, ctx) or None."""
    if t == "you gained life this turn":
        return lambda g, ctx: g.s.players[ctx["controller"]].life_gained > 0
    m = re.fullmatch(r"you control (\w+) or more (.+)", t)
    if m:
        n = num(m.group(1))
        f = parse_noun(m.group(2) + " you control")
        return lambda g, ctx, n=n, f=f: sum(1 for o in g.s.battlefield if f.match(g, o, ctx.get("source"), ctx)) >= n
    if t == "it was kicked" or t == "~ was kicked":
        return lambda g, ctx: bool(g.s.objs.get(ctx.get("source")) and g.s.objs[ctx["source"]].kicked)
    m = re.fullmatch(r"you control (a|an|no) (.+)", t)
    if m:
        f = parse_noun(m.group(2) + " you control")
        want = m.group(1) != "no"
        return lambda g, ctx, f=f, want=want: any(
            f.match(g, o, ctx.get("source"), ctx) for o in g.s.battlefield) == want
    m = re.fullmatch(r"(\w+) or more creatures died this turn", t)
    if m:
        n = num(m.group(1))
        return lambda g, ctx, n=n: g.s.turn_flags.get("creatures_died", 0) >= n
    m = re.fullmatch(r"you gained (\d+) or more life this turn", t)
    if m:
        n = int(m.group(1))
        return lambda g, ctx, n=n: g.s.players[ctx["controller"]].life_gained >= n
    if t == "an opponent lost life this turn":
        return lambda g, ctx: g.s.players[1 - ctx["controller"]].life_lost > 0
    if t in ("it had no counters on it", "it had one or more counters on it", "it had counters on it"):
        from ..engine.effects import event_obj_chars
        want = "no" not in t
        return lambda g, ctx, want=want: any(
            v > 0 for v in (getattr(event_obj_chars(g, ctx)[1], "counters", None) or {}).values()) == want
    if t == "it was a creature":
        from ..engine.effects import event_obj_chars
        return lambda g, ctx: "Creature" in (getattr(event_obj_chars(g, ctx)[0], "types", None) or ())
    m = re.fullmatch(r"its (power|toughness) is greater than ~'s \1 or its (power|toughness) is greater than ~'s \2", t)
    if m:
        from ..engine.effects import event_obj_chars, self_chars
        a, b = m.group(1), m.group(2)

        def cond(g, ctx, a=a, b=b):
            c, _ = event_obj_chars(g, ctx)
            me = self_chars(g, ctx)
            if c is None or me is None:
                return False
            return any(getattr(c, k) is not None and getattr(me, k) is not None and getattr(c, k) > getattr(me, k)
                       for k in (a, b))
        return cond
    m = re.fullmatch(r"there are (\w+) or more card types among cards in (your graveyard|all graveyards)", t)
    if m:
        n = num(m.group(1))
        from ..engine.effects import card_types_among

        def cond(g, ctx, n=n, mine=m.group(2) == "your graveyard"):
            ps = [ctx["controller"]] if mine else [0, 1]
            ids = [o for p in ps for o in g.s.players[p].graveyard]
            return len(card_types_among(g, ids)) >= n
        return cond
    m = re.fullmatch(r"(?:it|~) has (\w+) or more ([\w+\-/]+) counters on it", t)
    if m:
        n = num(m.group(1))
        k = m.group(2)

        def cond(g, ctx, n=n, k=k):
            o = g.s.objs.get(ctx.get("source"))
            if o is None or o.zone != "battlefield":
                lo = g.s.lki_obj.get(ctx.get("source"))
                return lo is not None and lo.counters.get(k, 0) >= n
            return o.counters.get(k, 0) >= n
        return cond
    if t in ("it isn't a token", "~ isn't a token"):
        # CR 603.4, 603.10a: the dying object as it last existed.
        from ..engine.effects import event_obj_chars
        return lambda g, ctx: not getattr(event_obj_chars(g, ctx)[1], "token", True)
    if t == "you cast it":
        # CR 603.4, 608.2h: rechecked on resolution from the permanent as it last existed if it
        # has left the battlefield.
        return lambda g, ctx: bool(getattr(_source_obj(g, ctx), "cast", None))
    for h in CONDITION_HOOKS:
        r = h(t, cx)
        if r is not None:
            return r
    return None


# =========================================================================== card support

def trigger_parser(fn):
    TRIGGER_HOOKS.append(fn)
    return fn


@_nc
class EntersOrDies(Trigger):
    """"When ~ enters or dies": one ability with two trigger events. Its dies part looks back in
    time (CR 603.10a), its enters part looks at the battlefield after the event (603.6a)."""
    events = ("zone",)
    lookback = False

    def __init__(self):
        self.zones = ("battlefield",)
        self._enters = Enters(self_=True)
        self._dies = Dies(self_=True)

    def lookback_for(self, ev):
        return ev.get("from") == "battlefield" and ev.get("to") != "battlefield"

    def match(self, g, ev, src, c, ctrl):
        if self.lookback_for(ev):
            return self._dies.match(g, ev, src, c, ctrl)
        return self._enters.match(g, ev, src, c, ctrl)


@trigger_parser
def _enters_or_dies(line, cx):
    m = re.match(r"^When(?:ever)? ~ enters or dies, (.+)$", line)
    if m:
        return EntersOrDies(), m.group(1)
    return None


@trigger_parser
def _another_leaves(line, cx):
    """"Whenever another permanent leaves the battlefield" (CR 603.6c, 603.10a: looks back)."""
    m = re.match(r"^Whenever another (permanent|creature|artifact|nontoken creature) leaves the battlefield, (.+)$", line)
    if not m:
        return None
    f = Filter(permanent=True, text="permanent") if m.group(1) == "permanent" else parse_noun(m.group(1))
    f.other = True
    d = Dies(filt=f, to=None)
    return d, m.group(2)


def condition_parser(fn):
    CONDITION_HOOKS.append(fn)
    return fn


def _source_obj(g, ctx):
    """The source permanent, or the object it was as it last existed (CR 608.2h)."""
    src = ctx.get("source")
    return g.s.objs.get(src) or g.s.lki_obj.get(src)


@condition_parser
def _cast_conditions(t, cx):
    if t in ("you cast him", "you cast her", "~ was cast", "it was cast"):
        # CR 603.4: the permanent was cast (it entered from the stack as a resolving spell);
        # "you cast him/her" is "you cast it" for a legendary character (CR 201.5c).
        return lambda g, ctx: bool(getattr(_source_obj(g, ctx), "cast", None))
    m = re.fullmatch(r"((?:\{[WUBRG]\})+) was spent to cast (?:it|~)", t)
    if m:
        # CR 601.2h, 106.1: at least that much mana of each color was spent on the total cost
        # (Deceit rulings 2025-11-17: the whole cost, not only the hybrid symbols).
        need = {}
        for c in re.findall(r"\{([WUBRG])\}", m.group(1)):
            need[c] = need.get(c, 0) + 1

        def cond(g, ctx, need=need):
            o = _source_obj(g, ctx)
            spent = (getattr(o, "mana_spent", None) or {}) if o is not None and getattr(o, "cast", None) else {}
            return all(spent.get(c, 0) >= n for c, n in need.items())
        return cond
    m = re.fullmatch(r"(\w+) or more mana was spent to cast that spell", t)
    if m:
        n = num(m.group(1))
        from ..engine.effects import spell_mana_spent
        return lambda g, ctx, n=n: spell_mana_spent(g, ctx) >= n
    return None


@_nc
class SearchesLibrary(Trigger):
    """"Whenever an opponent searches their library" (CR 701.23; Wan Shi Tong rulings
    2025-10-02: not when you search an opponent's library, nor when an opponent searches
    another player's)."""
    events = ("search",)

    def __init__(self, who="opp"):
        self.who = who
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        p = ev.get("player")
        if ev.get("library") != p:
            return None
        if (self.who == "opp") == (p == ctrl):
            return None
        return {"player": p}


def _most_life(g, p):
    """CR 103.4-style comparisons: p has the most life or is tied for most life."""
    return g.s.players[p].life >= max(pl.life for pl in g.s.players)


@_nc
class AttacksCond(Trigger):
    """"Whenever ~ attacks the player with the most life or tied for most life" / "... attacks
    while you have the most life or are tied for most life": part of the trigger event, checked
    only as it attacks (CR 603.2, 508.1m; Preacher of the Schism rulings 2023-11-10)."""
    events = ("attacks",)

    def __init__(self, kind):
        self.kind = kind
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev["obj"] != src:
            return None
        d = ev["defender"]
        if self.kind == "defender_most_life":
            if not (isinstance(d, tuple) and d[0] == "player" and _most_life(g, d[1])):
                return None
        elif self.kind == "you_most_life":
            if not _most_life(g, ctrl):
                return None
        return {"obj": src, "defender": d}


@_nc
class DealsDamage(Trigger):
    """"Whenever ~ deals damage": once per event for all the damage it dealt at the same time
    (CR 603.2c; 510.2 combat damage is simultaneous), data n = that much."""
    events = ("source_dealt_damage",)

    def __init__(self):
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev.get("source") != src or ev.get("n", 0) <= 0:
            return None
        return {"n": ev["n"], "obj": src}


@_nc
class YouAttackWith(Trigger):
    """"Whenever you attack with one or more Lizards" (CR 508.1m: once per declaration)."""
    events = ("attackers_declared",)

    def __init__(self, filt):
        self.filt = filt
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev["player"] != ctrl:
            return None
        if not any(a in g.s.objs and self.filt.match(g, a, src, {"controller": ctrl}) for a in ev.get("attackers", ())):
            return None
        return {"player": ctrl}


@trigger_parser
def _audit_u_triggers(line, cx):
    m = re.match(r"^Whenever an opponent searches their library, (.+)$", line)
    if m:
        return SearchesLibrary("opp"), m.group(1)
    m = re.match(r"^Whenever ~ attacks the player with the most life or tied for most life, (.+)$", line)
    if m:
        return AttacksCond("defender_most_life"), m.group(1)
    m = re.match(r"^Whenever ~ attacks while you have the most life or are tied for most life, (.+)$", line)
    if m:
        return AttacksCond("you_most_life"), m.group(1)
    m = re.match(r"^Whenever ~ deals damage, (.+)$", line)
    if m:
        return DealsDamage(), m.group(1)
    m = re.match(r"^Whenever you attack with one or more (.+?), (.+)$", line)
    if m:
        return YouAttackWith(parse_noun(m.group(1))), m.group(2)
    m = re.match(r"^When ~ enter, (.+)$", line)
    if m:
        # A plural name ("The Sackville-Bagginses enter"): the same enters trigger.
        return Enters(self_=True), m.group(1)
    m = re.match(r"^When ~ enters and at the beginning of your upkeep, (.+)$", line)
    if m:
        # One ability with two trigger events (CR 603.2).
        return AnyOf([Enters(self_=True), Step("Upkeep", "you")]), m.group(1)
    return None


@condition_parser
def _life_conditions(t, cx):
    if t == "your life total is less than or equal to half your starting life total":
        # CR 103.4: the starting life total is 20 in a two-player game; half is 10.
        return lambda g, ctx: g.s.players[ctx["controller"]].life <= 10
    m = re.fullmatch(r"you've gained (\d+) or more life this turn", t)
    if m:
        n = int(m.group(1))
        return lambda g, ctx, n=n: g.s.players[ctx["controller"]].life_gained >= n
    return None


@_nc
class CombatDamagePlayerOrPW(Trigger):
    """"Whenever a creature you control deals combat damage to a player or planeswalker" (CR
    510.2; one trigger per creature and recipient)."""
    events = ("damage",)

    def __init__(self, filt):
        self.filt = filt
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if not ev.get("combat"):
            return None
        t = ev["target"]
        if isinstance(t, tuple):
            if t[0] != "player":
                return None
        else:
            tc = g.chars(t) if t in g.s.objs else g.s.lki.get(t)
            if tc is None or "Planeswalker" not in tc.types:
                return None
        sc = ev.get("source_chars")
        if sc is None or not self.filt.match_chars(g, sc, g.s.objs.get(ev["source"]), src, {"controller": ctrl}):
            return None
        return {"n": ev["n"], "obj": ev["source"], "player": t[1] if isinstance(t, tuple) else None}


@trigger_parser
def _combat_damage_player_or_pw(line, cx):
    m = re.match(r"^Whenever (a|an) (.+?) deals combat damage to a player or planeswalker, (.+)$", line)
    if m:
        return CombatDamagePlayerOrPW(parse_noun(m.group(2))), m.group(3)
    return None


@_nc
class CastSelf(Trigger):
    """"When you cast this spell" (CR 601.2i; the ability functions on the stack, 113.6c)."""
    events = ("cast",)

    def __init__(self):
        self.zones = ("stack",)

    def match(self, g, ev, src, c, ctrl):
        if ev.get("obj") != src or ev.get("player") != ctrl:
            return None
        return {"obj": src, "player": ctrl, "item": ev.get("item")}


@trigger_parser
def _cast_self(line, cx):
    m = re.match(r"^When you cast ~, (.+)$", line)
    if m:
        return CastSelf(), m.group(1)
    return None


@condition_parser
def _spells_cast_conditions(t, cx):
    if t == "you've cast another spell this turn":
        # The spell itself is counted once it's cast (CR 601.2i): another one makes two.
        return lambda g, ctx: len(g.s.players[ctx["controller"]].spells_cast) >= 2
    return None
