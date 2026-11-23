"""Coverage batch 7 (EUT build 15d5447e): Oracle text of the twenty cards the EUT team made
playable, and the rules they need, each implemented from its CR rule.

Mechanics: gift (CR 702.174), paradigm (702.192a, 707.12), recruit (701.70a), face-down exile
and permissions to play the exiled cards (406.3, 406.3a), spending mana as though it were mana of
any type (609.4b), "add N mana in any combination of colors" with a spend restriction (106.6),
player hexproof until end of turn (702.11c), "choose one that hasn't been chosen this turn"
(700.2, 603.3c), first-noncreature-spell triggers, reflexive triggers (603.12), saga chapter
abilities (714.2), copies of spells (707.10, 707.10c) and the token-doubling replacement (614.1a).

Everything registers into the compiler's extension hooks (compiler.KEYWORD_HOOKS, POST_HOOKS;
triggers.TRIGGER_HOOKS, CONDITION_HOOKS; effects_text.PATTERNS and MERGE_PAIRS;
statics.EXTRA_STATICS; call order: compiler.py, above KEYWORD_HOOKS). Strict like the rest of the
compiler: a sentence these patterns don't match exactly still raises Unsupported. Shared with
the other card modules (one implementation each): modal triggers incl. "choose one that hasn't
been chosen this turn" (compiler.compile_modal_trigger), Saga chapters (recent.py Chapter),
token doubling (statics._doubling), "reveals their hand ... exile that card"
(effects_text._reveal_choose / effects.ChooseFromHand), spell copies (effects.copy_spell).
"""
import re

from ..engine import effects as fx
from ..engine.effects import Filter, E, Sel
from ..engine.ir import (Keyword, Triggered, Static, RulePart, ReplacementPart, TargetSpec, Seq,
                         Modes, Mode, SpellAbility, Trigger, Noop)
from ..engine.decisions import Decision, Opt
from ..engine.objects import Obj, StackItem
from .. import Unsupported
from . import compiler as C
from . import triggers as tg
from . import effects_text as et
from . import statics as st
from .nouns import parse_noun, num

KWS = r"(flying|first strike|double strike|deathtouch|haste|hexproof|indestructible|lifelink|menace|reach|trample|vigilance)"


def _nc(cls):
    cls.__deepcopy__ = lambda self, memo: self
    cls.__copy__ = lambda self: self
    return cls


def _src_obj(g, ctx):
    src = ctx.get("source")
    return g.s.objs.get(src) or g.s.lki_obj.get(src)


# =============================================================================== gift (702.174)

GIFTS = {"a card": "card", "a treasure": "Treasure", "a food": "Food", "a tapped fish": "Fish",
         "an octopus": "Octopus"}


@_nc
class GiftCost(fx.CostPart):
    """CR 702.174a: "As an additional cost to cast this spell, you may choose an opponent." In a
    two-player game that opponent is the other player. Paying it promises the gift (702.174k)."""
    name = "gift"

    def payable(self, g, p, oid):
        return True

    def pay(self, g, p, oid, ctx):
        ctx["gift_to"] = 1 - p


def gift_promised(g, ctx):
    """CR 702.174k: the gift was promised if its cost was paid. A spell (and a copy of it, whose
    cast choices are copied, CR 707.10; gift rulings 2024-07-26) knows it from its own choices; a
    permanent from the spell it was (400.7d). A permanent that entered as a copy of another one
    has its own (unpaid) record."""
    if ctx.get("gift_to") is not None or "gift" in (ctx.get("paid_additional") or ()):
        return True
    o = _src_obj(g, ctx)
    return bool(o is not None and o.cast and o.cast.get("gift_to") is not None)


def gift_player(g, ctx):
    if ctx.get("gift_to") is not None:
        return ctx["gift_to"]
    o = _src_obj(g, ctx)
    if o is not None and o.cast and o.cast.get("gift_to") is not None:
        return o.cast["gift_to"]
    return 1 - ctx["controller"]


class GiftGive(E):
    """The gift's effect (CR 702.174d-i): the chosen player draws a card / creates the token."""

    def __init__(self, kind, text=""):
        self.kind = kind
        self.text = text or f"gift {kind}"

    def run(self, g, ctx):
        if not gift_promised(g, ctx):
            return
        q = gift_player(g, ctx)
        if self.kind == "card":
            g.draw(q, 1)  # 702.174e
            return
        if self.kind in ("Treasure", "Food"):
            cd = et.predefined_token(self.kind)()  # 702.174d, 702.174h
            g.create_token(cd, q, 1)
        elif self.kind == "Fish":
            g.create_token(et.parse_token("1/1 blue Fish creature token")(), q, 1, tapped=True)  # 702.174f
        elif self.kind == "Octopus":
            g.create_token(et.parse_token("8/8 blue Octopus creature token")(), q, 1)  # 702.174i
        else:
            raise Unsupported(f"gift {self.kind}")


def _gift_keyword(p, cx):
    m = re.fullmatch(r"gift (a card|a treasure|a food|a tapped fish|an octopus)", p.lower())
    if not m:
        if p.lower().startswith("gift "):
            raise Unsupported(f"gift kind {p!r}")
        return None
    kind = GIFTS[m.group(1)]
    cost = fx.ChoiceCost([GiftCost()], optional=True, label=lambda i, a: "cost:gift")
    out = [Keyword("additional_cost", value=cost, text=p), Keyword("gift", value=kind, text=p)]
    if cx.is_permanent:
        # 702.174b: "When this permanent enters, if its gift cost was paid, [effect]."
        out.append(Triggered(tg.Enters(self_=True), GiftGive(kind), [], condition=gift_promised,
                             text=f"When ~ enters, if its gift cost was paid, gift {kind}."))
    return out


def _gift_spell_post(abilities, cx):
    """702.174b, 702.174j: on an instant or sorcery, "If this spell's gift cost was paid,
    [effect]" happens before the spell's other effects."""
    kind = next((a.value for a in abilities if a.kind == "keyword" and a.keyword == "gift"), None)
    if kind is None or not cx.is_spell:
        return abilities
    out = []
    for a in abilities:
        if a.kind == "spell":
            if a.modes is not None:
                raise Unsupported("modal spell with gift")
            a = SpellAbility(Seq(GiftGive(kind), a.effect), a.targets, text=a.text)
        out.append(a)
    return out


# =============================================================================== paradigm (702.192a)

class _ParadigmCast(E):
    """The delayed trigger's effect: "create a copy of this object in exile. You may cast the copy
    without paying its mana cost" (CR 702.192a). The copy is cast while the ability resolves
    (707.12, 608.2g: no timing restrictions; its targets must be legal, 601.2c). Not cast: it
    ceases to exist (704.5e; ruling 2026-03-20)."""
    text = "paradigm: you may cast a copy"

    def run(self, g, ctx):
        s = g.s
        p = ctx["controller"]
        d = ctx.get("data") or {}
        card, face = d.get("paradigm_card"), d.get("paradigm_face", 0)
        if card is None:
            return
        o = Obj(s.new_id(), card, p, "exile")
        o.is_copy = True
        o.face = face
        o.key = g.new_key(g.base_chars(o).name or card)
        o.timestamp = s.new_ts()
        s.objs[o.id] = o
        s.players[p].exile.append(o.id)
        s.touch()
        if not castable_now(g, p, o.id, "free"):
            return
        opts = [Opt("done", None), Opt(f"choose:{g.ref(o.id)}", o.id)]
        a = g.ask(Decision(p, "ChooseCard", opts, subject=g.ref(o.id),
                           prompt="Paradigm: cast the copy without paying its mana cost (or done)?"))
        if a is not None:
            g.cast(p, o.id, "free", face)


_PARADIGM_ABILITY = Triggered(None, _ParadigmCast(), [], text="paradigm")


def castable_now(g, p, oid, how):
    """A spell an effect lets p cast during a resolution (608.2g): timing permissions don't
    matter; it must have its required targets (601.2c) and not be forbidden (101.2)."""
    c = g.face_chars(oid, g.s.objs[oid].face)
    if g.rule_applies_player(p, "cant_cast", chars=c):
        return False
    from .batch9a import cast_outside_hand_forbidden
    if cast_outside_hand_forbidden(g, p, oid):
        return False  # Avatar's Wrath (101.2)
    f = g.spell_face(oid, g.s.objs[oid].face)
    sa = g.spell_ability(f)
    ctx = {"source": oid, "controller": p, "x": 0, "chars": c, "casting": True}
    if not g.targets_possible(p, f, sa, ctx):
        return False
    if sa is not None and sa.modes is not None and sa.modes.spree:
        # CR 702.172a, 118.9d: "without paying its mana cost" doesn't waive the spree costs: one
        # mode with its targets and a payable cost is needed (Uldaros Theorix's copy of Phantom
        # Interference with every land tapped can't be cast).
        return g.spree_payable_mode(p, oid, c, how, g.s.objs[oid].face, sa, ctx) is not None
    cost, extra = g.spell_cost(p, oid, c, how, g.s.objs[oid].face)
    return g.mana_payable(p, cost, 0, {"kind": "spell", "chars": c, "obj": oid})


class ParadigmEffect(E):
    """CR 702.192a: "If this is the first time a spell you control with this spell's name has
    resolved this game, at the beginning of each of your precombat main phases for the rest of
    the game, create a copy of this object in exile. You may cast the copy without paying its
    mana cost" and "Exile this spell." Every spell with that name has paradigm, so this
    ability's own record of the names it saw resolve tells the first time (a copy of the spell
    resolving first counts too)."""
    text = "paradigm"

    def run(self, g, ctx):
        s = g.s
        p = ctx["controller"]
        src = ctx.get("source")
        o = s.objs.get(src)
        if o is not None:
            name = g.chars(src).name
            pl = s.players[p]
            seen = tuple(pl.flags.get("paradigm_resolved", ()))
            if name not in seen:
                pl.flags["paradigm_resolved"] = seen + (name,)
                from .triggers import Step
                s.delayed.append({"trigger": Step("Main1", "you"), "ability": _PARADIGM_ABILITY,
                                  "controller": p, "source": src, "source_chars": g.chars(src),
                                  "once": False,
                                  "data": {"paradigm_card": o.card, "paradigm_face": o.face}})
        ctx["spell_dest"] = "exile"  # "Exile this spell." (608.2n: instead of the graveyard)


def _paradigm_keyword(p, cx):
    if p.lower() != "paradigm":
        return None
    if not cx.is_spell:
        raise Unsupported("paradigm on a permanent")
    return [Keyword("paradigm", text=p)]


def _paradigm_post(abilities, cx):
    if not any(a.kind == "keyword" and a.keyword == "paradigm" for a in abilities):
        return abilities
    out = []
    for a in abilities:
        if a.kind == "spell":
            if a.modes is not None:
                raise Unsupported("modal spell with paradigm")
            a = SpellAbility(Seq(a.effect, ParadigmEffect()), a.targets, text=a.text)
        out.append(a)
    return out


# =============================================================================== recruit (701.70a)

class Recruit(E):
    """CR 701.70a: "Draw a card, then discard a card. If you discarded a nonland card this way,
    create a 1/1 white Human Soldier creature token." Drawing from an empty library draws
    nothing (the loss is a state-based action, 704.5b); a card is still discarded if possible."""
    text = "recruit"

    def run(self, g, ctx):
        p = ctx["controller"]
        g.draw(p, 1)
        pl = g.s.players[p]
        if not pl.hand:
            return
        opts = [Opt(f"choose:{g.ref(o)}", o, sig=("disc", g.obj_sig(o))) for o in pl.hand]
        o = g.ask(Decision(p, "Discard", opts, prompt="Recruit: choose a card to discard."))
        nonland = "Land" not in g.chars(o).types
        g.discard(p, o, cause="effect")
        if nonland:
            g.create_token(et.parse_token("1/1 white Human Soldier creature token")(), p, 1)


# =============================================================================== triggers

@_nc
class NthNoncreatureCast(Trigger):
    """"Whenever an opponent casts their first noncreature spell each turn": the spell is that
    player's first noncreature spell this turn, counting ones cast before this permanent
    entered (The Queen of Dale ruling 2026-06-29)."""
    events = ("cast",)

    def __init__(self, who, nth):
        self.who = who
        self.nth = nth
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if self.who == "opp" and ev["player"] == ctrl:
            return None
        if self.who == "you" and ev["player"] != ctrl:
            return None
        if "Creature" in ev["chars"].types:
            return None
        if g.s.players[ev["player"]].turn_stats.get("noncreature_cast", 0) != self.nth:
            return None
        return {"obj": ev["obj"], "player": ev["player"], "item": ev["item"]}


@_nc
class Discarded(Trigger):
    """"Whenever you discard a card": once per card (CR 701.9a)."""
    events = ("discard",)

    def __init__(self, who="you"):
        self.who = who
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if self.who == "you" and ev["player"] != ctrl:
            return None
        return {"player": ev["player"], "obj": ev.get("obj")}


def _trigger_hook(line, cx):
    m = re.match(r"^Whenever an opponent casts their first noncreature spell each turn, (.+)$", line)
    if m:
        return NthNoncreatureCast("opp", 1), m.group(1)
    m = re.match(r"^Whenever you discard a card, (.+)$", line)
    if m:
        return Discarded("you"), m.group(1)
    return None


def _condition_hook(t, cx):
    if t in ("the gift was promised", "its gift cost was paid"):
        return gift_promised
    return None


# =============================================================================== statics

def _enters_with_kw_counter(l, cx):
    """"~ enters with a hexproof counter on her." (CR 614.1c, 122.6; a keyword counter gives the
    keyword, 122.1b)."""
    m = re.fullmatch(r"~ enters with an? " + KWS + r" counter on (?:it|him|her)\.", l)
    if not m:
        return None
    kind = m.group(1)

    def apply(g, ev, src, kind=kind):
        cs = ev["opts"].setdefault("counters", {})
        cs[kind] = cs.get(kind, 0) + 1
    p = ReplacementPart("enter_self", lambda g, ev, src: True, apply, text=l)
    p.commutes = True
    return [Static([p], text=l)]


def _threshold(g, src):
    return len(g.s.players[g.ctrl(src)].graveyard) >= 7


def _may_cast_exiled(l, cx):
    """Null Summoner: "As long as there are seven or more cards in your graveyard, you may cast
    the exiled card, and mana of any type can be spent to cast that spell." The exiled card is
    the one its linked ability exiled (CR 607.2a); 609.4b for the mana; normal timing."""
    if l != "As long as there are seven or more cards in your graveyard, you may cast the exiled card, and mana of any type can be spent to cast that spell.":
        return None
    return [Static([RulePart("may_cast_linked", "self", any_type=True)], text=l, condition=_threshold)]


# =============================================================================== effects

class RemoveACounter(E):
    """"Remove a counter from ~": one counter of the kind its controller chooses as the effect applies (CR 608.2d; counters of one kind are interchangeable, 122.1).
    Impossible when ~ has left the battlefield or has no counters (a "you may ... When you do"
    action that can't be done isn't done, 603.12)."""
    reports_done = True

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def _obj(self, g, ctx):
        objs = [o for o in fx.objs_of(self.sel, g, ctx) if g.s.objs[o].zone == "battlefield"]
        return objs[0] if objs else None

    def possible(self, g, ctx):
        oid = self._obj(g, ctx)
        return oid is not None and any(v > 0 for v in g.s.objs[oid].counters.values())

    def run(self, g, ctx):
        oid = self._obj(g, ctx)
        if oid is None:
            return False
        o = g.s.objs[oid]
        kinds = sorted(k for k, v in o.counters.items() if v > 0)
        if not kinds:
            return False
        opts = [Opt(f"counter:{k}", k) for k in kinds]
        k = g.ask(Decision(ctx["controller"], "RemoveCounters", opts, subject=g.ref(oid),
                           prompt="Choose a counter to remove."))
        return g.remove_counters(oid, k, 1) > 0


def _that_player_filter(noun):
    """'<noun> that player controls': controlled by the player of the trigger (the player dealt
    combat damage; trigger data 'player')."""
    f = parse_noun(noun)

    def ctl(g, c, o, src, ctx):
        p = ((ctx or {}).get("data") or {}).get("player")
        return p is not None and c.controller == p
    old = f.custom
    f.custom = ctl if old is None else (lambda g, c, o, src, ctx, a=old: a(g, c, o, src, ctx) and ctl(g, c, o, src, ctx))
    f.text = noun + " that player controls"
    return f


def _destroy_that_player(m, sc):
    f = _that_player_filter(m.group(1))
    sel = sc.tc.add(TargetSpec("object", filt=f, zone="battlefield", text="target " + f.text))
    sc.last = sel
    return fx.Destroy(sel, text=m.group(0))


def _remove_a_counter(m, sc):
    return RemoveACounter(fx.self_permanent(), text=m.group(0))


class GrantPlayPermission(E):
    """"You may cast that card for as long as it remains exiled, and mana of any type can be
    spent to cast it": a permission of the effect's controller on that exiled object (a card
    that leaves exile is a new object, CR 400.7). Normal timing and costs apply (Cruelclaw's
    Heist ruling 2024-07-26)."""

    def __init__(self, sel, any_type=True, lands=False, text=""):
        self.sel = sel
        self.any_type = any_type
        self.lands = lands
        self.text = text

    def run(self, g, ctx):
        for oid in fx.objs_of(self.sel, g, ctx):
            o = g.s.objs.get(oid)
            if o is not None and o.zone == "exile":
                o.flags["may_play"] = {"player": ctx["controller"], "until": None,  # effects.play_permission_valid
                                       "any_type": self.any_type, "lands": self.lands}
        g.s.touch()


class ExileTopFaceDownPlayable(E):
    """Outrageous Robbery: "Target opponent exiles the top X cards of their library face down.
    You may look at and play those cards for as long as they remain exiled. If you cast a spell
    this way, you may spend mana as though it were mana of any type to cast it." (CR 406.3,
    406.3a; 609.4b; lands follow land timing and land plays, ruling 2024-02-02)."""

    def __init__(self, who, n, text=""):
        self.who = who
        self.n = fx.amount(n)
        self.text = text

    def run(self, g, ctx):
        s = g.s
        p = ctx["controller"]
        n = self.n(g, ctx)
        for q in fx.players_of(self.who, g, ctx):
            lib = s.players[q].library
            k = min(n, len(lib))
            if k <= 0:
                continue
            g.top_hook(q, k, "exile")  # harness: the cards the EUT exiled (face down) go on top
            ids = list(lib[:k])
            res = g.move_batch([(o, "exile", {"face_down": True, "cause": "effect"}) for o in ids])
            for r in res:
                if r is not None and r in s.objs:
                    s.objs[r].flags["may_play"] = {"player": p, "until": None, "any_type": True, "lands": True}
        s.touch()


class AddManaCombination(E):
    """"Add N mana in any combination of colors. Spend this mana only to cast <X> spells."
    (CR 106.1a, 106.6): colors chosen one at a time in WUBRG order (each multiset of colors is
    reached by one sequence of picks, format §3); the mana stays in the pool until the step or
    phase ends (106.4)."""

    def __init__(self, n, restrict=None, text=""):
        self.n = n
        self.restrict = restrict
        self.text = text

    def run(self, g, ctx):
        from ..engine.mana import Unit
        p = ctx["controller"]
        last = 0
        cols = "WUBRG"
        for k in range(self.n):
            opts = [Opt(f"color:{c}", c) for c in cols[last:]]
            c = g.ask(Decision(p, "ChooseColor", opts, subject=g.ref(ctx["source"]) if ctx.get("source") in g.s.objs else None,
                               prompt=f"Choose the color of mana {k + 1} of {self.n}."))
            last = cols.index(c)
            g.s.players[p].pool.append(Unit(c, source=ctx.get("source"), restrict=self.restrict))
        g.s.touch()


def _spend_only_subtype_spells(subtype):
    def restrict(g, spend, st=subtype):
        return spend.get("kind") == "spell" and spend.get("chars") is not None and st in spend["chars"].subtypes
    return restrict


class PlayerHexproofEOT(E):
    """"You ... gain hexproof until end of turn" (CR 702.11c; 611.2)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        fx.AddEffect(None, {}, duration="eot", kind="rule",
                     data={"rule": "player_hexproof",
                           "part": RulePart("player_hexproof", None, player=p)}).run(g, ctx)


class GrantAbilities(E):
    """Grant abilities to the selected permanents (layer 6, CR 613.1f) for a duration; the
    affected set is locked as the effect begins (611.2c)."""

    def __init__(self, sel, abilities, duration="eot", text=""):
        self.sel = sel
        self.abilities = tuple(abilities)
        self.duration = duration
        self.text = text

    def run(self, g, ctx):
        def grant(g, oid, c, ce, abil=self.abilities):
            c.abilities.extend(abil)
        fx.AddEffect(self.sel, {6: grant}, duration=self.duration, text=self.text).run(g, ctx)


class NoncreatureCastDamage(E):
    """Magebane Lizard: "~ deals damage to that player equal to the number of noncreature spells
    they've cast this turn" (counted as it resolves, 608.2h; copies aren't cast, 707.10)."""

    def __init__(self, text=""):
        self.text = text
        self.to = fx.trigger_player("player")

    def run(self, g, ctx):
        for r in self.to.get(g, ctx):
            n = g.s.players[r[1]].turn_stats.get("noncreature_cast", 0)
            fx.Damage(n, Sel(lambda gg, cc, r=r: [r], "that player")).run(g, ctx)


class CopySpell(E):
    """"Copy target instant or sorcery spell. You may choose new targets for the copy." (CR
    707.10: the copy has the spell's characteristics and all decisions made for it: modes,
    targets, X, additional and alternative costs (a promised gift too); it isn't cast; 707.10c:
    each target may be left unchanged or changed to a legal one)."""

    def __init__(self, sel, new_targets=True, text=""):
        self.sel = sel
        self.new_targets = new_targets
        self.text = text

    def run(self, g, ctx):
        s = g.s
        for r in self.sel.get(g, ctx):
            it = next((x for x in s.stack if x.kind == "spell" and x.obj == r), None)
            if it is None or r not in s.objs:
                continue
            fx.copy_spell(g, it, ctx["controller"], self.new_targets)


# ---------------------------------------------------------------- sentence handlers

def _target_player_draws_loses(m, sc):
    who = et.player_ref(m.group(1), sc)
    return Seq(fx.Draw(num(m.group(2)), who=who), fx.LoseLife(int(m.group(3)), who, text=m.group(0)))


def _gift_may_cast_exiled(m, sc):
    return fx.If(gift_promised, GrantPlayPermission(fx.var("exiled"), any_type=True, lands=False), text=m.group(0))


def _gift_damage_controller(m, sc):
    if sc.last is None:
        raise Unsupported("'that creature's controller' without antecedent")
    return fx.If(gift_promised, fx.Damage(int(m.group(1)), fx.controller_of(sc.last)), text=m.group(0))


def _dewdrop(m, sc):
    """Dewdrop Cure: up to two targets, or up to three if the gift was promised (CR 702.174m:
    the targets of the "instead" part are chosen only if the gift was promised; the gift is
    announced before targets, 601.2b-c)."""
    if m.group(2) != m.group(4):
        raise Unsupported(f"gift 'instead' text {m.group(0)!r}")
    mv = int(m.group(2))
    f = Filter(types=["Creature"], mv=("<=", mv), owner="you", text=f"creature card with mana value {mv} or less from your graveyard")
    spec = TargetSpec("card", filt=f, zone="graveyard",
                      n=(0, lambda g, ctx: 3 if gift_promised(g, ctx) else 2), text=f.text)
    sel = sc.tc.add(spec)
    sc.last = sel
    return fx.MoveTo(sel, "battlefield", controller="you", text=m.group(0))


def _you_and_permanents_hexproof(m, sc):
    perms = fx.all_matching(Filter(permanent=True, controller="you", text="permanent you control"))
    return Seq(PlayerHexproofEOT(), fx.Pump(perms, None, None, keywords=["hexproof"], text=m.group(0)))


def _gift_permanents_gain(m, sc):
    perms = fx.all_matching(Filter(permanent=True, controller="you", text="permanent you control"))
    return fx.If(gift_promised, fx.Pump(perms, None, None, keywords=[m.group(1)]), text=m.group(0))


def _robbery(m, sc):
    who = et.player_ref(m.group(1), sc)
    return ExileTopFaceDownPlayable(who, et.amount_of(m.group(2), sc), text=m.group(0))


def _add_mana_combination(m, sc):
    n = num(m.group(1))
    restrict = None
    if m.group(2):
        mm = re.fullmatch(r"([A-Z][a-z]+) spells", m.group(2))
        if not mm:
            raise Unsupported(f"spend restriction {m.group(2)!r}")
        restrict = _spend_only_subtype_spells(mm.group(1))
    return AddManaCombination(n, restrict, text=m.group(0))


def _granted_trigger_pump(m, sc):
    """'Until end of turn, target attacking creature gets +2/+2 and gains trample and "Whenever
    this creature deals combat damage to a player, ..."' (CR 611.2c; layer 6 and 7c)."""
    sel = et.target_noun(m.group(1), sc)
    sc.last = sel
    kw = m.group(4)
    if not re.fullmatch(KWS, kw):
        raise Unsupported(f"granted keyword {kw!r}")
    res = C.compile_line(m.group(5), sc.cx)
    if res == "spell" or not res or any(a.kind != "triggered" for a in res):
        raise Unsupported(f"granted ability {m.group(5)!r}")
    return Seq(fx.Pump(sel, int(m.group(2)), int(m.group(3)), keywords=[kw], text=m.group(0)),
               GrantAbilities(sel, res, "eot", text=m.group(5)))


def _return_mv_from_gy(m, sc):
    """'Return target creature card with mana value 3 or less from your graveyard to the
    battlefield' (the noun with its mana value clause before "from your graveyard")."""
    f = parse_noun(m.group(1) + " " + m.group(2))
    f.owner = "you"
    sel = sc.tc.add(TargetSpec("card", filt=f, zone="graveyard", text=m.group(0)))
    sc.last = sel
    dest = "hand" if m.group(3) == "hand" else "battlefield"
    return fx.MoveTo(sel, dest, text=m.group(0), controller="you")


def _copy_spell(m, sc):
    f = et.parse_spell(m.group(1) + " spell")
    sel = sc.tc.add(TargetSpec("spell", filt=f, zone="stack", text=f"target {m.group(1)} spell"))
    return CopySpell(sel, new_targets=bool(m.group(2)), text=m.group(0))


et.PATTERNS[0:0] = [
    (r"(Target player|Target opponent) draws (a|two|three) cards? and loses (\d+) life\.", _target_player_draws_loses),
    (r"If the gift was promised, you may cast that card for as long as it remains exiled, and mana of any type can be spent to cast it\.", _gift_may_cast_exiled),
    (r"If the gift was promised, ~ also deals (\d+) damage to that creature's controller\.", _gift_damage_controller),
    (r"Return up to (two) target creature cards each with mana value (\d+) or less from your graveyard to the battlefield\. "
     r"If the gift was promised, instead return up to (three) target creature cards each with mana value (\d+) or less from your graveyard to the battlefield\.",
     _dewdrop),
    (r"You and permanents you control gain hexproof until end of turn\.", _you_and_permanents_hexproof),
    (r"If the gift was promised, permanents you control also gain (indestructible|hexproof) until end of turn\.", _gift_permanents_gain),
    (r"(Target opponent) exiles the top (X|\d+) cards of their library face down\. You may look at and play those cards for as long as they remain exiled\. "
     r"If you cast a spell this way, you may spend mana as though it were mana of any type to cast it\.", _robbery),
    (r"Add (two|three|four|five) mana in any combination of colors\.(?: Spend this mana only to cast (.+?)\.)?", _add_mana_combination),
    (r"~ deals damage to that player equal to the number of noncreature spells they've cast this turn\.", lambda m, sc: NoncreatureCastDamage(text=m.group(0))),
    (r'Until end of turn, (target .+?) gets \+(\d+)/\+(\d+) and gains (\w[\w ]*?) and "(.+)"\.?', _granted_trigger_pump),
    (r"Destroy target (artifact|artifact or enchantment|creature|enchantment|permanent) that player controls\.", _destroy_that_player),
    (r"Remove a counter from (?:~|her|him|it)\.", _remove_a_counter),
    (r"(?:You )?[Rr]ecruit\.", lambda m, sc: Recruit()),
    (r"Return target ([a-z ]+?) card (with mana value \d+ or (?:less|greater)) from your graveyard to (?:your )?(hand|the battlefield)\.", _return_mv_from_gy),
    (r"Copy target (instant or sorcery) spell\.( You may choose new targets for the copy\.)?", _copy_spell),
]

et.MERGE_PAIRS.extend([
    (r"^Return up to two target creature cards each with mana value \d+ or less from your graveyard to the battlefield\.$",
     r"^If the gift was promised, instead return up to three target"),
    (r"exiles the top (?:X|\d+) cards of their library face down\.$", r"^You may look at and play those cards for as long as they remain exiled\.$"),
    (r"You may look at and play those cards for as long as they remain exiled\.$",
     r"^If you cast a spell this way, you may spend mana as though it were mana of any type to cast it\.$"),
    (r"^Add (?:two|three|four|five) mana in any combination of colors\.$", r"^Spend this mana only to cast [A-Z][a-z]+ spells\.$"),
    (r"^Copy target instant or sorcery spell\.$", r"^You may choose new targets for the copy\.$"),
])

C.KEYWORD_HOOKS.extend([_gift_keyword, _paradigm_keyword])
C.POST_HOOKS.extend([_gift_spell_post, _paradigm_post])
tg.TRIGGER_HOOKS.append(_trigger_hook)
tg.CONDITION_HOOKS.append(_condition_hook)
# Before the general templates ("As long as ..." would try to parse the condition first).
st.EXTRA_STATICS[0:0] = [_enters_with_kw_counter, _may_cast_exiled]
