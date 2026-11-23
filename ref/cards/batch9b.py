"""Coverage batch 9 (EUT builds ad91628c / a004ffc2), the cards without firebending, airbend or
split cards: Garruk, Veiled Butcher; Anzrag's Rampage; High Noon; Fiery Annihilation; Archdruid's
Charm; Troublemaker Ouphe; Zero Point Ballad; Molten Collapse; Pawpatch Recruit; Sanctum Lurker;
Suki, Courageous Rescuer; Leyline of Resonance; Fumigate; Wail of the Forgotten; and the hidden
exile zone changes of Purgatory, Endless Horizons and Memory Theft.

Mechanics, each from its rule: offspring (CR 702.175a), descend and "descended this turn" (700.11;
the count is kept in zones.after_move), a number of modes that depends on the game as the spell is
cast (601.2b, 700.2; casting.choose_modes reads `Modes.max_for`), "if it was bargained" on the
permanent (702.166b, 400.7d; casting.cast records the optional costs paid in `cast`), a cast
limit per player (101.2; combat.rule_applies_player `filter_p`), the 704.5i exception (turn.sba,
rule `zero_loyalty_stays`), a granted loyalty ability (613.1f, 606.3), "becomes the target"
triggers (603.2e), leaves-the-battlefield triggers during your turn (603.6c, 603.10a), copies of
spells (707.10, 707.10c), simultaneous destruction and sacrifice (701.8a, 701.21a, 101.4), linked
exile (607.2a) and a target that depends on an earlier target (Fiery Annihilation; casting
`targets_partial`).

Decision kinds follow the EUT's where the format leaves them open: a card chosen from exile
(Anzrag's Rampage, Purgatory, Endless Horizons, Memory Theft) is a `Search` (`done` when it's
optional); Zero Point Ballad's return is a `ChooseCard`.

Registered into the compiler's hooks (compiler.py, above KEYWORD_HOOKS; MODAL_HEADER_HOOKS for
the conditional modal headers). Strict like the rest of the compiler: a sentence these patterns
don't match exactly still raises Unsupported.
"""
import re

from ..engine import effects as fx
from ..engine.effects import Filter, E, Sel
from ..engine.ir import (Keyword, Triggered, Static, RulePart, LayerPart, TargetSpec, Seq,
                         Activated, Cost, Trigger)
from ..engine.decisions import Decision, Opt
from .. import Unsupported
from . import compiler as C
from . import triggers as tg
from . import effects_text as et
from . import statics as st
from .nouns import num
from ..engine.mana import parse_cost

PERMANENT_TYPES = {"Artifact", "Battle", "Creature", "Enchantment", "Land", "Planeswalker"}


def _nc(cls):
    cls.__deepcopy__ = lambda self, memo: self
    cls.__copy__ = lambda self: self
    return cls


def _src_obj(g, ctx):
    src = ctx.get("source")
    return g.s.objs.get(src) or g.s.lki_obj.get(src)


def _one(oid):
    return Sel(lambda g, ctx, oid=oid: [oid] if oid in g.s.objs else [], "it")


def descended(g, p):
    """CR 700.11: a permanent card was put into p's graveyard from anywhere this turn
    (zones.after_move counts them; a scenario's "descended_this_turn" is the active player's)."""
    tf = g.s.turn_flags
    return bool(tf.get(f"descended_p{p}")) or (bool(tf.get("descended")) and p == g.s.active)


def destroy_recorded(g, objs):
    """Destroy objs at once (CR 701.8a; indestructible ones aren't destroyed, 702.12b; a
    regeneration shield replaces the destruction, 701.19). Returns [(old id, new id or None)] for
    the permanents destroyed (a destroyed creature exiled instead by a replacement effect was
    still destroyed: Fumigate ruling 2024-11-08)."""
    batch = []
    for o in objs:
        if o not in g.s.objs or g.s.objs[o].zone != "battlefield":
            continue
        if g.chars(o).has_kw("indestructible"):
            continue
        ev = g.replace("destroy", {"type": "destroy", "obj": o, "applied": set()})
        if not ev.get("cancel"):
            batch.append(o)
    res = g.move_batch([(o, "graveyard", {"cause": "destroy"}) for o in batch])
    return list(zip(batch, res))


# =============================================================================== Garruk

class EachPlayerSacrificesCreature(E):
    """"Each player sacrifices a creature of their choice. If you sacrificed a creature this way,
    create a 4/4 green Beast creature token with trample." CR 101.4: the players choose in APNAP
    order, then the creatures are sacrificed at the same time (701.21a). A token counts."""

    def __init__(self, then, text=""):
        self.then = then
        self.text = text

    def run(self, g, ctx):
        s = g.s
        me = ctx["controller"]
        chosen = []
        for p in (s.active, 1 - s.active):
            cands = [o for o in s.battlefield if g.ctrl(o) == p and "Creature" in g.chars(o).types]
            if not cands:
                continue
            opts = [Opt(f"choose:{g.ref(o)}", o, sig=("sac", g.obj_sig(o))) for o in cands]
            chosen.append((p, g.ask(Decision(p, "Sacrifice", opts, prompt="Choose a creature to sacrifice."))))
        if chosen:
            res = g.move_batch([(o, "graveyard", {"cause": "sacrifice"}) for _, o in chosen])
            for (p, o), nid in zip(chosen, res):
                ts = s.players[p].turn_stats
                ts["sacrificed"] = ts.get("sacrificed", 0) + 1  # as zones.sacrifice counts them
                g.emit({"type": "sacrificed", "obj": nid, "old": o, "player": p, "lki": s.lki.get(o)})
        if any(p == me for p, _ in chosen):
            self.then.run(g, ctx)


class OpponentsDiscardTwoDrawPer(E):
    """"Each opponent discards two cards. For each opponent who didn't discard two nonland cards
    this way, you draw a card." (CR 701.9a). A player with two or fewer cards discards them all
    (no choice); with fewer than two cards, or a land among the two, you draw."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        s = g.s
        me = ctx["controller"]
        draws = 0
        for p in fx.players_of(fx.opponent(), g, ctx):
            hand = list(s.players[p].hand)
            if len(hand) <= 2:
                picks = hand
            else:
                picks = []
                for _ in range(2):
                    cands = [o for o in s.players[p].hand if o not in picks]
                    opts = [Opt(f"choose:{g.ref(o)}", o, sig=("disc", g.obj_sig(o))) for o in cands]
                    picks.append(g.ask(Decision(p, "Discard", opts, prompt="Choose a card to discard.")))
            nonland = sum(1 for o in picks if "Land" not in g.chars(o).types)
            for o in picks:
                g.discard(p, o, cause="effect")
            if nonland < 2:
                draws += 1
        if draws:
            g.draw(me, draws)


def _pump_until_next_turn(m, sc):
    """"Up to one target creature gets -4/-1 until your next turn." (CR 611.2a: the stated
    duration; it ends as its controller's next turn begins, turn.next_turn)."""
    sel = et.obj_ref(m.group(1), sc)
    sc.last = sel
    return fx.Pump(sel, int(m.group(2)), int(m.group(3)), duration="until_your_next_turn", text=m.group(0))


def _garruk_sac(m, sc):
    beast = fx.CreateToken(et.parse_token("4/4 green Beast creature token with trample"), 1, text="create a Beast")
    return EachPlayerSacrificesCreature(beast, text=m.group(0))


# =============================================================================== Anzrag's Rampage

class _ReturnToHandObjs(E):
    """The delayed trigger's "Return it to your hand": only the same object, still on the
    battlefield (CR 603.7c, 400.7)."""

    def __init__(self, ids):
        self.ids = tuple(ids)
        self.text = "return it to your hand"

    def run(self, g, ctx):
        moves = [(o, "hand", {}) for o in self.ids if o in g.s.objs and g.s.objs[o].zone == "battlefield"]
        if moves:
            g.move_batch(moves)


class AnzragsRampage(E):
    """"Destroy all artifacts you don't control, then exile the top X cards of your library, where X
    is the number of artifacts that were put into graveyards from the battlefield this turn. You
    may put a creature card exiled this way onto the battlefield. It gains haste. Return it to your
    hand at the beginning of the next end step." X is counted after the destruction (608.2c: in
    order), every artifact put into a graveyard from the battlefield this turn, tokens included
    (ruling 2024-02-02). "It gains haste" has no duration (611.2a); the return is a delayed
    triggered ability (603.7) that affects only that object (603.7c)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        s = g.s
        p = ctx["controller"]
        theirs = [o for o in s.battlefield if "Artifact" in g.chars(o).types and g.ctrl(o) != p]
        destroy_recorded(g, theirs)
        x = s.turn_flags.get("artifacts_to_gy", 0)
        lib = s.players[p].library
        n = min(x, len(lib))
        exiled = []
        if n > 0:
            g.top_hook(p, n, "exile")
            res = g.move_batch([(o, "exile", {"cause": "effect"}) for o in list(lib[:n])])
            exiled = [r for r in res if r is not None and r in s.objs and s.objs[r].zone == "exile"]
        cands = [o for o in exiled if "Creature" in g.chars(o).types]
        if not cands:
            return
        opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("anz", g.obj_sig(o))) for o in cands]
        a = g.ask(Decision(p, "Search", opts, prompt="You may put a creature card exiled this way onto the battlefield."))
        if a is None:
            return
        nid = g.move(a, "battlefield", controller=p, cause="effect")
        if nid is None or nid not in s.objs or s.objs[nid].zone != "battlefield":
            return
        fx.Pump(_one(nid), None, None, keywords=["haste"], duration="forever").run(g, ctx)
        from .triggers import Step
        ab = Triggered(Step("End", "each"), _ReturnToHandObjs((nid,)), text="Return it to your hand at the beginning of the next end step.")
        s.delayed.append({"trigger": ab.trigger, "ability": ab, "controller": p, "source": ctx.get("source"),
                          "source_chars": ctx.get("source_chars"), "once": True, "data": {}})


# =============================================================================== High Noon

def _high_noon(l, cx):
    """"Each player can't cast more than one spell each turn." (CR 101.2: a "can't" wins). Every
    spell the player cast this turn counts, countered ones and those cast before High Noon entered
    (rulings 2024-04-12); copies aren't cast (707.10)."""
    if l != "Each player can't cast more than one spell each turn.":
        return None
    part = RulePart("cant_cast", "self", who="each",
                    filter_p=lambda g, p, chars: len(g.s.players[p].spells_cast) >= 1)
    return [Static([part], text=l)]


# =============================================================================== Fiery Annihilation

def _fiery_annihilation(m, sc):
    """"~ deals 5 damage to target creature. Exile up to one target Equipment attached to that
    creature. If that creature would die this turn, exile it instead." The Equipment target must be
    attached to the creature target (chosen first, 601.2c; checked again on resolution, 608.2b:
    unattached, it isn't exiled); with the creature an illegal target the Equipment isn't exiled
    (rulings 2024-11-08); the replacement is 614.1a (any way of dying this turn)."""
    cre = et.target_noun("target creature", sc)
    idx = len(sc.tc.specs) - 1

    def attached(g, c, o, src, ctx, idx=idx):
        ctx = ctx or {}
        groups = ctx.get("targets") if ctx.get("targets") is not None else ctx.get("targets_partial")
        if not groups or len(groups) <= idx or not groups[idx]:
            return False
        return o is not None and o.attached_to == groups[idx][0]
    f = Filter(subtypes=["Equipment"], custom=attached, text="Equipment attached to that creature")
    eq = sc.tc.add(TargetSpec("object", filt=f, zone="battlefield", n=(0, 1),
                              text="up to one target Equipment attached to that creature"))
    sc.last = cre
    return Seq(fx.Damage(int(m.group(1)), cre, text="~ deals 5 damage to target creature"),
               fx.If(lambda g, ctx: bool(cre.get(g, ctx)), fx.MoveTo(eq, "exile")),
               fx.DieExileInstead(cre, text="If that creature would die this turn, exile it instead."))


# =============================================================================== Archdruid's Charm

class SearchCreatureOrLand(E):
    """"Search your library for a creature or land card and reveal it. Put it onto the battlefield
    tapped if it's a land card. Otherwise, put it into your hand. Then shuffle." (CR 701.23a; the
    player may fail to find one, 701.23b; a land creature card is a land card)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        s = g.s
        p = ctx["controller"]
        g.emit({"type": "search", "player": p, "library": p})
        f = Filter(any_types=["Creature", "Land"], text="creature or land card")
        cands = [o for o in s.players[p].library if f.match(g, o, ctx.get("source"), ctx)]
        a = None
        if cands:
            opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("srch", g.obj_sig(o))) for o in cands]
            a = g.ask(Decision(p, "Search", opts, prompt=f"Search: {self.text}"))
        if a is not None:
            if "Land" in g.chars(a).types:
                g.move(a, "battlefield", controller=p, tapped=True, cause="effect")
            else:
                g.move(a, "hand", cause="effect")
        g.shuffle(p)


def _bite_by_power(m, sc):
    """"It deals damage equal to its power to target creature you don't control." The creature
    "it" (the first target) is the source; its power as the effect happens (608.2h). If it's no
    longer a legal target, no damage (Archdruid's Charm ruling 2024-02-02)."""
    if sc.last is None:
        raise Unsupported("'it deals damage' without antecedent")
    src = sc.last
    to = et.target_noun(m.group(1), sc)

    def pw(g, ctx, src=src):
        objs = [o for o in src.get(g, ctx) if not isinstance(o, tuple) and o in g.s.objs and g.s.objs[o].zone == "battlefield"]
        return max(0, g.chars(objs[0]).power or 0) if objs else 0
    return fx.Damage(fx.Amount(pw, "its power"), to, source=src, text=m.group(0))


# =============================================================================== Troublemaker Ouphe, offspring

def _paid(g, ctx, name):
    o = _src_obj(g, ctx)
    return bool(o is not None and o.cast and name in (o.cast.get("paid_additional") or ()))


def _condition_hook(t, cx):
    if t in ("it was bargained", "~ was bargained"):
        # CR 702.166b, 400.7d: the permanent knows its spell's bargain cost was paid.
        return lambda g, ctx: _paid(g, ctx, "bargain")
    if t in ("its offspring cost was paid",):
        return lambda g, ctx: _paid(g, ctx, "offspring")
    return None


class OffspringCost(fx.ManaCostPart):
    """CR 702.175a: "You may pay an additional [cost] as you cast this spell" (601.2b, 601.2f:
    part of the total cost)."""
    name = "offspring"


def _one_one(cop):
    cop.power, cop.toughness = 1, 1  # "except it's 1/1": part of the copiable values (707.9b)


def _offspring_keyword(p, cx):
    """CR 702.175a: "Offspring [cost]" means "You may pay an additional [cost] as you cast this
    spell" and "When this permanent enters, if its offspring cost was paid, create a token that's
    a copy of it, except it's 1/1." The token is created even if the permanent left the
    battlefield (its last known information; ruling 2024-07-26)."""
    m = re.fullmatch(r"offspring ((?:\{[^}]+\})+)", p, re.I)
    if not m:
        return None
    if not cx.is_permanent:
        raise Unsupported("offspring on an instant or sorcery")
    cost = fx.ChoiceCost([OffspringCost(parse_cost(m.group(1)))], optional=True, label=lambda i, a: "cost:offspring")
    trig = Triggered(tg.Enters(self_=True), fx.CopyToken(fx.self_lki(), extra=_one_one), [],
                     condition=lambda g, ctx: _paid(g, ctx, "offspring"),
                     text="When this permanent enters, if its offspring cost was paid, create a token that's a copy of it, except it's 1/1.")
    return [Keyword("additional_cost", value=cost, text=p), Keyword("offspring", text=p), trig]


# =============================================================================== Zero Point Ballad, Fumigate

class ZeroPointBallad(E):
    """"Destroy all creatures with toughness X or less. You lose X life. If X is 6 or more, return a
    creature card put into a graveyard this way to the battlefield under your control." (CR
    701.8a; a creature exiled instead, regenerated or indestructible wasn't put into a graveyard
    this way; a token isn't a card; 110.2a)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        s = g.s
        p = ctx["controller"]
        x = ctx.get("x", 0)
        objs = [o for o in s.battlefield if "Creature" in g.chars(o).types
                and g.chars(o).toughness is not None and g.chars(o).toughness <= x]
        done = destroy_recorded(g, objs)
        g.lose_life(p, x, source=ctx.get("source"))
        if x < 6:
            return
        cards = [n for _, n in done if n is not None and n in s.objs and s.objs[n].zone == "graveyard"
                 and not s.objs[n].token and "Creature" in g.chars(n).types]
        if not cards:
            return
        opts = [Opt(f"choose:{g.ref(o)}", o, sig=("zpb", g.obj_sig(o))) for o in cards]
        a = g.ask(Decision(p, "ChooseCard", opts, prompt="Return a creature card put into a graveyard this way."))
        g.move(a, "battlefield", controller=p, cause="effect")


class Fumigate(E):
    """"Destroy all creatures. You gain 1 life for each creature destroyed this way." (CR 701.8a;
    one gain, 119; creatures destroyed but put elsewhere count, indestructible ones aren't
    destroyed: rulings 2024-11-08)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        objs = [o for o in g.s.battlefield if "Creature" in g.chars(o).types]
        done = destroy_recorded(g, objs)
        n = sum(1 for _, nid in done if nid is not None)
        if n:
            g.gain_life(ctx["controller"], n, source=ctx.get("source"))


# =============================================================================== modal headers

def _modal_header(line, cx):
    """Molten Collapse: "Choose one. If you descended this turn, you may choose both instead."
    Wail of the Forgotten: "Descend 8 — Choose one. If there are eight or more permanent cards in
    your graveyard as you cast ~, choose one or more instead." The number of modes is decided as
    the spell is cast (CR 601.2b, 700.2; 700.11)."""
    l = C.strip_ability_word(line)
    if l == "Choose one. If you descended this turn, you may choose both instead.":
        def fix(modes):
            modes.min, modes.max = 1, len(modes.modes)
            modes.max_for = lambda g, p: len(modes.modes) if descended(g, p) else 1
            return modes
        return fix
    m = re.fullmatch(r"Choose one\. If there are (\w+) or more permanent cards in your graveyard as you cast ~, choose one or more instead\.", l)
    if m:
        k = num(m.group(1))

        def fix(modes, k=k):
            def mx(g, p):
                n = sum(1 for o in g.s.players[p].graveyard if PERMANENT_TYPES & g.chars(o).types)
                return len(modes.modes) if n >= k else 1
            modes.min, modes.max = 1, len(modes.modes)
            modes.max_for = mx
            return modes
        return fix
    return None


# =============================================================================== Pawpatch Recruit

@_nc
class YourCreatureBecomesTarget(Trigger):
    """"Whenever a creature you control becomes the target of a spell or ability an opponent
    controls" (CR 603.2e). A spell or ability that targets the same creature more than once
    triggers it once (Pawpatch Recruit ruling 2024-07-26)."""
    events = ("targeted",)

    def __init__(self):
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        r = ev["target"]
        if isinstance(r, tuple) or ev["controller"] == ctrl or r not in g.s.objs:
            return None
        o = g.s.objs[r]
        if o.zone != "battlefield":
            return None
        ch = g.chars(r)
        if "Creature" not in ch.types or ch.controller != ctrl:
            return None
        k = ("became_target_seen", src, ev["item"], r)
        if g.s.turn_flags.get(k):
            return None
        g.s.turn_flags[k] = True
        return {"obj": r, "item": ev["item"], "player": ev["controller"]}


def _counter_other_than_that(m, sc):
    """"put a +1/+1 counter on target creature you control other than that creature" (the creature
    that became the target; no other creature: the trigger is removed, 603.3d)."""
    def other(g, c, o, src, ctx):
        d = (ctx or {}).get("data") or {}
        return o is not None and o.id != d.get("obj")
    f = Filter(types=["Creature"], controller="you", custom=other, text="creature you control other than that creature")
    sel = sc.tc.add(TargetSpec("object", filt=f, zone="battlefield", text="target creature you control other than that creature"))
    sc.last = sel
    return fx.PutCounters(sel, "+1/+1", 1, text=m.group(0))


# =============================================================================== Suki

@_nc
class AnotherYoursLeavesDuringYourTurn(tg.Dies):
    """"Whenever another permanent you control leaves the battlefield during your turn" (CR 603.6c,
    603.10a: looks back; the permanent as it last existed)."""

    def match(self, g, ev, src, c, ctrl):
        if g.s.active != ctrl:
            return None
        return super().match(g, ev, src, c, ctrl)


# =============================================================================== Leyline of Resonance

@_nc
class CastTargetingOnlyYourCreature(Trigger):
    """"Whenever you cast an instant or sorcery spell that targets only a single creature you
    control" (every target of the spell is that one creature; Leyline of Resonance rulings
    2024-09-20)."""
    events = ("cast",)

    def __init__(self):
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev["player"] != ctrl or not ({"Instant", "Sorcery"} & ev["chars"].types):
            return None
        it = next((x for x in g.s.stack if x.id == ev["item"]), None)
        if it is None:
            return None
        flat = [r for grp in (it.targets or []) for r in grp]
        if not flat or any(r != flat[0] for r in flat) or isinstance(flat[0], tuple):
            return None
        o = g.s.objs.get(flat[0])
        if o is None or o.zone != "battlefield":
            return None
        ch = g.chars(flat[0])
        if "Creature" not in ch.types or ch.controller != ctrl:
            return None
        return {"obj": ev["obj"], "player": ev["player"], "item": ev["item"]}


class CopyThatSpell(E):
    """"copy that spell. You may choose new targets for the copy." (CR 707.10, 707.10c: the
    copy is created on the stack, not cast). A
    spell that left the stack before the ability resolved (countered) is still copied, as it last
    existed on the stack (CR 608.2h; the rulings of such "copy that spell" triggers, e.g.
    Thousand-Year Storm 2018-10-05, Swarm Intelligence: "you'll still copy it"). campaign21
    1312212: Get Lost countered by Spell Snare in response to Leyline's trigger; the EUT copied
    it."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        data = ctx.get("data") or {}
        item = data.get("item")
        it = next((x for x in g.s.stack if x.id == item and x.kind == "spell"), None)
        if it is not None and it.obj in g.s.objs:
            fx.copy_spell(g, it, ctx["controller"], True)
            return
        obj = data.get("obj")
        it0, lo = g.s.lki_spell.get(obj), g.s.lki_obj.get(obj)
        if it0 is None or lo is None or lo.zone != "stack":
            return
        fx.copy_spell(g, it0, ctx["controller"], True, lki=lo)


# =============================================================================== Sanctum Lurker

def _sanctum_lurker(l, cx):
    if l == "Planeswalkers you control aren't put into their owners' graveyards for having 0 loyalty.":
        # An exception to the state-based action 704.5i (turn.sba); other effects that remove a
        # planeswalker still apply. Damage removes loyalty counters (120.3c), so a walker damaged
        # to 0 stays too.
        f = Filter(types=["Planeswalker"], controller="you", text="planeswalker you control")
        return [Static([RulePart("zero_loyalty_stays", f)], text=l)]
    m = re.fullmatch(r'Planeswalkers you control have "\[([+−])(\d+)\]: (.+)"', l)
    if m:
        v = int(m.group(2)) * (-1 if m.group(1) == "−" else 1)
        eff, specs, modes = C.compile_effect(m.group(3), cx, ability=True)
        if modes is not None:
            raise Unsupported("granted modal loyalty ability")
        ab = Activated(Cost(loyalty=v), eff, specs, timing="sorcery", text=f"{m.group(1)}{m.group(2)}: {m.group(3)}", loyalty=v)

        def aff(g, oid, c, src):
            return "Planeswalker" in c.types and c.controller == g.ctrl(src)

        def grant(g, oid, c, src, ab=ab):
            c.abilities.append(ab)  # layer 6 (613.1f); after its printed abilities (format §3.1)
        return [Static([LayerPart(6, aff, grant, l)], text=l)]
    return None


# =============================================================================== Purgatory, Endless Horizons, Memory Theft

class PayManaAndLife(E):
    """"Pay {4} and 2 life." (CR 118.3, 119.4: only a player who can pay all of it may)."""
    reports_done = True

    def __init__(self, cost, life, text=""):
        self.cost = list(cost)
        self.life = life
        self.text = text

    def possible(self, g, ctx):
        p = ctx["controller"]
        return g.s.players[p].life >= self.life and g.mana_payable(p, self.cost, 0, {"kind": "unless", "obj": None, "chars": None})

    def run(self, g, ctx):
        p = ctx["controller"]
        if not self.possible(g, ctx):
            return False
        g.pay_mana(p, self.cost, 0, {"kind": "unless", "obj": None, "chars": None}, None)
        g.pay_life(p, self.life)
        return True


class LinkedExiledCard(E):
    """"return a card exiled with ~ to the battlefield" (Purgatory) / "you may put a card you own
    exiled with ~ into your hand" (Endless Horizons): one of the cards in exile linked to this
    object (CR 607.2a, 406.6), chosen as the ability resolves; a permanent put onto the battlefield
    enters under the ability's controller's control (110.2a; Purgatory ruling 2004-10-04)."""

    def __init__(self, zone, owned=False, optional=False, text=""):
        self.zone = zone
        self.owned = owned
        self.optional = optional
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        cands = [o for o in fx.exiled_with_ids(g, ctx.get("source")) if not self.owned or g.s.objs[o].owner == p]
        if not cands:
            return
        opts = ([Opt("done", None)] if self.optional else []) + \
            [Opt(f"choose:{g.ref(o)}", o, sig=("lnk", g.obj_sig(o))) for o in cands]
        a = g.ask(Decision(p, "Search", opts, prompt=self.text))
        if a is None:
            return
        if self.zone == "battlefield":
            g.move(a, "battlefield", controller=p, cause="effect")
        else:
            g.move(a, self.zone, cause="effect")


class SearchAnyNumberExileLinked(E):
    """"search your library for any number of Plains cards, exile them, then shuffle." (CR
    701.23a-b; the cards are exiled face up and linked to this object, 607.2a)."""

    def __init__(self, filt, text=""):
        self.filt = filt
        self.text = text

    def run(self, g, ctx):
        s = g.s
        p = ctx["controller"]
        g.emit({"type": "search", "player": p, "library": p})
        found = []
        while True:
            cands = [o for o in s.players[p].library if o not in found and self.filt.match(g, o, ctx.get("source"), ctx)]
            if not cands:
                break
            opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("srch", g.obj_sig(o))) for o in cands]
            a = g.ask(Decision(p, "Search", opts, prompt=f"Search: {self.text}"))
            if a is None:
                break
            found.append(a)
        if found:
            g.move_batch([(o, "exile", {"cause": "effect", "link": (ctx.get("source"), "exiled")}) for o in found])
        g.shuffle(p)


class AdventureFromExileToGraveyard(E):
    """Memory Theft: "You may put a card that has an Adventure that player owns from exile into
    that player's graveyard." Any face-up exiled card with an Adventure that player owns (rulings
    2019-10-04: not only one cast as an Adventure; even if the hand had no nonland card)."""

    def __init__(self, who, text=""):
        self.who = who
        self.text = text

    def run(self, g, ctx):
        from .model import DB
        me = ctx["controller"]
        for q in fx.players_of(self.who, g, ctx):
            cands = [o for o in g.s.players[q].exile
                     if not g.s.objs[o].face_down and DB.get(g.s.objs[o].card).layout == "adventure"]
            if not cands:
                continue
            opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("adv", g.obj_sig(o))) for o in cands]
            a = g.ask(Decision(me, "Search", opts, prompt=self.text))
            if a is not None:
                g.move(a, "graveyard", cause="effect")


class DigRestToGraveyard(E):
    """"Look at the top three cards of your library. Put one of them into your hand and the rest
    into your graveyard." (CR 701.20e: looked at in the library; the rest go to the graveyard at
    the same time)."""

    def __init__(self, n, k, text=""):
        self.n = n
        self.k = k
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        pl = g.s.players[p]
        n = min(self.n, len(pl.library))
        if n <= 0:
            return
        g.dig_hook(p, n, rest_to="graveyard")
        top = list(pl.library[:n])
        chosen = []
        for _ in range(min(self.k, len(top))):
            cands = [o for o in top if o not in chosen]
            opts = [Opt(f"choose:{g.ref(o)}", o, sig=("dig", g.obj_sig(o))) for o in cands]
            chosen.append(g.ask(Decision(p, "Dig", opts, prompt=f"Choose a card to put into your hand ({self.text})")))
        g.move_batch([(o, "hand", {}) for o in chosen])
        rest = [o for o in top if o not in chosen and o in pl.library]
        if rest:
            g.move_batch([(o, "graveyard", {"cause": "effect"}) for o in rest])


def _memory_theft_last(m, sc):
    if sc.last_player is None:
        raise Unsupported("'that player' without antecedent")
    return AdventureFromExileToGraveyard(sc.last_player, text=m.group(0))


# =============================================================================== triggers

def _trigger_hook(line, cx):
    m = re.match(r"^Whenever a creature you control becomes the target of a spell or ability an opponent controls, (.+)$", line)
    if m:
        return YourCreatureBecomesTarget(), m.group(1)
    m = re.match(r"^Whenever another permanent you control leaves the battlefield during your turn, (.+)$", line)
    if m:
        f = Filter(permanent=True, controller="you", other=True, text="another permanent you control")
        return AnotherYoursLeavesDuringYourTurn(filt=f, to=None), m.group(1)
    m = re.match(r"^Whenever you cast an instant or sorcery spell that targets only a single creature you control, (.+)$", line)
    if m:
        return CastTargetingOnlyYourCreature(), m.group(1)
    m = re.match(r"^Whenever a nontoken creature is put into your graveyard from the battlefield, (.+)$", line)
    if m:
        # "your graveyard": a creature card you own (CR 400.3), whoever controlled it.
        f = Filter(types=["Creature"], token=False, owner="you", text="nontoken creature you own")
        return tg.Dies(filt=f, to="graveyard"), m.group(1)
    return None


# =============================================================================== sentences

et.PATTERNS[0:0] = [
    (r"(Up to one target creature|Target creature) gets ([+-]\d+)/([+-]\d+) until your next turn\.", _pump_until_next_turn),
    (r"Each player sacrifices a creature of their choice\. If you sacrificed a creature this way, create a 4/4 green Beast creature token with trample\.", _garruk_sac),
    (r"Each opponent discards two cards\. For each opponent who didn't discard two nonland cards this way, you draw a card\.",
     lambda m, sc: OpponentsDiscardTwoDrawPer(text=m.group(0))),
    (r"Destroy all artifacts you don't control, then exile the top X cards of your library, where X is the number of artifacts that were put into graveyards from the battlefield this turn\. "
     r"You may put a creature card exiled this way onto the battlefield\. It gains haste\. Return it to your hand at the beginning of the next end step\.",
     lambda m, sc: AnzragsRampage(text=m.group(0))),
    (r"~ deals (\d+) damage to target creature\. Exile up to one target Equipment attached to that creature\. If that creature would die this turn, exile it instead\.", _fiery_annihilation),
    (r"Search your library for a creature or land card and reveal it\. Put it onto the battlefield tapped if it's a land card\. Otherwise, put it into your hand\. Then shuffle\.",
     lambda m, sc: SearchCreatureOrLand(text=m.group(0))),
    (r"It deals damage equal to its power to (target creature you don't control)\.", _bite_by_power),
    (r"Destroy all creatures with toughness X or less\. You lose X life\. If X is 6 or more, return a creature card put into a graveyard this way to the battlefield under your control\.",
     lambda m, sc: ZeroPointBallad(text=m.group(0))),
    (r"Destroy all creatures\. You gain 1 life for each creature destroyed this way\.", lambda m, sc: Fumigate(text=m.group(0))),
    (r"Put a \+1/\+1 counter on target creature you control other than that creature\.", _counter_other_than_that),
    (r"Copy that spell\. You may choose new targets for the copy\.", lambda m, sc: CopyThatSpell(text=m.group(0))),
    (r"Pay ((?:\{[^}]+\})+) and (\d+) life\.", lambda m, sc: PayManaAndLife(parse_cost(m.group(1)), int(m.group(2)), text=m.group(0))),
    (r"Return a card exiled with ~ to the battlefield\.", lambda m, sc: LinkedExiledCard("battlefield", text=m.group(0))),
    (r"You may put a card you own exiled with ~ into your hand\.", lambda m, sc: LinkedExiledCard("hand", owned=True, optional=True, text=m.group(0))),
    (r"Search your library for any number of (Plains|Island|Swamp|Mountain|Forest) cards, exile them, then shuffle\.",
     lambda m, sc: SearchAnyNumberExileLinked(Filter(subtypes=[m.group(1)], text=m.group(1) + " card"), text=m.group(0))),
    (r"You may put a card that has an Adventure that player owns from exile into that player's graveyard\.", _memory_theft_last),
    (r"Look at the top (two|three|four) cards of your library\. Put (one|two) of them into your hand and the rest into your graveyard\.",
     lambda m, sc: DigRestToGraveyard(num(m.group(1)), num(m.group(2)), text=m.group(0))),
]


def _exile_that_card(m, sc):
    """Purgatory: "Whenever a nontoken creature is put into your graveyard from the battlefield,
    exile that card." The card in the graveyard (CR 603.6c: only in the first zone it went to;
    400.7), linked to this object when another ability refers to cards exiled with it (607.2a)."""
    sel = et.trigger_obj(sc, "that card") if sc.last is None else None
    if sel is None:
        raise Unsupported("'that card' without a triggering card")
    return fx.MoveTo(sel, "exile", link="exiled" if getattr(sc.cx, "links_exile", False) else None, text=m.group(0))


# After the existing patterns (a later "Exile that card." with another antecedent stays theirs).
et.PATTERNS.append((r"Exile that card\.", _exile_that_card))


et.MERGE_PAIRS.extend([
    (r"^Each player sacrifices a creature of their choice\.$", r"^If you sacrificed a creature this way, create "),
    (r"^Each opponent discards two cards\.$", r"^For each opponent who didn't discard two nonland cards this way, you draw a card\.$"),
    (r"from the battlefield this turn\.$", r"^You may put a creature card exiled this way onto the battlefield\.$"),
    (r"You may put a creature card exiled this way onto the battlefield\.$", r"^It gains haste\.$"),
    (r"exiled this way onto the battlefield\. It gains haste\.$", r"^Return it to your hand at the beginning of the next end step\.$"),
    (r"^~ deals \d+ damage to target creature\.$", r"^Exile up to one target Equipment attached to that creature\.$"),
    (r"Exile up to one target Equipment attached to that creature\.$", r"^If that creature would die this turn, exile it instead\.$"),
    (r"^Search your library for a creature or land card and reveal it\.$", r"^Put it onto the battlefield tapped if it's a land card\.$"),
    (r"Put it onto the battlefield tapped if it's a land card\.$", r"^Otherwise, put it into your hand\.$"),
    (r"Otherwise, put it into your hand\.$", r"^Then shuffle\.$"),
    (r"^Destroy all creatures with toughness X or less\.$", r"^You lose X life\.$"),
    (r"^Destroy all creatures with toughness X or less\. You lose X life\.$", r"^If X is 6 or more, return a creature card put into a graveyard this way"),
    (r"^Destroy all creatures\.$", r"^You gain 1 life for each creature destroyed this way\.$"),
    (r"^[Cc]opy that spell\.$", r"^You may choose new targets for the copy\.$"),
])

C.KEYWORD_HOOKS.append(_offspring_keyword)
C.MODAL_HEADER_HOOKS.append(_modal_header)
tg.TRIGGER_HOOKS.append(_trigger_hook)
tg.CONDITION_HOOKS.append(_condition_hook)
st.EXTRA_STATICS[0:0] = [_high_noon, _sanctum_lurker]
