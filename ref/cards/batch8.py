"""Coverage batch 8 (EUT build f788140b): the thirteen cards the EUT team made playable with
abilities on the stack as targets of their own, each rule implemented from the CR.

Mechanics: abilities on the stack as targets (CR 113.7a, 115.1; refs `stack:N`), copying an
activated or triggered ability (707.10, 707.10b-c), changing the target of a spell or ability with
a single target (115.7a, 115.9a), countering a triggered ability (701.6b), web-slinging
(702.188a), "players can't cast spells this turn" (101.2), returning a spell to its owner's hand,
gift "instead" targets (702.174m), finality counters (122.1h), Case cards (719.3, 702.169),
"you may <action> or <action>. If you do" choices, Vehicles' crew (702.122) and outlaws (700.?
Laughing Jasper Flint's Mercenaries, layer 4).

Registered through the compiler's extension hooks (compiler.KEYWORD_HOOKS, LINE_HOOKS,
POST_HOOKS; triggers.TRIGGER_HOOKS, CONDITION_HOOKS; effects_text.PATTERNS and MERGE_PAIRS;
statics.EXTRA_STATICS). Strict like the rest of the compiler: a sentence these patterns don't
match exactly still raises Unsupported.
"""
import re

from ..engine import effects as fx
from ..engine.effects import Filter, E, Sel
from ..engine.ir import (Keyword, Triggered, Static, RulePart, LayerPart, TargetSpec, Seq, Activated,
                         Cost, Trigger, Noop)
from ..engine.decisions import Decision, Opt, IllegalAction
from ..engine.objects import StackItem
from ..engine.mana import Unit
from .. import Unsupported
from . import compiler as C
from . import triggers as tg
from . import effects_text as et
from . import statics as st
from . import batch7 as b7
from .nouns import parse_noun, num
from .model import token_def


def _nc(cls):
    cls.__deepcopy__ = lambda self, memo: self
    cls.__copy__ = lambda self: self
    return cls


def _item_of(g, r):
    """The stack item a target ref names: a spell (its object id) or an ability ('stack', id)."""
    s = g.s
    if isinstance(r, tuple):
        if r[0] != "stack":
            return None
        return next((x for x in s.stack if x.id == r[1]), None)
    return next((x for x in s.stack if x.kind == "spell" and x.obj == r), None)


def _n_targets(it):
    """CR 115.9a: the number of times any object or player was chosen as a target of the spell or
    ability when it was put on the stack (changing targets doesn't change the count)."""
    return sum(len(grp) for grp in (it.targets or []))


# =============================================================================== stack targets

def stack_filter(spell_types=None, any_spell=False, ability_kinds=(), single_target=False, text=""):
    """A target word on the stack: spells (any, or of the given card types) and/or activated or
    triggered abilities (CR 113.3b-c; mana abilities never use the stack, 605.3b). A spell is
    matched through its characteristics (Filter.match_chars), an ability through match_stack.
    CR 115.5: a spell or ability on the stack is an illegal target for itself (a spell is
    skipped by legal_targets; an ability by its own context). single_target: "with a single
    target" (115.9a)."""
    spell_types = tuple(spell_types or ())

    def custom(g, c, o, src, ctx):
        if c is None:  # an ability on the stack (match_stack)
            it = o
            if getattr(it, "kind", None) not in ability_kinds or getattr(it, "ctx", None) is ctx:
                return False
            return not single_target or _n_targets(it) == 1
        if not (any_spell or (set(spell_types) & c.types)):
            return False
        if single_target:
            it = _item_of(g, o.id) if o is not None else None
            return it is not None and _n_targets(it) == 1
        return True
    return Filter(custom=custom, text=text)


def _stack_target(sc, filt, text, n=(1, 1)):
    sel = sc.tc.add(TargetSpec("stack", filt=filt, zone="stack", n=n, text=text))
    sc.last = sel
    return sel


# =============================================================================== copy an ability

def copy_ability(g, it, controller, new_targets=False):
    """CR 707.10: put a copy of an activated or triggered ability on the stack under
    `controller`. It has the same source (707.10b), the ability's characteristics and every
    decision made for it (modes, targets, X; objects used to pay its costs, rulings 2024-04-12),
    and isn't activated (no activation triggers, no "activate only" limits). Its controller is
    the copier ("you" in its text is the copier, 707.10). 707.10c: each target may stay (even if
    illegal) or change to a legal one; a copy can't target itself (115.5)."""
    s = g.s
    it2 = StackItem(it.kind, controller, s.new_id())
    it2.source = it.source
    it2.source_lki = it.source_lki
    it2.ability = it.ability
    it2.key = it.key
    fresh = ("controller", "item", "targets", "legal", "stop", "vars", "resolving", "resolving_item",
             "mode_targets", "mode_target_specs")
    ctx2 = {k: (list(v) if isinstance(v, list) else v) for k, v in it.ctx.items() if k not in fresh}
    ctx2.update({"controller": controller, "item": it2.id, "is_copy": True})
    it2.ctx = ctx2
    it2.targets = [list(grp) for grp in it.targets]
    s.stack.append(it2)
    s.touch()
    if new_targets and it2.targets:
        specs = g.target_specs(it.ability, ctx2)
        groups = []
        for spec, grp in zip(specs, it2.targets):
            out = []
            for t in grp:
                cands = [x for x in g.legal_targets(spec, controller, ctx2, out) if x not in out and x != ("stack", it2.id)]
                if t not in cands and t not in out:
                    cands.append(t)  # 707.10c: may stay unchanged, even if illegal
                opts = [Opt(f"target:{g.ref(x)}", x, sig=("target", x if isinstance(x, tuple) else g.obj_sig(x))) for x in cands]
                out.append(g.ask(Decision(controller, "Target", opts, subject=g.ref(("stack", it2.id)),
                                          prompt=f"Choose a new target for the copy (or keep it): {spec.text}")))
            groups.append(out)
        it2.targets = groups
    ctx2["targets"] = it2.targets
    s.touch()
    if it2.targets:
        g.became_targets(it2, copy=True)
    return it2


class CopyStackObject(E):
    """Return the Favor: "Copy target instant spell, sorcery spell, activated ability, or
    triggered ability. You may choose new targets for the copy." (CR 707.10, 707.10c)."""

    def __init__(self, sel, new_targets=True, text=""):
        self.sel = sel
        self.new_targets = new_targets
        self.text = text

    def run(self, g, ctx):
        for r in self.sel.get(g, ctx):
            it = _item_of(g, r)
            if it is None:
                continue
            if it.kind == "spell":
                if it.obj in g.s.objs:
                    fx.copy_spell(g, it, ctx["controller"], self.new_targets)
            else:
                copy_ability(g, it, ctx["controller"], self.new_targets)


# =============================================================================== change the target

def _target_specs_of(g, it):
    """The target words of a spell or ability on the stack, aligned with it.targets."""
    if it.kind == "spell":
        o = g.s.objs.get(it.obj)
        if o is None:
            return []
        f = g.spell_face(it.obj, o.face)
        if "Aura" in f.subtypes and bool({"Enchantment"} & set(f.types)):
            return [g.enchant_spec(f)]
        sa = g.spell_ability(f)
        return g.target_specs(sa, it.ctx) if sa is not None else []
    return g.target_specs(it.ability, it.ctx)


class ChangeTheTarget(E):
    """"Change the target of target spell or ability with a single target." (CR 115.7a): the
    new target is chosen as this resolves, by this effect's controller; it must be another legal
    target for that spell or ability (legal as if its controller chose it), never the spell or
    ability itself (115.5), and it must change if any other legal target exists; with none, the
    target is unchanged even if it's illegal (rulings 2024-04-12, 2024-11-08, 2025-10-02). The
    new target becomes the target (ward, "becomes the target" triggers: CR 702.21a)."""

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        for r in self.sel.get(g, ctx):
            it = _item_of(g, r)
            if it is None or _n_targets(it) != 1:
                continue
            specs = _target_specs_of(g, it)
            gi = next((i for i, grp in enumerate(it.targets) if grp), None)
            if gi is None or gi >= len(specs):
                raise Unsupported("change the target: target word not found")
            spec, cur = specs[gi], it.targets[gi][0]
            tctx = dict(it.ctx)
            tctx.pop("resolving", None)
            me = it.obj if it.kind == "spell" else ("stack", it.id)
            cands = [x for x in g.legal_targets(spec, it.controller, tctx, []) if x != cur and x != me]
            if not cands:
                continue
            opts = [Opt(f"target:{g.ref(x)}", x, sig=("target", x if isinstance(x, tuple) else g.obj_sig(x))) for x in cands]
            new = g.ask(Decision(p, "Target", opts, subject=g.ref(me),
                                 prompt=f"Choose the new target: {spec.text}"))
            it.targets[gi][0] = new
            if it.ctx.get("targets") is not it.targets:
                it.ctx["targets"] = it.targets
            g.s.touch()
            g.emit({"type": "targeted", "target": new, "item": it.id, "controller": it.controller,
                    "kind": it.kind, "source": it.obj if it.kind == "spell" else it.source})


def _change_target(m, sc):
    f = stack_filter(any_spell=True, ability_kinds=("activated", "triggered"), single_target=True,
                     text="spell or ability with a single target")
    sel = _stack_target(sc, f, "target spell or ability with a single target")
    return ChangeTheTarget(sel, text=m.group(0))


def _copy_spell_or_ability(m, sc):
    f = stack_filter(spell_types=("Instant", "Sorcery"), ability_kinds=("activated", "triggered"),
                     text="instant spell, sorcery spell, activated ability, or triggered ability")
    sel = _stack_target(sc, f, "target instant spell, sorcery spell, activated ability, or triggered ability")
    return CopyStackObject(sel, new_targets=bool(m.group(1)), text=m.group(0))


def _counter_inst_sorc_trig(m, sc):
    """Spider-Sense: "Counter target instant spell, sorcery spell, or triggered ability." (CR
    701.6a-b: a countered ability is removed from the stack; an activated ability or a creature
    spell isn't a legal target)."""
    f = stack_filter(spell_types=("Instant", "Sorcery"), ability_kinds=("triggered",),
                     text="instant spell, sorcery spell, or triggered ability")
    sel = _stack_target(sc, f, "target instant spell, sorcery spell, or triggered ability")
    return fx.CounterSpell(sel, text=m.group(0))


# =============================================================================== return a spell

class ReturnSpellToHand(E):
    """Bilbo's Gambit: "Return target spell to its owner's hand." Not countering: a spell that
    can't be countered still goes (CR 701.6 doesn't apply). A copy of a spell ceases to exist
    once it's off the stack (707.10a, 704.5e); a spell cast with flashback is exiled instead of
    going anywhere else (702.34a)."""

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        for r in fx.objs_of(self.sel, g, ctx):
            it = _item_of(g, r)
            o = g.s.objs.get(r)
            if it is None or o is None or o.zone != "stack":
                continue
            dest = g._leave_stack_zone(it)
            g.move(r, "hand" if dest == "graveyard" else dest, cause="effect")


def _return_spell(m, sc):
    sel = _stack_target(sc, Filter(text="spell"), "target spell")
    return ReturnSpellToHand(sel, text=m.group(0))


class PlayersCantCastThisTurn(E):
    """"Players can't cast spells this turn." (CR 101.2: a "can't" effect; it starts as the spell
    resolves, so spells already on the stack still resolve; it lasts until the cleanup step,
    514.2)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        fx.AddEffect(None, {}, duration="eot", kind="rule",
                     data={"rule": "cant_cast", "part": RulePart("cant_cast", None, who="each")}).run(g, ctx)


def _gift_players_cant_cast(m, sc):
    return fx.If(b7.gift_promised, PlayersCantCastThisTurn(), text=m.group(0))


# =============================================================================== gift "instead" targets

def _gift_n(promised):
    """Target count of a gift spell's target word that exists only if the gift was (or wasn't)
    promised (CR 702.174m; announced before targets, 601.2b-c)."""
    def n(g, ctx, promised=promised):
        return 1 if b7.gift_promised(g, ctx) == promised else 0
    return n


@_nc
class GiftTargetChoice(fx.ChoiceCost):
    """The optional gift cost of a spell whose targets depend on the promise (Into the Flood
    Maw, Long River's Pull): promising is offered only if the promised targets exist, declining
    only if the others do (601.2b-c; a spell can't be cast without its required targets)."""

    def __init__(self, base, specs):
        self.alts = base.alts
        self.optional = base.optional
        self.label = base.label
        self.specs = specs  # (unpromised spec, promised spec)

    def _has(self, g, p, oid, spec):
        ctx = {"source": oid, "controller": p, "x": 0, "chars": g.chars(oid)}
        return bool(g.legal_targets(spec, p, ctx, []))

    def castable(self, g, p, oid, cost, spend):
        if not g.mana_payable(p, cost, 0, spend):
            return False
        return self._has(g, p, oid, self.specs[0]) or self._has(g, p, oid, self.specs[1])

    def choose(self, g, p, oid, ctx, allowed=None):
        opts = []
        if self._has(g, p, oid, self.specs[1]):
            for i, a in enumerate(self.alts):
                if allowed is not None and not any(a is b for b in allowed):
                    continue
                if a.payable(g, p, oid):
                    opts.append(Opt(self.label(i, a) if self.label else f"cost:{i + 1}", a))
        if self._has(g, p, oid, self.specs[0]):
            opts.append(Opt("no", None))
        if not opts:
            raise IllegalAction("no legal targets with or without the gift")
        return g.ask(Decision(p, "AdditionalCost", opts, subject=g.ref(oid), prompt="Promise the gift?"))


def _gift_instead_bounce(m, sc):
    """Into the Flood Maw: "Return target creature an opponent controls to its owner's hand. If
    the gift was promised, instead return target nonland permanent an opponent controls to its
    owner's hand." (702.174m: one target, of the first kind or, if promised, of the second)."""
    f0 = parse_noun(m.group(1))
    f1 = parse_noun(m.group(2))
    s0 = TargetSpec("object", filt=f0, zone="battlefield", n=(_gift_n(False), _gift_n(False)), text="target " + m.group(1))
    s1 = TargetSpec("object", filt=f1, zone="battlefield", n=(_gift_n(True), _gift_n(True)), text="target " + m.group(2))
    t0 = sc.tc.add(s0)
    t1 = sc.tc.add(s1)
    sc.gift_specs = (s0, s1)
    return fx.If(b7.gift_promised, fx.MoveTo(t1, "hand"), fx.MoveTo(t0, "hand"), text=m.group(0))


def _gift_instead_counter(m, sc):
    """Long River's Pull: "Counter target creature spell. If the gift was promised, instead
    counter target spell." (702.174m)."""
    f0 = et.parse_spell(m.group(1) + " spell")
    f1 = et.parse_spell(m.group(2) + " spell" if m.group(2) else "spell")
    s0 = TargetSpec("spell", filt=f0, zone="stack", n=(_gift_n(False), _gift_n(False)), text=f"target {m.group(1)} spell")
    s1 = TargetSpec("spell", filt=f1, zone="stack", n=(_gift_n(True), _gift_n(True)), text="target spell")
    t0 = sc.tc.add(s0)
    t1 = sc.tc.add(s1)
    sc.gift_specs = (s0, s1)
    return fx.If(b7.gift_promised, fx.CounterSpell(t1), fx.CounterSpell(t0), text=m.group(0))


def _gift_targets_post(abilities, cx):
    """Swap the gift's ChoiceCost for GiftTargetChoice on a spell whose targets depend on the
    promise (the two target words are the spell's only ones)."""
    sa = next((a for a in abilities if a.kind == "spell"), None)
    if sa is None or not any(getattr(t, "n", None) and callable(t.n[0]) for t in sa.targets):
        return abilities
    specs = [t for t in sa.targets if callable(t.n[0])]
    if len(specs) != 2 or len(sa.targets) != 2:
        raise Unsupported("gift-dependent targets with other targets")
    out = []
    for a in abilities:
        if a.kind == "keyword" and a.keyword == "additional_cost" and isinstance(a.value, fx.ChoiceCost) \
                and any(isinstance(x, b7.GiftCost) for x in a.value.alts):
            a = Keyword("additional_cost", value=GiftTargetChoice(a.value, (specs[0], specs[1])), text=a.text)
        out.append(a)
    return out


# =============================================================================== "you may A or B. If you do"

class MayActionThen(E):
    """"You may sacrifice an artifact or discard a card. If you do, draw a card." / "You may
    discard a card or sacrifice a land. If you do, draw two cards." (CR 608.2c-d): as it
    resolves, its controller chooses one of the actions they can do (mode:<n>, Oracle order) or
    neither (no); an action that can't be done isn't offered (Highway Robbery ruling
    2024-04-12), and with none, nothing is asked and nothing is drawn. The chosen action is then
    done (which card / which permanent: its own choice)."""
    reports_done = True

    def __init__(self, actions, then, text=""):
        self.actions = actions  # [("discard", None) | ("sacrifice", Filter)]
        self.then = then
        self.text = text

    def _cands(self, g, p, a, ctx):
        kind, f = a
        if kind == "discard":
            return list(g.s.players[p].hand)
        return [x for x in g.s.battlefield if g.ctrl(x) == p and f.match(g, x, ctx.get("source"), ctx)]

    def run(self, g, ctx):
        p = ctx["controller"]
        opts = [Opt(f"mode:{i + 1}", i) for i, a in enumerate(self.actions) if self._cands(g, p, a, ctx)]
        if not opts:
            ctx.setdefault("vars", {})["did"] = False
            return False
        opts.append(Opt("no", None))
        i = g.ask(Decision(p, "ChooseMode", opts, subject=g.ref(ctx["source"]) if ctx.get("source") in g.s.objs else None,
                           prompt=f"Choose an action or neither: {self.text}"))
        if i is None:
            ctx.setdefault("vars", {})["did"] = False
            return False
        kind, f = self.actions[i]
        cands = self._cands(g, p, self.actions[i], ctx)
        if kind == "discard":
            opts = [Opt(f"choose:{g.ref(o)}", o, sig=("disc", g.obj_sig(o))) for o in cands]
            o = g.ask(Decision(p, "Discard", opts, prompt="Choose a card to discard."))
            g.discard(p, o, cause="effect")
        else:
            opts = [Opt(f"choose:{g.ref(o)}", o, sig=("sac", g.obj_sig(o))) for o in cands]
            o = g.ask(Decision(p, "Sacrifice", opts, prompt=f"Choose {f.text} to sacrifice."))
            g.sacrifice(o)
        ctx.setdefault("vars", {})["did"] = True
        self.then.run(g, ctx)
        return True


def _may_action_then(m, sc):
    acts = []
    for part in (m.group(1), m.group(2)):
        if part == "discard a card":
            acts.append(("discard", None))
        else:
            mm = re.fullmatch(r"sacrifice an? (artifact|land|creature)", part)
            if not mm:
                raise Unsupported(f"action {part!r}")
            acts.append(("sacrifice", parse_noun(mm.group(1) + " you control")))
    then = fx.Draw(num(m.group(3)))
    return MayActionThen(acts, then, text=m.group(0))


# =============================================================================== Overlord's Everywhere

def _everywhere_token():
    """"a tapped colorless land token named Everywhere that is every basic land type" (CR 111.4:
    the effect names it; 305.6: each basic land type gives its mana ability; it isn't basic,
    Overlord of the Hauntwoods ruling 2024-09-20)."""
    return token_def("Everywhere", ("Land",), ("Plains", "Island", "Swamp", "Mountain", "Forest"), (),
                     named="Everywhere")


def _everywhere(m, sc):
    sc.last = fx.var("token")
    return fx.CreateToken(_everywhere_token, 1, tapped=True, text=m.group(0), remember="token")


# =============================================================================== Case cards (719)

def _solved(g, oid):
    o = g.s.objs.get(oid)
    return o is not None and o.zone == "battlefield" and bool(o.flags.get("solved"))


class BecomeSolved(E):
    """CR 719.3a-b: this Case becomes solved (a designation, not copiable; it stays until the
    permanent leaves the battlefield)."""
    text = "becomes solved"

    def run(self, g, ctx):
        o = g.s.objs.get(ctx.get("source"))
        if o is not None and o.zone == "battlefield":
            o.flags["solved"] = True
            g.s.touch()


@_nc
class SolvedOnly(Trigger):
    """CR 702.169c: "Solved — [triggered ability]" means "[ability]. This ability triggers only if
    this Case is solved." (a trigger condition, not an intervening "if": not rechecked on
    resolution)."""

    def __init__(self, inner):
        self.inner = inner
        self.events = inner.events
        self.lookback = inner.lookback
        self.zones = inner.zones

    def match(self, g, ev, src, c, ctrl):
        if not _solved(g, src):
            return None
        return self.inner.match(g, ev, src, c, ctrl)


_SOLVE_CONDITIONS = {
    "You have no cards in hand": lambda g, ctx: not g.s.players[ctx["controller"]].hand,
}


def _case_line(line, cx):
    m = re.fullmatch(r"To solve — (.+?)\.", line)
    if m:
        cond = _SOLVE_CONDITIONS.get(m.group(1))
        if cond is None:
            raise Unsupported(f"Case condition {m.group(1)!r}")

        def check(g, ctx, cond=cond):
            # 719.3a: "if [condition] and this Case is not solved" (an intervening if, 603.4).
            return cond(g, ctx) and not _solved(g, ctx.get("source"))
        return [Triggered(tg.Step("End", "you"), BecomeSolved(), [], condition=check, text=line)]
    m = re.fullmatch(r"Solved — (.+)", line)
    if m:
        res = C.compile_line(m.group(1), cx)
        if res == "spell" or not res or any(a.kind != "triggered" for a in res):
            raise Unsupported(f"Solved ability {line!r}")
        out = []
        for a in res:
            out.append(Triggered(SolvedOnly(a.trigger), a.effect, a.targets, optional=a.optional,
                                 condition=a.condition, text=line, zones=a.zones, modes=a.modes,
                                 limit=a.limit))
        return out
    return None


# =============================================================================== finality, delirium

def _four_card_types(g, oid, p):
    """Delirium: four or more card types among cards in your graveyard (CR 207.2c ability word;
    205.2a card types)."""
    return len(fx.card_types_among(g, g.s.players[p].graveyard)) >= 4


def _return_from_gy_line(line, cx):
    """Balustrade Wurm: "{2}{G}{G}: Return ~ from your graveyard to the battlefield with a
    finality counter on it. Activate only if there are four or more card types among cards in
    your graveyard and only as a sorcery." (CR 113.6m: it functions only in the graveyard; the
    finality counter, 122.1h, is put on as it enters, 614.1c)."""
    m = re.fullmatch(r"((?:\{[^}]+\})+): Return ~ from your graveyard to the battlefield with a finality counter on it\. "
                     r"Activate only if there are four or more card types among cards in your graveyard and only as a sorcery\.", line)
    if not m:
        return None
    from ..engine.mana import parse_cost
    eff = fx.PutOntoBattlefield(fx.self_sel(), from_zones=("graveyard",), controller="you",
                                counters={"finality": 1}, text=line)
    return [Activated(Cost(mana=parse_cost(m.group(1))), eff, [], timing="sorcery", text=line,
                      condition=_four_card_types, zones=("graveyard",))]


# =============================================================================== Bolt Bend

def _self_cost_if_power(l, cx):
    """"~ costs {3} less to cast if you control a creature with power 4 or greater." (CR 601.2f;
    the condition is checked as the total cost is determined, ruling 2024-11-08)."""
    m = re.fullmatch(r"~ costs \{(\d+)\} less to cast if you control a creature with power (\d+) or greater\.", l)
    if not m:
        return None
    k, pw = int(m.group(1)), int(m.group(2))

    def n(g, oid, p, k=k, pw=pw):
        return -k if any(g.ctrl(x) == p and "Creature" in g.chars(x).types and (g.chars(x).power or 0) >= pw
                         for x in g.s.battlefield) else 0
    return [Static([RulePart("self_cost_mod", "self", n=n)], text=l, zones=("all",))]


# =============================================================================== registration

et.PATTERNS[0:0] = [
    (r"Change the target of target spell or ability with a single target\.", _change_target),
    (r"Copy target instant spell, sorcery spell, activated ability, or triggered ability\.( You may choose new targets for the copy\.)?",
     _copy_spell_or_ability),
    (r"Counter target instant spell, sorcery spell, or triggered ability\.", _counter_inst_sorc_trig),
    (r"Return target spell to its owner's hand\.", _return_spell),
    (r"If the gift was promised, players can't cast spells this turn\.", _gift_players_cant_cast),
    (r"Return target (creature an opponent controls) to its owner's hand\. If the gift was promised, instead return target (nonland permanent an opponent controls) to its owner's hand\.",
     _gift_instead_bounce),
    (r"Counter target (creature) spell\. If the gift was promised, instead counter target ()spell\.", _gift_instead_counter),
    (r"You may (discard a card|sacrifice an artifact) or (sacrifice a land|discard a card)\. If you do, draw (a|two) cards?\.", _may_action_then),
    (r"Create a tapped colorless land token named Everywhere that is every basic land type\.", _everywhere),
    (r"Discard a card, then draw (two) cards\.", lambda m, sc: Seq(fx.Discard(1, fx.you()), fx.Draw(num(m.group(1))))),
]

et.MERGE_PAIRS.extend([
    (r"^Copy target instant spell, sorcery spell, activated ability, or triggered ability\.$", r"^You may choose new targets for the copy\.$"),
    (r"^You may (?:discard a card or sacrifice a land|sacrifice an artifact or discard a card)\.$", r"^If you do, draw (?:a card|two cards)\.$"),
    (r"^Return target creature an opponent controls to its owner's hand\.$", r"^If the gift was promised, instead return target nonland permanent"),
    (r"^Counter target creature spell\.$", r"^If the gift was promised, instead counter target spell\.$"),
])

C.LINE_HOOKS.extend([_case_line, _return_from_gy_line])
C.POST_HOOKS.append(_gift_targets_post)
st.EXTRA_STATICS[0:0] = [_self_cost_if_power]


# =============================================================================== Laughing Jasper Flint

OUTLAW_TYPES = ("Assassin", "Mercenary", "Pirate", "Rogue", "Warlock")  # CR 700.12


def _outlaws_you_control(g, ctx):
    """CR 700.12, 700.12a: outlaw permanents a player controls."""
    p = ctx["controller"]
    return sum(1 for x in g.s.battlefield if g.ctrl(x) == p and set(OUTLAW_TYPES) & g.chars(x).subtypes)


def _mercenaries_static(l, cx):
    """"Creatures you control but don't own are Mercenaries in addition to their other types."
    A type-changing effect (layer 4, CR 613.1d) of a static ability; whether an object is a
    creature is decided in that layer too, so another layer-4 effect that makes a stolen object
    a creature, or stops it being one, changes what this applies to (613.8a: dependency). Once it
    began applying, losing the ability in layer 6 doesn't undo it (613.6)."""
    if l != "Creatures you control but don't own are Mercenaries in addition to their other types.":
        return None

    def aff(g, oid, c, src):
        o = g.s.objs.get(oid)
        sc = g.chars(src) if src in g.s.objs else None
        return (o is not None and sc is not None and "Creature" in c.types
                and c.controller == sc.controller and o.owner != c.controller)

    def add(g, oid, c, src):
        c.subtypes.add("Mercenary")
    return [Static([LayerPart(4, aff, add, text=l)], text=l)]


class ExileTopOfTargetMayCast(E):
    """Laughing Jasper Flint: "exile the top X cards of target opponent's library, where X is the
    number of outlaws you control. Until end of turn, you may cast spells from among those cards,
    and mana of any type can be spent to cast those spells." X is counted as it resolves (ruling
    2024-04-12). The cards are exiled face up (406.3); casting them follows the normal timing and
    costs (ruling 2024-04-12), with mana of any type (609.4b); lands can't be played (only
    "cast")."""

    def __init__(self, who, text=""):
        self.who = who
        self.text = text

    def run(self, g, ctx):
        s = g.s
        p = ctx["controller"]
        n = _outlaws_you_control(g, ctx)
        ctx.setdefault("vars", {})["exiled"] = []
        for q in fx.players_of(self.who, g, ctx):
            lib = s.players[q].library
            k = min(n, len(lib))
            if k <= 0:
                continue
            g.top_hook(q, k, "exile")
            res = g.move_batch([(o, "exile", {"cause": "effect"}) for o in list(lib[:k])])
            kept = [r for r in res if r is not None and r in s.objs and s.objs[r].zone == "exile"]
            for r in kept:
                s.objs[r].flags["may_play"] = {"player": p, "until": (s.turn, None), "any_type": True, "lands": False}
            ctx["vars"]["exiled"] += kept
        s.touch()


def _flint_upkeep(m, sc):
    who = et.player_ref("target opponent", sc)
    return ExileTopOfTargetMayCast(who, text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Exile the top X cards of target opponent's library, where X is the number of outlaws you control\. "
     r"Until end of turn, you may cast spells from among those cards, and mana of any type can be spent to cast those spells\.",
     _flint_upkeep),
]
et.MERGE_PAIRS.extend([
    (r"exile the top X cards of target opponent's library, where X is the number of outlaws you control\.$",
     r"^Until end of turn, you may cast spells from among those cards, and mana of any type can be spent to cast those spells\.$"),
])
st.EXTRA_STATICS[0:0] = [_mercenaries_static]


# =============================================================================== web-slinging (702.188a)

def _tap_only(cost):
    """A mana ability whose whole cost is {T} (no mana, no other parts)."""
    return (cost.tap and not cost.untap and not cost.mana and not cost.life and not cost.sac_self
            and cost.sacrifice is None and cost.discard is None and not cost.exile_self
            and cost.exile_from_gy is None and cost.remove_counters is None and cost.loyalty is None
            and cost.tap_others is None and cost.return_to_hand is None and not cost.discard_self
            and cost.exile_from_hand is None and cost.blight is None and getattr(cost, "put_counters", None) is None)


@_nc
class WebSlingReturn(fx.CostPart):
    """CR 702.188a: "You may cast this spell by paying [cost] and returning a tapped creature you
    control to its owner's hand rather than paying its mana cost" (an alternative cost, 601.2b,
    601.2f-h). The creature must be tapped as costs are paid, so one tapped for mana during the
    same payment (601.2g) may be returned (Spider-Sense ruling 2025-09-19): the choice is a
    PayCost decision before the mana is paid, offering each tapped creature you control
    (choose:<ref>) and each untapped one with a {T}-only mana ability it can activate (302.6),
    tapped right away for that mana (mana:<ref>:<color>), whenever the rest of the cost stays
    payable (118.3). Returned to its owner's hand; its leaves-the-battlefield triggers wait until
    the spell is cast (603.3)."""
    name = "web-slinging"

    def _options(self, g, p, oid, cost, spend):
        s = g.s
        out = []
        for x in list(s.battlefield):
            o = s.objs[x]
            c = g.chars(x)
            if c.controller != p or "Creature" not in c.types or o.phased_out:
                continue
            if o.tapped:
                if g.mana_payable(p, cost, 0, spend, exclude={x}):
                    out.append(("choose", x, None, None))
                continue
            for ab in c.abilities:
                if ab.kind != "activated" or not ab.is_mana or not _tap_only(ab.cost):
                    continue
                if not g.can_activate_mana(p, x, ab):
                    continue
                for prod in g.productions(ab, x):
                    pool = s.players[p].pool
                    add = [Unit(col, source=x, restrict=ab.mana.restrict) for col in prod]
                    pool.extend(add)
                    try:
                        ok = g.mana_payable(p, cost, 0, spend, exclude={x})
                    finally:
                        del pool[len(pool) - len(add):]
                    if ok and not any(o2[0] == "mana" and o2[1] == x and o2[3] == prod for o2 in out):
                        out.append(("mana", x, ab, prod))
        return out

    def castable(self, g, p, oid, cost, spend):
        return bool(self._options(g, p, oid, cost, spend))

    def payable(self, g, p, oid):
        return True

    def pre_mana(self, g, p, oid, ctx, cost, x):
        """Called by casting.cast before the mana payment (601.2g-h)."""
        if ctx.get("web_slung"):
            return
        spend = {"kind": "spell", "chars": g.chars(oid), "obj": oid}
        cands = self._options(g, p, oid, cost, spend)
        if not cands:
            raise IllegalAction("no tapped creature to return for web-slinging")
        opts = []
        for kind, c, ab, prod in cands:
            if kind == "choose":
                opts.append(Opt(f"choose:{g.ref(c)}", (kind, c, ab, prod), sig=("ws", g.obj_sig(c))))
            else:
                opts.append(Opt(f"mana:{g.ref(c)}:{prod[0]}", (kind, c, ab, prod), sig=("wsm", g.obj_sig(c), prod)))
        kind, c, ab, prod = g.ask(Decision(p, "PayCost", opts, subject=g.ref(oid),
                                           prompt="Web-slinging: return a tapped creature you control to its owner's hand."))
        if kind == "mana":
            g.activate_mana(p, c, ab, prod, spend)
        # Returned when the costs are paid (601.2h), after the mana abilities (601.2g): until
        # then it is still on the battlefield, so an ability it grants can make mana for this
        # payment (Great Divide Guide's "Each land and Ally you control has '{T}: Add one mana
        # of any color'" on a Forest, campaign13 835031).
        ctx["web_slung"] = True
        ctx["web_sling_obj"] = c

    def pay(self, g, p, oid, ctx):
        if not ctx.get("web_slung"):
            self.pre_mana(g, p, oid, ctx, [], 0)
        c = ctx.pop("web_sling_obj", None)
        if c is not None and c in g.s.objs and g.s.objs[c].zone == "battlefield":
            ctx.setdefault("paid", {}).setdefault("returned", []).append(g.chars(c))
            g.move(c, "hand", cause="cost")


def _web_slinging_keyword(p, cx):
    m = re.fullmatch(r"web-slinging ((?:\{[^}]+\})+)", p, re.I)
    if not m:
        return None
    from ..engine.mana import parse_cost
    k = Keyword("web-slinging", value=parse_cost(m.group(1)), text=p)
    k.alt_zone = "hand"
    k.extra = {"parts": [WebSlingReturn()]}
    return [k]


C.KEYWORD_HOOKS.append(_web_slinging_keyword)


# =============================================================================== crew, Chandra

def _becomes_artifact_creature(g, oid, c, ce):
    c.types.update(("Artifact", "Creature"))  # layer 4 (CR 613.1d); printed P/T, 301.7b


def _gains_haste(g, oid, c, ce):
    c.abilities.append(Keyword("haste"))  # layer 6 (613.1f)


def crew_ability(n, text):
    """CR 702.122a: "Crew N" means "Tap any number of other untapped creatures you control with
    total power N or greater: This permanent becomes an artifact creature until end of turn."
    (casting.crew_payable / pay_cost: PayCost choose:<ref> picks, done)."""
    cost = Cost(text=text)
    cost.crew = n
    eff = fx.AddEffect(fx.self_permanent(), {4: _becomes_artifact_creature}, duration="eot", text=text)
    a = Activated(cost, eff, [], timing="instant", text=text)
    a.keyword = "crew"
    return a


def _crew_keyword(p, cx):
    m = re.fullmatch(r"crew (\d+)", p, re.I)
    if not m:
        return None
    return [crew_ability(int(m.group(1)), p)]


def _vehicle_token(m, sc):
    """"Create a 3/2 colorless Vehicle artifact token with crew 1." (CR 111.4: named "Vehicle
    Token"; a noncreature artifact until crewed, 301.7a)."""
    pw, tg_ = int(m.group(1)), int(m.group(2))
    sc.last = fx.var("token")
    return fx.CreateToken(lambda: token_def("Vehicle Token", ("Artifact",), ("Vehicle",), (), pw, tg_,
                                            f"Crew {m.group(3)}", named="Vehicle Token"),
                          1, text=m.group(0), remember="token")


def _choose_target_then_animate(m, sc):
    """Chandra, Spark Hunter: "choose up to one target Vehicle you control. Until end of turn, it
    becomes an artifact creature and gains haste." (layer 4 and layer 6; the affected object is
    locked as the effect begins, 611.2c)."""
    sel = et.target_noun("up to one target " + m.group(1), sc)
    sc.last = sel
    return fx.AddEffect(sel, {4: _becomes_artifact_creature, 6: _gains_haste}, duration="eot", text=m.group(0))


def _emblem_damage(m, sc):
    """An emblem's "this emblem deals N damage to any target" (CR 114.1-114.4: the emblem is the
    source; it has no characteristics (colorless, Chandra ruling 2025-02-07): no object source)."""
    sel = et.target_noun(m.group(2), sc)
    return fx.Damage(int(m.group(1)), sel, text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Choose up to one target (Vehicle you control)\. Until end of turn, it becomes an artifact creature and gains haste\.",
     _choose_target_then_animate),
    (r"Create a (\d+)/(\d+) colorless Vehicle artifact token with crew (\d+)\.", _vehicle_token),
    (r"This emblem deals (\d+) damage to (any target)\.", _emblem_damage),
]
et.MERGE_PAIRS.extend([
    (r"^choose up to one target Vehicle you control\.$", r"^Until end of turn, it becomes an artifact creature and gains haste\.$"),
])
C.KEYWORD_HOOKS.append(_crew_keyword)
