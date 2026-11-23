"""Card-support round 2: combat restrictions, Equipment and
attach effects, static P/T and characteristic-defining abilities, ward costs, planeswalkers.

Registered through the hook registries (compiler.py KEYWORD_HOOKS / LINE_HOOKS / POST_HOOKS,
triggers.py TRIGGER_HOOKS / CONDITION_HOOKS, statics.py EXTRA_STATICS, effects_text.py
PATTERNS / MERGE_PAIRS); imported after batch9b. Engine pieces outside this file are listed
where they are used. Strict like the rest of the compiler: a sentence these patterns don't
match exactly still raises Unsupported.
"""
import re

from ..engine import effects as fx
from ..engine.effects import Filter, E
from ..engine.ir import (Keyword, Triggered, Static, RulePart, LayerPart, ReplacementPart, TargetSpec, Seq)
from ..engine.decisions import Decision, Opt
from .. import Unsupported
from . import compiler as C
from . import triggers as tg
from . import effects_text as et
from . import statics as st
from .nouns import parse_noun, num


# =============================================================================== blocking restrictions

def _power_of(g, oid):
    c = g.chars(oid)
    return c.power if c.power is not None else 0


@st.static_parser
def _cant_be_blocked_by_power(l, cx):
    """"Creatures with power less than ~'s power can't block it." (Elusive Otter) and "~ can't be
    blocked by creatures with power N or less." (Sandman): restrictions on blocks (CR 509.1b),
    checked only as blockers are declared (Elusive Otter ruling 2023-09-01, Sandman ruling
    2025-09-19: a later power change doesn't undo the block)."""
    if l == "Creatures with power less than ~'s power can't block it.":
        def filt(g, b, a):
            return _power_of(g, b) < _power_of(g, a)
        return [Static([RulePart("cant_be_blocked_by", "self", filter=filt)], text=l)]
    m = re.fullmatch(r"~ can't be blocked by creatures with power (\d+) or (less|greater)\.", l)
    if m:
        n, less = int(m.group(1)), m.group(2) == "less"

        def filt(g, b, a, n=n, less=less):
            pw = _power_of(g, b)
            return pw <= n if less else pw >= n
        return [Static([RulePart("cant_be_blocked_by", "self", filter=filt)], text=l)]
    return None


# =============================================================================== granted abilities, conditional keywords

def _monocolored(g, c):
    return len(c.colors) == 1  # CR 105.4a? "monocolored": exactly one color (105.2a/105.4)


def granted_ability(k):
    """One granted ability of an "and has ..." list: a plain keyword, "ward {N}" (CR 702.21a, a
    triggered ability) or "hexproof from monocolored" (702.11d: can't be the target of monocolored
    spells or abilities an opponent controls)."""
    k = k.strip()
    if re.fullmatch(st.KWS, k):
        return Keyword(k)
    m = re.fullmatch(r"ward (\{[^}]+\}(?:\{[^}]+\})*)", k)
    if m:
        from ..engine.mana import parse_cost
        return C.ward_ability(parse_cost(m.group(1).upper()), 0)
    if k == "hexproof from monocolored":
        return Keyword("hexproof from", value=_monocolored, text=k)
    raise Unsupported(f"granted ability {k!r}")


@st.static_parser
def _attached_grants(l, cx):
    """"Equipped creature gets +1/+0 and has haste and ward {1}." / "Enchanted creature gets +1/+0
    and has lifelink and ward {2}." / "Equipped creature gets +2/+2 and has hexproof from
    monocolored.": layer 7c and layer 6 (CR 613.4c, 613.1f) for the attached creature."""
    m = re.fullmatch(r"(Enchanted|Equipped) creature gets \+(\d+)/\+(\d+) and has (.+?)\.", l)
    if not m or not re.search(r"ward \{|hexproof from monocolored", m.group(4)):
        return None
    abil = [granted_ability(k) for k in re.split(r",? and |, ", m.group(4))]
    p, t = int(m.group(2)), int(m.group(3))

    def pt(g, oid, c, src, p=p, t=t):
        if c.power is not None:
            c.power += p
            c.toughness += t

    def grant(g, oid, c, src, abil=tuple(abil)):
        c.abilities.extend(abil)
    return [Static([LayerPart("7c", "attached", pt, l), LayerPart(6, "attached", grant, l)], text=l)]


@st.static_parser
def _has_kw_as_long_as_you_control(l, cx):
    """"This creature has lifelink as long as you control another Dwarf." A static ability whose
    effect applies only while its condition is true (CR 611.3a), checked continuously; layer 6."""
    m = re.fullmatch(r"~ has " + st.KWS + r" as long as you control (another|an?) (.+?)\.", l)
    if not m:
        return None
    k = Keyword(m.group(1))
    f = parse_noun(("another " if m.group(2) == "another" else "") + m.group(3) + " you control")

    def cond(g, src, f=f):
        p = g.ctrl(src)
        return any(o != src and g.ctrl(o) == p and f.match(g, o, src, {"controller": p}) for o in g.s.battlefield)

    def grant(g, oid, c, src, k=k):
        c.abilities.append(k)
    return [Static([LayerPart(6, "self", grant, l)], text=l, condition=cond)]


@st.static_parser
def _enters_tapped_unless_you_control_a(l, cx):
    """"This land enters tapped unless you control an Equipment." (CR 614.1c; the permanents you
    control as it would enter, 614.12)."""
    m = re.fullmatch(r"~ enters tapped unless you control an? (Equipment|Food|Dwarf)\.", l)
    if not m:
        return None
    f = parse_noun(m.group(1))

    def cond(g, ev, f=f):
        p = ev["opts"].get("controller", ev.get("owner"))
        return any(g.ctrl(o) == p and f.match(g, o, None, {"controller": p}) for o in st.already_there(g, ev))
    return [st._enter_tapped(cond, text=l)]


@st.static_parser
def _karn_entering(l, cx):
    """Karn, Argent Defender: "Artifacts and creatures entering the battlefield don't cause abilities
    to trigger." The same rule as Doorkeeper Thrull's (statics._entering_doesnt_trigger); rulings
    2026-08-21: replacement effects still apply, the permanent is looked at as it exists on the
    battlefield, and one entering with Karn doesn't trigger abilities either."""
    if l == "Artifacts and creatures entering the battlefield don't cause abilities to trigger.":
        return st._entering_doesnt_trigger("Artifacts and creatures entering don't cause abilities to trigger.", cx)
    return None


# =============================================================================== Chainsaw

class CreaturesDie(tg.Trigger):
    """"Whenever one or more creatures die": once per event, however many creatures died at the
    same time (CR 603.2c); a leaves-the-battlefield ability, so it looks back (603.10a) and
    triggers even if its source died at the same time. One event = one batch of simultaneous
    zone changes (zones.move_batch; they share the battlefield snapshot `bf_lki`)."""
    events = ("zone",)
    lookback = True

    def __init__(self):
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev.get("from") != "battlefield" or ev.get("to") != "graveyard":
            return None
        lki = ev.get("lki")
        if lki is None or "Creature" not in lki.types:
            return None
        batch = ev.get("bf_lki")
        for t in g.s.pending:
            if t.source == src and getattr(t.ability, "trigger", None) is self and t.event is not ev \
                    and isinstance(t.event, dict) and t.event.get("bf_lki") is batch and batch is not None:
                return None  # this batch already triggered it
        return {"obj": ev.get("new"), "old": ev["old"], "lki": lki}


tg._nc(CreaturesDie)  # immutable card data: state clones share it


class CreatureOrCreatureSpellBecomesTarget(tg.Trigger):
    """Surrak, Elusive Hunter: "Whenever a creature you control or a creature spell you control
    becomes the target of a spell or ability an opponent controls" (CR 603.2e: once per spell or
    ability, even if it targets the object more than once; 115.10: a spell on the stack can be a
    target). The object's characteristics as it becomes the target."""
    events = ("targeted",)

    def __init__(self):
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        r = ev["target"]
        if isinstance(r, tuple) or ev["controller"] == ctrl or r not in g.s.objs:
            return None
        o = g.s.objs[r]
        if o.zone not in ("battlefield", "stack"):
            return None
        ch = g.chars(r)
        if "Creature" not in ch.types or ch.controller != ctrl:
            return None
        k = ("became_target_seen", src, ev["item"], r)
        if g.s.turn_flags.get(k):
            return None
        g.s.turn_flags[k] = True
        return {"obj": r, "item": ev["item"], "player": ev["controller"]}


tg._nc(CreatureOrCreatureSpellBecomesTarget)


def _trigger_hook(line, cx):
    m = re.match(r"^Whenever one or more creatures die, (.+)$", line)
    if m:
        return CreaturesDie(), m.group(1)
    m = re.match(r"^Whenever a creature you control or a creature spell you control becomes the target of a spell or ability an opponent controls, (.+)$", line)
    if m:
        return CreatureOrCreatureSpellBecomesTarget(), m.group(1)
    return None


tg.TRIGGER_HOOKS.append(_trigger_hook)


@st.static_parser
def _equipped_gets_x_counters(l, cx):
    """Chainsaw: "Equipped creature gets +X/+0, where X is the number of rev counters on this
    Equipment." (layer 7c, X counted continuously, CR 613.4c)."""
    m = re.fullmatch(r"Equipped creature gets \+X/\+0, where X is the number of (\w+) counters on ~\.", l)
    if not m:
        return None
    kind = m.group(1)

    def fn(g, oid, c, src, kind=kind):
        o = g.s.objs.get(src)
        if c.power is not None and o is not None:
            c.power += o.counters.get(kind, 0)
    return [Static([LayerPart("7c", "attached", fn, l)], text=l)]


def _it_deals(m, sc):
    """"When this Equipment enters, it deals 3 damage to up to one target creature.": "it" is the
    source itself (the only object the ability mentions)."""
    tr = sc.trigger
    if not (isinstance(tr, tg.Enters) and getattr(tr, "self_", False)):
        raise Unsupported(f"'it deals' outside a self enters trigger: {m.group(0)!r}")
    sel = et.target_noun(m.group(2), sc)
    return fx.Damage(int(m.group(1)), sel, text=m.group(0))


et.PATTERNS[0:0] = [
    # Only "up to one target creature": effects_text's own "It deals N damage to target ..."
    # (Mouser Foundry's sacrifice ability, round2_c2) isn't shadowed (round 2 merge).
    (r"It deals (\d+) damage to (up to one target creature)\.", _it_deals),
    (r"Put an? (rev) counter on ~\.", lambda m, sc: fx.PutCounters(fx.self_permanent(), m.group(1), 1, text=m.group(0))),
]


# =============================================================================== converge, amounts

def _colors_spent(g, ctx):
    """CR 202.2, 601.2h: the colors of mana spent to cast the spell (converge, 207.2c). A copy, or
    a spell cast without spending mana, had none spent (Together as One rulings 2026-03-20)."""
    src = ctx.get("source")
    o = g.s.objs.get(src) or g.s.lki_obj.get(src)
    if o is None or getattr(o, "is_copy", False):
        return 0
    spent = getattr(o, "mana_spent", None) or {}
    return sum(1 for c in "WUBRG" if spent.get(c, 0) > 0)


def _amount_hook(t, sc):
    if t == "the number of colors of mana spent to cast ~":
        if not sc.cx.is_spell:
            raise Unsupported(f"amount {t!r} on a permanent")
        return fx.Amount(_colors_spent, t)
    return None


et.AMOUNT_HOOKS.append(_amount_hook)


def _draws_deals_gains(m, sc):
    """Together as One: "Target player draws X cards, ~ deals X damage to any target, and you gain
    X life." Three instructions in order (608.2c), two target words (115.1)."""
    who = et.target_noun("target player", sc)
    n1 = et.amount_of(m.group(1), sc)
    to = et.target_noun("any target", sc)
    n2 = et.amount_of(m.group(2), sc)
    n3 = et.amount_of(m.group(3), sc)
    return Seq(fx.Draw(n1, who=who, text=m.group(0)), fx.Damage(n2, to, text=m.group(0)), fx.GainLife(n3, text=m.group(0)))


et.PATTERNS[0:0] = [
    (r"Target player draws (X|\d+) cards, ~ deals (X|\d+) damage to any target, and you gain (X|\d+) life\.", _draws_deals_gains),
    # "Target player discards X cards." (Arcane Omens; CR 701.9b: that player chooses).
    (r"(Target opponent|Each opponent|Target player|Each player) discards? (X) cards\.", et._discard),
]


# =============================================================================== Gandalf, Goblins' Bane // Flameshape

class LookExileFaceDownMayPlayIf(E):
    """Flameshape: "Look at the top two cards of your library and exile them face down. For as long
    as they remain exiled, you may play them if you control a Wizard." The player who looked may go
    on looking at them (CR 406.3) and play them (406.3a: turned face up just before), following the
    normal timing and costs (ruling 2026-06-29); the Wizard is checked as a card is played (ruling:
    losing it afterwards doesn't stop the spell)."""

    def __init__(self, n, cond, text=""):
        self.n = n
        self.cond = cond
        self.text = text

    def run(self, g, ctx):
        s = g.s
        p = ctx["controller"]
        lib = s.players[p].library
        k = min(self.n, len(lib))
        if k <= 0:
            return
        g.top_hook(p, k, "exile")  # harness: the cards the EUT exiled go on top
        ids = list(lib[:k])
        res = g.move_batch([(o, "exile", {"face_down": True, "cause": "effect"}) for o in ids])
        for r in res:
            if r is not None and r in s.objs:
                s.objs[r].flags["may_play"] = {"player": p, "until": None, "any_type": False, "lands": True,
                                               "if_control": self.cond}
        s.touch()


et.PATTERNS[0:0] = [
    (r"~ gets \+(\d+)/\+(\d+) until end of turn and deals (\d+) damage to each opponent\.",
     lambda m, sc: Seq(fx.Pump(fx.self_permanent(), int(m.group(1)), int(m.group(2)), text=m.group(0)),
                       fx.Damage(int(m.group(3)), fx.opponent(), text=m.group(0)))),
    (r"Look at the top two cards of your library and exile them face down\. For as long as they remain exiled, you may play them if you control an? (Wizard)\.",
     lambda m, sc: LookExileFaceDownMayPlayIf(2, parse_noun(m.group(1)), text=m.group(0))),
]
et.MERGE_PAIRS.append((r"Look at the top two cards of your library and exile them face down\.$",
                       r"^For as long as they remain exiled, you may play them if you control an? \w+\.$"))


# =============================================================================== characteristic-defining P/T

def _lands_you_control(g, oid, c):
    """Lands controlled by the object's controller; off the battlefield and the stack, its owner
    (CR 108.4a, 109.5: an object not on the battlefield or stack has no controller; "you" is its
    owner)."""
    o = g.s.objs.get(oid)
    if o is not None and o.zone in ("battlefield", "stack"):
        p = c.controller
    else:
        p = o.owner if o is not None else c.controller
    return sum(1 for x in g.s.battlefield if g.ctrl(x) == p and "Land" in g.chars(x).types)


@st.static_parser
def _cda_lands(l, cx):
    """"~'s power is equal to the number of lands you control." (Lumbering Worldwagon) / "~'s power
    and toughness are each equal to the number of lands you control." (Sandman): characteristic-
    defining abilities, layer 7a, in all zones (CR 604.3, 613.4a; Sandman ruling 2025-09-19)."""
    m = re.fullmatch(r"~'s (power|power and toughness) (?:is|are each) equal to the number of lands you control\.", l)
    if not m:
        return None
    both = m.group(1) != "power"

    def fn(g, oid, c, src, both=both):
        n = _lands_you_control(g, oid, c)
        c.power = n
        if both:
            c.toughness = n
    s_ = Static([LayerPart("7a", "self", fn, l)], text=l, zones=("all",))
    s_.cda = True
    return [s_]


def _return_self_and_land(line, cx):
    """Sandman: "{3}{G}{G}: Return this card and target land card from your graveyard to the
    battlefield tapped." It moves its own card out of the graveyard, so it functions only there
    (CR 113.6m); both enter at the same time. If the land card target is illegal as it resolves,
    the ability doesn't resolve and Sandman stays (608.2b)."""
    m = re.fullmatch(r"((?:\{[^}]+\})+): Return ~ and target land card from your graveyard to the battlefield tapped\.", line)
    if not m:
        return None
    from ..engine.mana import parse_cost
    from ..engine.ir import Activated, Cost
    f = Filter(types=["Land"], owner="you", text="land card from your graveyard")
    spec = TargetSpec("card", filt=f, zone="graveyard", text="target land card from your graveyard")
    both = fx.Sel(lambda g, ctx: fx.self_sel().get(g, ctx) + fx.target(0).get(g, ctx), "~ and target")
    eff = fx.PutOntoBattlefield(both, from_zones=("graveyard",), controller="you", tapped=True, text=line)
    return [Activated(Cost(mana=parse_cost(m.group(1))), eff, [spec], text=line, zones=("graveyard",))]


C.LINE_HOOKS.append(_return_self_and_land)


def _activate_only_if_n_lands(line, cx):
    """Cryptic Caves: "{1}, {T}, Sacrifice this land: Draw a card. Activate only if you control five
    or more lands." A restriction checked as the ability is activated (CR 602.5b); the Caves itself
    counts, and sacrificing it as a cost doesn't undo the activation (rulings 2019-07-12)."""
    m = re.fullmatch(r"(.+?) Activate only if you control (\w+) or more (lands|creatures|artifacts)\.", line)
    if not m or ": " not in m.group(1):
        return None
    n = num(m.group(2))
    t = {"lands": "Land", "creatures": "Creature", "artifacts": "Artifact"}[m.group(3)]
    abil = C.compile_line(m.group(1), cx)
    acts = [a for a in abil if a.kind == "activated"]
    if len(acts) != 1 or acts[0].condition is not None:
        raise Unsupported(f"activation condition in {line!r}")
    acts[0].condition = lambda g, oid, p, n=n, t=t: sum(1 for x in g.s.battlefield if g.ctrl(x) == p and t in g.chars(x).types) >= n
    return abil


C.LINE_HOOKS.append(_activate_only_if_n_lands)


def _counters_for_each(m, sc):
    """"Put a +1/+1 counter on target creature for each land you control." (the count as the
    ability resolves, 608.2h)."""
    sel = et.obj_ref(m.group(2), sc)
    sc.last = sel
    n = fx.count_amount(parse_noun(m.group(3)))
    return fx.PutCounters(sel, m.group(1), n, text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Put an? ([+-]1/[+-]1) counter on (target creature|target creature you control|~) for each (land you control|creature you control|artifact you control)\.", _counters_for_each),
]


# =============================================================================== amass (Azog)

def amass(g, ctx, p, subtype, n):
    """CR 701.47a: "If you don't control an Army creature, create a 0/0 black [subtype] Army
    creature token. Choose an Army creature you control. Put N +1/+1 counters on that creature. If
    it isn't a [subtype], it becomes a [subtype] in addition to its other types." (The token is
    0/0 as it enters, Azog ruling 2026-06-29; 701.47b: amassed even if some steps did nothing.)"""
    from .model import token_def
    armies = [o for o in g.s.battlefield if g.ctrl(o) == p and "Creature" in g.chars(o).types and "Army" in g.chars(o).subtypes]
    if armies and getattr(g.s, "patch_a4_amass", False):
        # Opt-in alignment for lockstep runs (State.patch_a4_amass; off by default): amass doesn't
        # count a creature that is an Army only through changeling. By default it does (CR 701.47a,
        # 702.73a).
        real = [o for o in armies if not (g.chars(o).has_kw("changeling") and "Army" not in g.base_chars(g.s.objs[o]).subtypes)]
        if len(real) != len(armies):
            hook = getattr(getattr(g, "hooks", None), "patch_hit", None)
            if hook is not None:
                hook("patch:a4-amass-follow")
            armies = real
    if not armies:
        armies = g.create_token(token_def(None, ("Creature",), (subtype, "Army"), ("B",), 0, 0), p, 1)
        armies = [o for o in armies if o in g.s.objs and g.s.objs[o].zone == "battlefield"]
        if not armies:
            return None
    if len(armies) == 1:
        a = armies[0]
    else:
        opts = [Opt(f"choose:{g.ref(o)}", o, sig=("army", g.obj_sig(o))) for o in armies]
        a = g.ask(Decision(p, "ChooseCard", opts, prompt="Choose an Army creature you control (amass)."))
    if n > 0:
        g.add_counters(a, "+1/+1", n, source=ctx.get("source"))
    if a in g.s.objs and subtype not in g.chars(a).subtypes:
        fx.AddEffect(fx.Sel(lambda g_, c_, a=a: [a], "amassed Army"),
                     {4: (lambda g_, oid, c, ce, sub=subtype: c.subtypes.add(sub))}, duration="forever",
                     text=f"becomes a {subtype}").run(g, ctx)
    return a


class AzogEnters(E):
    """Azog, Moria's Ruin: "destroy up to one other target creature. Its controller amasses Goblins
    X, where X is that creature's power. If you controlled that creature, draw a card." With no
    target chosen, "its controller" is undefined: no amass, no draw (ruling 2026-06-29); an
    illegal target makes the ability not resolve (608.2b, resolve_ability). X is the creature's
    power as it last existed on the battlefield (ruling), or its current power if it wasn't
    destroyed (608.2h)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        tl = (ctx.get("legal") or [[]])[0]
        if not tl:
            return
        t = tl[0]
        c0 = g.chars(t)
        ctrl0 = c0.controller
        g.destroy(t)
        o = g.s.objs.get(t)
        if o is not None and o.zone == "battlefield":
            c = g.chars(t)
        else:
            c = g.s.lki.get(t) or c0
        ctrl = c.controller if c is not None else ctrl0
        x = max(0, c.power or 0) if c is not None and c.power is not None else 0
        amass(g, ctx, ctrl, "Goblin", x)
        if ctrl == ctx["controller"]:
            g.draw(ctx["controller"], 1)


def _azog(m, sc):
    et.target_noun("up to one other target creature", sc)
    return AzogEnters(text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Destroy up to one other target creature\. Its controller amasses Goblins X, where X is that creature's power\. If you controlled that creature, draw a card\.", _azog),
]
et.MERGE_PAIRS.extend([
    (r"^[Dd]estroy up to one other target creature\.$", r"^Its controller amasses Goblins X, where X is that creature's power\.$"),
    (r"Its controller amasses Goblins X, where X is that creature's power\.$", r"^If you controlled that creature, draw a card\.$"),
])


# "Untap another target permanent." (Formidable Speaker; "another": not the source, CR 115.1).
et.PATTERNS[0:0] = [
    (r"(Tap|Untap) (another target (?:permanent|creature|nonland permanent|artifact|land))\.", et._tap),
]


# =============================================================================== The End

@st.static_parser
def _self_cost_if_life(l, cx):
    """"This spell costs {2} less to cast if your life total is 5 or less." (CR 601.2f: generic
    mana only; it changes neither the mana cost nor the mana value, The End ruling 2023-09-01).
    Checked as the total cost is determined."""
    m = re.fullmatch(r"~ costs \{(\d+)\} less to cast if your life total is (\d+) or less\.", l)
    if not m:
        return None
    k, life = int(m.group(1)), int(m.group(2))
    part = RulePart("self_cost_mod", "self", n=lambda g, oid, p, k=k, life=life: -k if g.s.players[p].life <= life else 0)
    return [Static([part], text=l, zones=("all",))]


class ExileSearchSameName(E):
    """The End: "Exile target creature or planeswalker. Search its controller's graveyard, hand, and
    library for any number of cards with the same name as that permanent and exile them. That
    player shuffles, then draws a card for each card exiled from their hand this way."
    The name is the permanent's as it last existed on the battlefield (copy exceptions included,
    707.9b; a back face's name finds nothing, ruling 2023-09-01: cards in those zones have their
    front-face names). "Any number": the searcher may find fewer (701.23b), one card at a time,
    `done` to stop (as the EUT asks it)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        s = g.s
        legal = (ctx.get("legal") or [[]])[0]
        if not legal:
            return
        t = legal[0]
        c = g.chars(t)
        name, p = c.name, c.controller
        g.move(t, "exile", cause="effect")
        me = ctx["controller"]
        g.emit({"type": "search", "player": me, "library": p})  # CR 701.23
        pl = s.players[p]
        g.s.know.looks(g, me, list(pl.hand))  # 701.23a: the searched hand is looked at too
        found = []
        # One zone at a time, graveyard, hand, then library, each a set of picks ended by `done`
        # (the EUT's presentation; the same choices as one combined search).
        for z in ("graveyard", "hand", "library"):
            while True:
                cands = [o for o in getattr(pl, z) if o not in found and g.chars(o).name == name]
                if not cands:
                    break
                opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("srch", z, g.obj_sig(o))) for o in cands]
                a = g.ask(Decision(me, "Search", opts, prompt=f"Search the {z} for cards named {name} (or done)."))
                if a is None:
                    break
                found.append(a)
        from_hand = sum(1 for o in found if s.objs[o].zone == "hand")
        if found:
            g.move_batch([(o, "exile", {"cause": "effect"}) for o in found])
        g.shuffle(p)
        if from_hand:
            g.draw(p, from_hand)


def _the_end(m, sc):
    et.target_noun("target creature or planeswalker", sc)
    return ExileSearchSameName(text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Exile target creature or planeswalker\. Search its controller's graveyard, hand, and library for any number of cards with the same name as that permanent and exile them\. "
     r"That player shuffles, then draws a card for each card exiled from their hand this way\.", _the_end),
]
et.MERGE_PAIRS.extend([
    (r"^Exile target creature or planeswalker\.$", r"^Search its controller's graveyard, hand, and library for any number of cards with the same name as that permanent and exile them\.$"),
    (r"for any number of cards with the same name as that permanent and exile them\.$", r"^That player shuffles, then draws a card for each card exiled from their hand this way\.$"),
])


# =============================================================================== Braided Net // Braided Quipu, craft

@st.static_parser
def _enters_with_named_counters(l, cx):
    """"This artifact enters with three net counters on it." (CR 122.6, 614.1c: part of the event
    of entering)."""
    m = re.fullmatch(r"~ enters with (a|an|one|two|three|four|five) (net|charge|oil|quest|lore|page|time) counters? on it\.", l)
    if not m:
        return None
    n, kind = num(m.group(1)), m.group(2)

    def apply(g, ev, src, n=n, kind=kind):
        cs = ev["opts"].setdefault("counters", {})
        cs[kind] = cs.get(kind, 0) + n
    p = ReplacementPart("enter_self", lambda g, ev, src: True, apply, text=l)
    p.commutes = True
    return [Static([p], text=l)]


class _LockEndsOnUntap(tg.Trigger):
    """Ends Braided Net's lock for good when the locked permanent untaps or leaves the battlefield
    ("for as long as it remains tapped", CR 611.2b): it never triggers; it removes the effect and
    retires itself (its delayed entry is dropped at the next event)."""
    events = ("untapped", "zone")

    def __init__(self, oid, ce_id):
        self.oid = oid
        self.ce_id = ce_id

    def match(self, g, ev, src, c, ctrl):
        if (ev["type"] == "untapped" and ev.get("obj") == self.oid) or (ev["type"] == "zone" and ev.get("old") == self.oid):
            g.s.effects = [e for e in g.s.effects if e.id != self.ce_id]
            for d in g.s.delayed:
                if d.get("trigger") is self:
                    d["this_turn"] = -1  # check_triggers drops it (603.7b-style expiry)
            g.s.touch()
        return None


tg._nc(_LockEndsOnUntap)


class TapAndLock(E):
    """Braided Net: "Tap another target nonland permanent. Its activated abilities can't be
    activated for as long as it remains tapped." Mana abilities too, even ones whose cost untaps it
    (ruling 2023-11-10); an already tapped permanent is a legal target and gets locked (ruling). The
    effect ends when it untaps (611.2b); it doesn't start if the permanent isn't tapped."""

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        from ..engine.objects import ContEffect
        s = g.s
        for t in fx.objs_of(self.sel, g, ctx):
            if t not in s.objs or s.objs[t].zone != "battlefield":
                continue
            g.tap(t)
            if not s.objs[t].tapped:
                continue
            ce = ContEffect(s.new_id())
            ce.source = ctx.get("source")
            ce.controller = ctx["controller"]
            ce.timestamp = s.new_ts()
            ce.duration = "forever"
            ce.kind = "rule"
            ce.affected = [t]
            ce.data = {"rule": "cant_activate", "part": RulePart("cant_activate", "locked")}
            s.effects.append(ce)
            s.delayed.append({"trigger": _LockEndsOnUntap(t, ce.id), "ability": None, "controller": ctx["controller"],
                              "source": ctx.get("source"), "once": False, "data": {"obj": t}})
            s.touch()


def _tap_and_lock(m, sc):
    sel = et.target_noun("another target nonland permanent", sc)
    return TapAndLock(sel, text=m.group(0))


# Craft itself (CR 702.167a-b: the keyword, its materials and the transformed return) is
# ref/cards/round2_c2.py's `_craft` / `CraftMaterials` / `CraftReturn` (round 2 merge:
# one implementation; this module's `_craft_keyword` and `casting._material_cands` went).


class DrawPerArtifactThenThird(E):
    """Braided Quipu: "Draw a card for each artifact you control, then put this artifact into its
    owner's library third from the top." (CR 401.4: with two or fewer cards left it goes to the
    bottom, ruling 2023-11-10; back in the library its front face is up again, ruling 2023-11-10)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        n = sum(1 for x in g.s.battlefield if g.ctrl(x) == p and "Artifact" in g.chars(x).types)
        if n:
            g.draw(p, n)
        src = ctx.get("source")
        o = g.s.objs.get(src)
        if o is None or o.zone != "battlefield":
            return
        lib = g.s.players[o.owner].library
        pos = 2 if len(lib) >= 2 else "bottom"
        g.move(src, "library", pos=pos, cause="effect")


et.PATTERNS[0:0] = [
    (r"Tap another target nonland permanent\. Its activated abilities can't be activated for as long as it remains tapped\.", _tap_and_lock),
    (r"Draw a card for each artifact you control, then put ~ into its owner's library third from the top\.",
     lambda m, sc: DrawPerArtifactThenThird(text=m.group(0))),
]
et.MERGE_PAIRS.append((r"^Tap another target nonland permanent\.$", r"^Its activated abilities can't be activated for as long as it remains tapped\.$"))


# =============================================================================== Nowhere to Run

@st.static_parser
def _nowhere_to_run(l, cx):
    """"Creatures your opponents control can be the targets of spells and abilities as though they
    didn't have hexproof. Ward abilities of those creatures don't trigger." Hexproof and "hexproof
    from" are ignored for targeting (CR 702.11e; casting.can_target_obj); ward's trigger condition
    is checked as the creature becomes a target (triggers.WardTrigger), so a ward ability that
    already triggered resolves, and Nowhere to Run leaving later doesn't make ward trigger
    (rulings 2024-09-20)."""
    if l != ("Creatures your opponents control can be the targets of spells and abilities as though they didn't have hexproof. "
             "Ward abilities of those creatures don't trigger."):
        return None

    def opp_creature(g, oid, src):
        if not isinstance(src, int) or oid not in g.s.objs or g.s.objs[oid].zone != "battlefield":
            return False
        return "Creature" in g.chars(oid).types and g.ctrl(oid) != g.ctrl(src)
    return [Static([RulePart("targetable_despite_hexproof", opp_creature),
                    RulePart("ward_doesnt_trigger", opp_creature)], text=l)]


# =============================================================================== ward with a life cost

class CounterUnlessPaysLifeEqualPower(fx.CounterUnlessPays):
    """Raubahn: "Ward—Pay life equal to Raubahn's power." The power as the ward ability resolves,
    or as Raubahn last existed on the battlefield (ruling 2025-06-06; CR 608.2h). 0 life can
    always be paid (119.4 limits only payments greater than 0)."""

    def __init__(self, text=""):
        super().__init__([], 0, text=text)

    def run(self, g, ctx):
        c = fx.self_chars(g, ctx)
        n = max(0, c.power or 0) if c is not None and c.power is not None else 0
        fx.CounterUnlessPays([], n, text=f"{n} life").run(g, ctx)


def _ward_life_power(part, cx):
    if part.lower() != "ward—pay life equal to ~'s power":
        return None
    t = Triggered(tg.WardTrigger(by_opponent=True), CounterUnlessPaysLifeEqualPower(text=part), text="ward (life equal to power)")
    t.keyword = "ward"
    t.value = ([], "power")
    return [t]


C.KEYWORD_HOOKS.append(_ward_life_power)


# =============================================================================== activation cost reductions

def _cost_reduction_for_each(line, cx):
    """An activated ability ending in "This ability costs {1} less to activate for each Equipment
    you control." (The Lonely Mountain) or "Equip {4}. This ability costs {1} less to activate for
    each color of the creature it targets." (Dragonfire Blade): the reduction is part of the
    total cost, determined after targets are chosen (CR 602.2b, 601.2f), generic mana only."""
    m = re.fullmatch(r"(.+?) This ability costs \{1\} less to activate for each (Equipment you control|color of the creature it targets)\.( Activate only as a sorcery\.)?", line)
    if not m:
        return None
    head = m.group(1) + (m.group(3) or "")
    if head.startswith("Equip ") and re.fullmatch(r"Equip \{\d+\}\.", m.group(1)):
        abil = C.parse_keyword(m.group(1).rstrip("."), cx)
    else:
        abil = C.compile_line(head, cx)
    acts = [a for a in abil if a.kind == "activated"]
    if len(acts) != 1:
        raise Unsupported(f"cost reduction of {line!r}")
    ab = acts[0]
    if m.group(2) == "Equipment you control":
        f = parse_noun("Equipment you control")

        def n(g, oid, ab_, src, targets, f=f):
            p = g.ctrl(src)
            return -sum(1 for o in g.s.battlefield if g.ctrl(o) == p and f.match(g, o, src, {"controller": p}))

        def filt(g, oid, ab_, src, ab=ab):
            return oid == src and ab_ is ab
        part = RulePart("ability_cost_mod", "self", filter=filt, n=n)
    else:
        def may_apply(g, oid, ab_, src, ab=ab):
            return oid == src and ab_ is ab

        def filt_t(g, oid, ab_, src, targets, ab=ab):
            return may_apply(g, oid, ab_, src)

        def n(g, oid, ab_, src, targets):
            t = next((r for grp in targets for r in grp), None)
            if t is None or t not in g.s.objs:
                return 0
            return -len(g.chars(t).colors)
        part = RulePart("ability_cost_mod", "self", filter=lambda g, oid, ab_, src: False, filter_t=filt_t,
                        may_apply=may_apply, n=n)
    return abil + [Static([part], text=line)]


C.LINE_HOOKS.append(_cost_reduction_for_each)


# =============================================================================== divided effects (CR 601.2d)

def _division_amounts(g, chosen, total, done):
    """Amounts the next target (index len(done)) of a division may get. Each target gets at
    least one (CR 601.2d); the last one gets the rest. Interchangeable targets (identical
    objects, offered once) are divided non-increasingly in the order chosen, as the EUT does
    (DECISIONS.md "divided counters": a canonical order only, both orders give the same game)."""
    sigs = [g.obj_sig(r) if not isinstance(r, tuple) else r for r in chosen]
    i = len(done)
    left = total - sum(done)

    def cap(k, amounts):
        same = [amounts[j] for j in range(k) if sigs[j] == sigs[k]]
        return min(same) if same else None

    def max_rest(start, amounts):
        """Largest total targets start.. can take given amounts so far (greedy: each takes its
        cap; None = unbounded)."""
        a = list(amounts)
        tot = 0
        for k in range(start, len(chosen)):
            c = cap(k, a)
            if c is None:
                return None
            a.append(c)
            tot += c
        return tot
    out = []
    n_rest = len(chosen) - i - 1
    for v in range(1, left - n_rest + 1):
        c = cap(i, done)
        if c is not None and v > c:
            break
        rest = left - v
        mx = max_rest(i + 1, done + [v])
        if rest >= n_rest and (mx is None or rest <= mx):
            out.append(v)
    return out


def divide_hook(total):
    """spec.divide for a target word whose effect is divided among its targets (CR 601.2d): the
    division is announced after the targets are chosen, one amount per target in the order
    chosen but the last (ChooseX `x:n`, as the EUT asks it), and kept in ctx["division"][spec
    index]. It can't change later (115.7f)."""
    def ask(g, p, si, chosen, it):
        ctx = it.ctx
        n = total(g, ctx)
        if chosen and n < len(chosen):
            raise Unsupported("division with fewer than one per target (CR 601.2d)")
        done = []
        for k in range(len(chosen) - 1):
            vals = _division_amounts(g, chosen, n, done)
            if not vals:
                raise Unsupported("no legal division (CR 601.2d)")
            opts = [Opt(f"x:{v}", v) for v in vals]
            done.append(g.ask(Decision(p, "ChooseX", opts, subject=g.ref(chosen[k]) if not isinstance(chosen[k], tuple) else None,
                                       prompt=f"Divide {n}: amount for target {k + 1} (CR 601.2d).")))
        if chosen:
            done.append(n - sum(done))
        ctx.setdefault("division", {})[si] = list(done)
    return ask


class DistributeCounters(E):
    """"Distribute N <kind> counters among <targets>." (CR 601.2d: divided as the spell is cast;
    608.2b: the counters a target that became illegal would have got are lost, Grove's Bounty
    ruling 2023-09-01)."""

    def __init__(self, si, kind, text=""):
        self.si = si
        self.kind = kind
        self.text = text

    def run(self, g, ctx):
        if ctx.get("mode_targets") is not None:
            raise Unsupported("divided effect in a mode")  # target words are numbered per mode there
        orig = (ctx.get("targets") or [])[self.si] if self.si < len(ctx.get("targets") or []) else []
        legal = ctx.get("legal")
        ok = legal[self.si] if legal is not None and self.si < len(legal) else orig
        div = (ctx.get("division") or {}).get(self.si) or []
        put = 0
        for r, n in zip(orig, div):
            if r not in ok or isinstance(r, tuple):
                continue
            if r in g.s.objs and g.s.objs[r].zone == "battlefield" and n > 0:
                put += g.add_counters(r, self.kind, n, source=ctx.get("source")) or 0
        ctx.setdefault("vars", {})["did"] = put > 0


def _distribute(m, sc):
    amt = et.amount_of(m.group(1), sc)
    total = fx.amount(amt)
    kind = m.group(2)
    how, noun = m.group(3), m.group(4)
    f = parse_noun(noun)
    if how == "any number of":
        n = (0, lambda g, ctx, total=total: total(g, ctx))
    else:
        mm = re.fullmatch(r"one, two, or three|one or two", how)
        if not mm:
            raise Unsupported(f"distribute among {how!r}")
        n = (1, 3 if "three" in how else 2)
    spec = TargetSpec("object", filt=f, text=f"{how} target {noun}", n=n)
    spec.divide = divide_hook(total)
    sel = sc.tc.add(spec)
    sc.last = sel
    si = len(sc.tc.specs) - 1
    return DistributeCounters(si, kind, text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Distribute (X|two|three|four|five) ([+-]1/[+-]1) counters among (any number of|one, two, or three|one or two) target (.+?)\.", _distribute),
]


# =============================================================================== Food, milled cards (Bristlebud Farmer)

PERMANENT_TYPES = {"Artifact", "Battle", "Creature", "Enchantment", "Land", "Planeswalker"}


class PutMilledIntoHand(E):
    """"Mill N cards. You may put a <kind> card from among them into your hand." "Them" are the
    cards milled by this effect (CR 701.17a), in the graveyard or in the public zone a replacement
    effect put them into instead (701.17c: "An effect that refers to a milled card can find that card
    in the zone it moved to from the library, as long as that zone is a public zone"; exile under
    Rest in Peace, campaign17 1005950). The choice is offered only when one
    of them qualifies (608.2d: no impossible option); a permanent card is an artifact, battle,
    creature, enchantment, land or planeswalker card (Bristlebud Farmer ruling 2024-04-12). Asked
    as one Search, `done` | `choose:<card>` (`done` declines), the shape of Rapid Rescue's choice
    (round2_b2.MillMayReturnPermanent) and of the EUT's for all of these cards (CR 608.2d lets a "may" choice be one decision or a yes/no and then the card)."""

    def __init__(self, filt, text=""):
        self.filt = filt
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        milled = [o for o in (ctx.get("vars", {}).get("milled") or [])
                  if o in g.s.objs and g.s.objs[o].zone in ("graveyard", "exile") and not g.s.objs[o].face_down]
        cands = [o for o in milled if self.filt(g, o)]
        if not cands:
            return
        put = re.search(r"You may put .+? into your hand\.", self.text)
        opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("milled", g.obj_sig(o))) for o in cands]
        pick = g.ask(Decision(p, "Search", opts,
                              subject=g.ref(ctx["source"]) if ctx.get("source") in g.s.objs else None,
                              prompt=put.group(0) if put else self.text))
        if pick is not None:
            g.move(pick, "hand", cause="effect")


def _milled_card_filter(kind):
    if kind == "permanent":
        return lambda g, o: bool(PERMANENT_TYPES & g.chars(o).types)
    if kind in ("creature", "land", "artifact", "enchantment"):
        t = kind.capitalize()
        return lambda g, o, t=t: t in g.chars(o).types
    raise Unsupported(f"milled card kind {kind!r}")


def _mill_then_put(m, sc):
    n = et.amount_of(m.group(1), sc)
    return Seq(fx.Mill(n, text=f"Mill {m.group(1)} cards."),
               PutMilledIntoHand(_milled_card_filter(m.group(2)), text=m.group(0)))


et.PATTERNS[0:0] = [
    (r"Mill (two|three|four|five) cards\. You may put an? (permanent|creature|land|artifact|enchantment) card from among them into your hand\.", _mill_then_put),
    # "Sacrifice a Food." (an artifact subtype, CR 205.3g; any Food artifact, ruling 2024-11-08).
    (r"Sacrifice (a|an|another) (Food|Treasure|Clue|Equipment)\.", et._sacrifice_you),
]
et.MERGE_PAIRS.append((r"[Mm]ill (?:two|three|four|five) cards\.$", r"^You may put an? \w+ card from among them into your hand\.$"))


def _reveal_matching(f, what):
    """Dáin's Company: "You may reveal a Dwarf or Equipment card from among them and put it into
    your hand." (CR 701.20a: revealed, then put into the hand; the choice is made as the ability
    resolves, 608.2d; none qualifying: nothing to choose). Asked like Break Out's: Dig with `done`."""
    def reveal(g, ctx, p, top, chosen):
        cands = [o for o in top if f.match_chars(g, g.chars(o), g.s.objs[o], ctx.get("source"))]
        if not cands:
            return
        opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("dig", g.obj_sig(o))) for o in cands]
        a = g.ask(Decision(p, "Dig", opts, prompt=f"Reveal a {what} card from among them (or done)."))
        if a is None:
            return
        chosen.append(a)
        g.emit({"type": "reveal", "obj": a, "player": p})
        g.move(a, "hand")
    return reveal


def _destroy_artifact_enchantment_or_flyer(m, sc):
    """Vivien Reid: "Destroy target artifact, enchantment, or creature with flying." "With flying"
    qualifies only the last noun (an artifact or enchantment needn't fly); the generic noun parser
    would apply it to all three."""
    f = Filter(custom=lambda g, c, o, src, ctx: bool({"Artifact", "Enchantment"} & c.types)
               or ("Creature" in c.types and c.has_kw("flying")),
               text="artifact, enchantment, or creature with flying")
    sel = sc.tc.add(TargetSpec("object", filt=f, text="target artifact, enchantment, or creature with flying"))
    sc.last = sel
    return fx.Destroy(sel, text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Destroy target artifact, enchantment, or creature with flying\.", _destroy_artifact_enchantment_or_flyer),
]


def _look_reveal_put(m, sc):
    n = num(m.group(1))
    f = parse_noun(m.group(2))
    return fx.Dig(n, 0, order="random" if m.group(3) == "a random" else "any",
                  reveal_creature=_reveal_matching(f, m.group(2)), text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Look at the top (\w+) cards of your library\. You may reveal an? ((?:[A-Z][a-z]+|creature|land|artifact)(?: or (?:[A-Z][a-z]+|creature|land|artifact))?) card from among them and put it into your hand\. "
     r"Put the rest on the bottom of your library in (a random|any) order\.", _look_reveal_put),
]


# =============================================================================== planeswalkers' short names

def _short_planeswalker_name(line, cx):
    """CR 201.5c: a legendary planeswalker's text may call it by its first name ("Ajani" on Ajani
    Resolute, which is also its planeswalker type); those instances mean the card itself. A token
    "named Ajani's Pridemate" names another object and stays as it is."""
    if getattr(cx, "_d2_short", False):
        return None
    f = cx.face
    if "Planeswalker" not in cx.types or "Legendary" not in f.supertypes or "," in f.name or " " not in f.name:
        return None
    w = f.name.split()[0]
    if w not in f.subtypes:
        return None
    new = re.sub(r"(?<!named )\b" + re.escape(w) + r"\b", "~", line)
    if new == line:
        return None
    cx._d2_short = True
    try:
        return C.compile_line(new, cx)
    finally:
        cx._d2_short = False


C.LINE_HOOKS.append(_short_planeswalker_name)


# =============================================================================== attaching Equipment

def can_equip(g, eq, t):
    """Could Equipment eq become attached to t (CR 701.3a: otherwise it doesn't move)? t must be a
    creature on the battlefield (301.5); an Equipment that's also a creature can't equip one
    (301.5c; reconfigure isn't supported); protection: can't be equipped by Equipment with that
    quality (702.16e)."""
    if eq == t or eq not in g.s.objs or t not in g.s.objs:
        return False
    if g.s.objs[eq].zone != "battlefield" or g.s.objs[t].zone != "battlefield":
        return False
    ec, tc = g.chars(eq), g.chars(t)
    if "Equipment" not in ec.subtypes or "Creature" in ec.types:
        return False
    if "Creature" not in tc.types:
        return False
    if g.protected_from(t, eq):
        return False
    return True


def attach_equipment(g, eq, t):
    """Attach eq to t (CR 701.3a-b). Returns True if it became attached (not if it already was)."""
    o = g.s.objs[eq]
    if o.attached_to == t:
        return False  # 701.3b: nothing happens
    o.attached_to = t
    o.timestamp = g.s.new_ts()  # 613.7e
    g.s.touch()
    g.emit({"type": "attached", "obj": eq, "to": t})
    return True


def data_obj(key):
    """The object a reflexive ability's creator remembered in its trigger data."""
    def fn(g, ctx):
        x = (ctx.get("data") or {}).get(key)
        return [x] if x is not None and x in g.s.objs and g.s.objs[x].zone == "battlefield" else []
    return fx.Sel(fn, f"data:{key}")


class AttachEquipmentThenReflexive(E):
    """Thorin, Mountain-king: "attach any number of target Equipment you control to target creature
    you control. When one or more Equipment become attached to that creature this way, that
    creature deals damage equal to its power to up to one target creature." Each legal Equipment
    target that can be attached is attached (701.3a-b); if at least one became attached, the
    reflexive ability triggers (603.12) and its target is chosen as it's put on the stack."""

    def __init__(self, eq_i, cr_i, reflex_specs, text=""):
        self.eq_i = eq_i
        self.cr_i = cr_i
        self.reflex_specs = list(reflex_specs)
        self.text = text

    def run(self, g, ctx):
        from ..engine.ir import Triggered
        from ..engine.triggers import PendingTrigger
        crs = [o for o in fx.objs_of(fx.target(self.cr_i), g, ctx) if o in g.s.objs and g.s.objs[o].zone == "battlefield"]
        if not crs:
            return
        t = crs[0]
        newly = 0
        for e in fx.objs_of(fx.target(self.eq_i), g, ctx):
            if can_equip(g, e, t) and attach_equipment(g, e, t):
                newly += 1
        if not newly:
            return
        cre = data_obj("d2_creature")

        def power(g, rctx):
            objs = cre.get(g, rctx)
            if not objs:
                raise Unsupported("Thorin's reflexive damage from a creature that left the battlefield")
            return max(0, g.chars(objs[0]).power or 0)  # 608.2h: its power as the ability resolves
        eff = fx.Damage(fx.Amount(power, "its power"), fx.target(0), source=cre, text="that creature deals damage equal to its power")
        ab = Triggered(None, eff, self.reflex_specs, text=self.text)
        sc_ = ctx.get("source_chars") or ctx.get("chars")
        data = dict(ctx.get("data") or {})
        data["d2_creature"] = t
        g.s.pending.append(PendingTrigger(ab, ctx.get("source"), sc_, ctx["controller"], ctx.get("event"), data,
                                          g.after_own_triggers(ctx.get("source"), sc_)))


def _thorin(m, sc):
    eq = TargetSpec("object", filt=parse_noun("Equipment you control"), text="any number of target Equipment you control",
                    n=(0, 1000))
    sc.tc.add(eq)
    eq_i = len(sc.tc.specs) - 1
    cr = TargetSpec("object", filt=parse_noun("creature you control"), text="target creature you control")
    sc.tc.add(cr)
    cr_i = len(sc.tc.specs) - 1
    rs = TargetSpec("object", filt=parse_noun("creature"), text="up to one target creature", n=(0, 1))
    return AttachEquipmentThenReflexive(eq_i, cr_i, [rs], text=m.group(0))


class AttachTo(E):
    """"Attach <Equipment> to <creature>." (CR 701.3a: an Equipment that can't be attached to it
    doesn't move, Raubahn ruling 2025-06-06; 701.3b: already attached, nothing happens). eq=None:
    the source itself ("attach this Equipment to it", Doc Ock's Tentacles)."""

    def __init__(self, eq, cr, text=""):
        self.eq = eq
        self.cr = cr
        self.text = text

    def run(self, g, ctx):
        eqs = [ctx.get("source")] if self.eq is None else fx.objs_of(self.eq, g, ctx)
        crs = fx.objs_of(self.cr, g, ctx)
        if not eqs or not crs:
            return
        t = crs[0]
        for e in eqs:
            if can_equip(g, e, t):
                attach_equipment(g, e, t)


def _raubahn_attach(m, sc):
    eq = et.target_noun("up to one target Equipment you control", sc)
    cr = et.target_noun("target attacking creature", sc)
    return AttachTo(eq, cr, text=m.group(0))


def _attach_self_to(m, sc):
    sel = et.obj_ref(m.group(1), sc)
    return AttachTo(None, sel, text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Attach up to one target Equipment you control to target attacking creature\.", _raubahn_attach),
    (r"Attach ~ to (it|that creature)\.", _attach_self_to),
    (r"Attach any number of target Equipment you control to target creature you control\. When one or more Equipment become attached to that creature this way, that creature deals damage equal to its power to up to one target creature\.", _thorin),
]
et.MERGE_PAIRS.append((r"^[Aa]ttach any number of target Equipment you control to target creature you control\.$",
                       r"^When one or more Equipment become attached to that creature this way, "))


def _loyalty_counter_on_self(m, sc):
    """"Put a loyalty counter on ~." (CR 122.1, 306.5c: loyalty counters are the planeswalker's
    loyalty; only while it's on the battlefield)."""
    return fx.PutCounters(fx.self_permanent(), "loyalty", et.amount_of(m.group(1), sc), text=m.group(0))


def _token_named_with(m, sc):
    """"Create a 2/2 white Cat Soldier creature token named Ajani's Pridemate with "<ability>"."
    (CR 111.4: the effect names the token; its quoted ability is compiled like printed text, with
    "this token" as ~, 111.10 / 201.5)."""
    from .model import token_def
    p, t = int(m.group(1)), int(m.group(2))
    cols = [et.COLORS[c] for c in re.findall(r"white|blue|black|red|green", m.group(3))]
    subs = tuple(m.group(4).split())
    name, text = m.group(5), m.group(6)
    sc.last = fx.var("token")
    return fx.CreateToken(lambda: token_def(name, ("Creature",), subs, cols, p, t, text, named=name), 1,
                          text=m.group(0), remember="token")


def _group_gains(m, sc):
    """"Permanents you control gain indestructible until end of turn." / "Treefolk and Forests you
    control gain ..." / "Creatures you control get +1/+1 and gain trample until end of turn.": the
    affected set is locked in as the effect begins (CR 611.2c; Boros Charm ruling 2024-11-08:
    permanents that come under your control later aren't affected)."""
    noun = m.group(1)
    if noun.split()[0] in ("Permanents", "Artifacts", "Creatures", "Other", "Lands"):
        noun = noun[0].lower() + noun[1:]  # a card type, not a subtype (Treefolk)
    f = parse_noun(noun + " you control")
    sel = fx.all_matching(f)
    p = t = None
    if m.group(2):
        p, t = int(m.group(3)), int(m.group(4))
    kt = m.group(5) or m.group(6)
    kws = [k.strip() for k in re.split(r",? and |, ", kt)] if kt else []
    if "Other " in m.group(1):
        f.other = True
    for k in kws:
        if not re.fullmatch(et.KW, k):
            raise Unsupported(f"granted ability {k!r}")
    return fx.Pump(sel, p, t, keywords=kws, text=m.group(0))


et.PATTERNS[0:0] = [
    (r"(Permanents|Treefolk and Forests|Artifacts|Creatures|Other creatures|Lands) you control "
     r"(?:(get ([+-]\d+)/([+-]\d+))(?: and gain (.+?))?|gain (.+?)) until end of turn\.",
     _group_gains),
    # "Each opponent sacrifices a nontoken artifact of their choice." (Lorehold Charm): the player
    # who sacrifices chooses anyway (CR 701.21a); "of their choice" only says so.
    (r"(Each opponent|Target opponent|Target player|Each player) sacrifices? (a|an|one|two) (.+?) of their choice\.", et._sacrifice),
    # "You draw a card and gain 3 life." (Vigorbloom Charm): two instructions, in order (608.2c).
    (r"You draw (a card|two cards) and gain (\d+) life\.",
     lambda m, sc: Seq(fx.Draw(1 if m.group(1) == "a card" else 2, text=m.group(0)), fx.GainLife(int(m.group(2)), text=m.group(0)))),
    (r"Put (a|one|two|three) loyalty counters? on ~\.", _loyalty_counter_on_self),
    (r'Create an? (\d+)/(\d+) ((?:white|blue|black|red|green)(?: and (?:white|blue|black|red|green))?) ((?:[A-Z][a-z]+ )+)creature token named ([A-Z][^"]+?) with "([^"]+)"\.?', _token_named_with),
]


# =============================================================================== sacrifice any number

class SacrificeAnyNumber(E):
    """"Sacrifice any number of <permanents>, then draw that many cards." (Pitiless Carnage,
    Malevolent Witchkite) / "... Return that many creature cards from your graveyard to the
    battlefield." (Lich-Knights' Conquest). "Any number" is any subset, none included (CR 107.1c),
    chosen as the effect applies (608.2d): a Sacrifice set choice, one permanent at a time, `done`
    to stop. The chosen permanents are sacrificed at the same time (701.21a, zones.sacrifice_batch)
    and "that many" is how many were sacrificed (608.2c). then: "draw" or "return"."""

    def __init__(self, filt, then, text=""):
        self.filt = filt
        self.then = then
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        src = ctx.get("source")
        chosen = []
        while True:
            cands = [o for o in g.s.battlefield if g.ctrl(o) == p and o not in chosen
                     and self.filt.match(g, o, src, {"controller": p})]
            if not cands:
                break
            opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("sac", g.obj_sig(o))) for o in cands]
            o = g.ask(Decision(p, "Sacrifice", opts, prompt=f"Choose a permanent to sacrifice (or done): {self.text}"))
            if o is None:
                break
            chosen.append(o)
        res = g.sacrifice_batch(chosen)
        k = sum(1 for r in (res or []) if r is not None)
        ctx.setdefault("vars", {})["that_many"] = k
        if not k:
            return
        if self.then == "draw":
            g.draw(p, k)
            return
        # "Return that many creature cards from your graveyard to the battlefield": the player
        # chooses which (608.2d), all of them when there are no more than that many; they enter
        # at the same time (608.2c). Those just sacrificed are among them (400.7: new objects).
        pl = g.s.players[p]
        picked = []
        while len(picked) < k:
            cands = [o for o in pl.graveyard if o not in picked and "Creature" in g.chars(o).types]
            if not cands:
                break
            if len(cands) <= k - len(picked):
                picked.extend(cands)
                break
            opts = [Opt(f"choose:{g.ref(o)}", o, sig=("ret", g.obj_sig(o))) for o in cands]
            picked.append(g.ask(Decision(p, "ChooseCard", opts, prompt="Choose a creature card to return to the battlefield.")))
        g.move_batch([(o, "battlefield", {"controller": p}) for o in picked])


def _sacrifice_any_number(m, sc):
    filt = parse_noun(m.group(1) + " you control")
    then = "draw" if m.group(2) else "return"
    return SacrificeAnyNumber(filt, then, text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Sacrifice any number of (permanents|artifacts, enchantments, and/or tokens)(?: you control)?"
     r"(?:(, then draw that many cards)|\. Return that many creature cards from your graveyard to the battlefield)\.",
     _sacrifice_any_number),
]
et.MERGE_PAIRS.append((r"^Sacrifice any number of [a-z, /]+\.$",
                       r"^Return that many creature cards from your graveyard to the battlefield\.$"))


# =============================================================================== an opponent's "may"

class OpponentsMaySacrifice(E):
    """Desecration Demon: "any opponent may sacrifice a creature of their choice. If a player
    does, tap this creature and put a +1/+1 counter on it." The opponents decide, each in turn
    order (ruling 2024-11-08: "Each opponent in turn order may choose to sacrifice a creature,
    even if an opponent already chose to"), not the ability's controller (CR 603.5, 608.2d: the
    choice belongs to the player the effect offers it to); one who controls no creature can't
    (608.2d, 701.21a). Each is asked yes/no, then which creature (forced with one). `then`
    happens once if any player did (the ruling: at most one counter each combat)."""

    def __init__(self, filt, then, text=""):
        self.filt = filt
        self.then = then
        self.text = text

    def run(self, g, ctx):
        me = ctx["controller"]
        src = ctx.get("source")
        did = False
        order = [g.s.active, 1 - g.s.active]  # turn order from the active player (CR 101.4)
        for q in order:
            if q == me:
                continue
            cands = [o for o in g.s.battlefield if g.ctrl(o) == q and self.filt.match(g, o, src, {"controller": q})]
            if not cands:
                continue
            a = g.ask(Decision(q, "OptionalEffect", [Opt("yes", True), Opt("no", False)],
                               subject=g.ref(src) if src in g.s.objs else None,
                               prompt=f"Opponent's optional effect? {self.text}"))
            if not a:
                continue
            opts = [Opt(f"choose:{g.ref(o)}", o, sig=("sac", g.obj_sig(o))) for o in cands]
            o = g.ask(Decision(q, "Sacrifice", opts, prompt="Choose a permanent to sacrifice."))
            g.sacrifice(o)
            did = True
        ctx.setdefault("vars", {})["did"] = did
        if did and self.then is not None:
            self.then.run(g, ctx)


def _opponents_may_sacrifice(m, sc):
    filt = parse_noun(m.group(1))
    then = et.parse_one(m.group(2)[0].upper() + m.group(2)[1:], sc)
    return OpponentsMaySacrifice(filt, then, text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Any opponent may sacrifice an? (creature) of their choice\. If a player does, (.+\.)", _opponents_may_sacrifice),
]
et.MERGE_PAIRS.append((r"^[Aa]ny opponent may sacrifice an? [a-z]+ of their choice\.$", r"^If a player does, "))


def _each_combat_hook(line, cx):
    """"At the beginning of each combat, ..." (CR 507.1: every turn's beginning of combat step)."""
    m = re.match(r"^At the beginning of each combat, (.+)$", line)
    if m:
        return tg.Step("BeginCombat", "each"), m.group(1)
    return None


tg.TRIGGER_HOOKS.append(_each_combat_hook)
