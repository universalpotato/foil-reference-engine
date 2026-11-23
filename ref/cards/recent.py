"""Oracle templates of recent mechanics and unusual permanents:
 man-lands, chosen basic land types, plot, warp, impending, ninjutsu,
behold, Empower Jace, prepared, conceal, mobilize, collect evidence, Sagas, end the turn.

Everything here registers into the shared compiler tables (static parsers, effect patterns,
keyword parsers) so the shared files only gain small hooks. Rules are cited by CR number.
"""
import re

from ..engine import effects as fx
from ..engine.effects import Filter
from ..engine.ir import Static, LayerPart, ReplacementPart, RulePart, Keyword
from ..engine.decisions import Decision, Opt
from ..cards.model import BASIC_LAND_TYPES
from . import statics as st
from . import effects_text as et
from .nouns import COLORS
from .. import Unsupported

BASIC_TYPES = ("Plains", "Island", "Swamp", "Mountain", "Forest")


# =============================================================================== chosen basic land type

@st.static_parser
def _choose_basic_then_shock(l, cx):
    """Multiversal Passage: "As this land enters, choose a basic land type. Then you may pay 2
    life. If you don't, it enters tapped." One replacement effect (CR 614.1c, 614.12): the type
    choice, then the optional life payment (119.4: only with enough life)."""
    m = re.fullmatch(r"As ~ enters, choose a basic land type\. Then you may pay (\d+) life\. If you don't, it enters tapped\.", l)
    if not m:
        return None
    life = int(m.group(1))

    def apply(g, ev, src, life=life):
        p = ev["opts"].get("controller", ev.get("owner"))
        opts = [Opt(f"type:{t}", t) for t in BASIC_TYPES]
        t = g.ask(Decision(p, "ChooseType", opts, prompt="Choose a basic land type."))
        ev["opts"].setdefault("chosen", {})["type"] = t
        subj = f"key:{g.s.objs[ev['old']].key}" if ev.get("old") in g.s.objs else None
        if g.s.players[p].life >= life:
            a = g.ask(Decision(p, "PayUnless", [Opt("yes", True), Opt("no", False)], subject=subj,
                               prompt=f"Pay {life} life? If you don't, it enters tapped."))
        else:
            a = False
        if a:
            g.pay_life(p, life)
        else:
            ev["opts"]["tapped"] = True
    part = ReplacementPart("enter_self", lambda g, ev, src: True, apply, text=l)
    return [Static([part], text=l)]


@st.static_parser
def _is_the_chosen_type(l, cx):
    """"This land is the chosen type." The static ability sets the land's subtype (layer 4,
    CR 613.1d): CR 305.7, it loses its other land types and the abilities from its rules text
    and gains the chosen basic land type's mana ability (305.6). 613.6: the effect keeps
    applying although the ability generating it is among those removed."""
    if l != "~ is the chosen type.":
        return None
    if "Land" not in cx.types:
        raise Unsupported(f"type-setting static {l!r}")

    def fn(g, oid, c, src):
        from .compiler import subtypes_of
        o = g.s.objs.get(oid)
        t = o.chosen.get("type") if o is not None else None
        if t not in BASIC_LAND_TYPES:
            return  # not on the battlefield with a chosen type (CR 607.2d)
        c.subtypes = (c.subtypes - subtypes_of("Land")) | {t}
        c.abilities = [fx.intrinsic_mana_ability(BASIC_LAND_TYPES[t])]
    return [Static([LayerPart(4, "self", fn, l)], text=l)]


# =============================================================================== man-lands (613.1d-f)

COLOR_LIST = r"((?:(?:white|blue|black|red|green)(?:,? and |, | ))+)"


def _becomes_colored(m, sc):
    """"Until end of turn, ~ becomes a 4/4 blue and black Shark creature with deathtouch. It's
    still a land." Layer 4 (adds the creature type and subtype, keeps the land type: 205.1b),
    layer 5 (sets the colors, 105.3), layer 6 (abilities), layer 7b (sets P/T)."""
    who = m.group(1)
    sel = fx.self_permanent() if who == "~" else et.obj_ref(who[0].lower() + who[1:] if who == "It" else who, sc)
    sc.last = sel
    cols = [COLORS[c] for c in re.findall(r"white|blue|black|red|green", m.group(3))]
    parts = et.parse_becomes(f"a {m.group(2)} {m.group(4)}", sc)

    def color(g, oid, c, ce, cols=frozenset(cols)):
        c.colors = set(cols)
    parts[5] = color
    return fx.AddEffect(sel, parts, duration="eot", text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Until end of turn, (~|It|Target .+?) becomes an? (\d+/\d+) " + COLOR_LIST + r"(.+?)\. It's still an? (?:land|artifact|enchantment)\.", _becomes_colored),
]


# =============================================================================== first spell each turn

def casts_this_turn(g, p):
    """Characteristics of the spells p cast this turn, as each became cast (CR 601.2i), oldest
    first. A scenario that lists spells cast this turn by name only gives no characteristics:
    fail loudly rather than guess."""
    rec = [c for q, c in g.s.turn_flags.get("cast_chars", ()) if q == p]
    if len(rec) != len(g.s.players[p].spells_cast):
        raise Unsupported("spells cast this turn without their characteristics (scenario history)")
    return rec


@st.static_parser
def _first_spell_each_turn_costs_less(l, cx):
    """Momo, Friendly Flier: "The first non-Lemur creature spell with flying you cast during each
    of your turns costs {1} less to cast." A cost reduction (CR 601.2f, generic mana only:
    118.7d/ruling) for a spell cast while its controller has cast no earlier spell of that kind
    this turn. Per the ruling, the first such spell uses it up even if it has no generic mana."""
    m = re.fullmatch(r"The first non-(\w+) creature spell with (\w+) you cast during each of your turns costs \{(\d+)\} less to cast\.", l)
    if not m:
        return None
    sub, kw, n = m.group(1), m.group(2), int(m.group(3))
    from .compiler import SIMPLE_KEYWORDS
    if kw not in SIMPLE_KEYWORDS:
        raise Unsupported(f"spell quality {kw!r}")

    def kind(c, sub=sub, kw=kw):
        return "Creature" in c.types and sub not in c.subtypes and c.has_kw(kw)

    def filt(g, c, oid, src, kind=kind):
        p = g.ctrl(src)
        if g.s.active != p or not kind(c):
            return False
        return not any(kind(x) for x in casts_this_turn(g, p))
    return [Static([RulePart("cost_mod", "self", who="you", n=-n, filter=filt)], text=l)]


# =============================================================================== keyword lines

from ..engine.ir import Activated, Triggered, Cost, Seq, Effect  # noqa: E402
from ..engine.mana import parse_cost  # noqa: E402
from . import compiler as _C  # noqa: E402  (fully defined by the time compiler imports this module)
from . import triggers as tg  # noqa: E402
from .nouns import num  # noqa: E402

_COST = r"((?:\{[^}]+\})+)"


def _plot(p, cx):
    """CR 702.170a: "Plot [cost]" functions in the hand: a special action (116.2k) of the
    card's owner during their main phase while the stack is empty (ref/engine/altcast.py)."""
    m = re.fullmatch(r"plot " + _COST, p, re.I)
    if not m:
        return None
    return [Keyword("plot", value=parse_cost(m.group(1)), text=p)]


def _warp(p, cx):
    """CR 702.185a: "You may cast this card from your hand by paying [cost] rather than its mana
    cost" (an alternative cost, 118.9) and, if it was paid, the permanent is exiled at the
    beginning of the next end step (ref/engine/altcast.py: permanent_resolved)."""
    m = re.fullmatch(r"warp " + _COST, p, re.I)
    if not m:
        return None
    k = Keyword("warp", value=parse_cost(m.group(1)), text=p)
    k.alt_zone = "hand"
    return [k]


def _impending_paid(o):
    return o is not None and (o.cast or {}).get("how") == "impending"


def _impending(p, cx):
    """CR 702.176a: "Impending N—[cost]" represents four abilities: the alternative cost; "If you
    chose to pay this permanent's impending cost, it enters with N time counters on it"; "As long
    as this permanent's impending cost was paid and it has a time counter on it, it's not a
    creature"; "At the beginning of your end step, if this permanent's impending cost was paid
    and it has a time counter on it, remove a time counter from it." The cost was paid if the
    permanent is the spell cast that way (a copy entering wasn't cast: ruling 2024-09-20)."""
    m = re.fullmatch(r"impending (\d+)—" + _COST, p, re.I)
    if not m:
        return None
    n = int(m.group(1))
    k = Keyword("impending", value=parse_cost(m.group(2)), text=p)
    k.alt_zone = "hand"

    def enters_applies(g, ev, src):
        lo = ev.get("lki_obj")
        return ev.get("from") == "stack" and _impending_paid(lo) and not ev["opts"].get("copy_of")

    def enters(g, ev, src, n=n):
        cs = ev["opts"].setdefault("counters", {})
        cs["time"] = cs.get("time", 0) + n
    rep = ReplacementPart("enter_self", enters_applies, enters, text=p + " (enters with time counters)")
    rep.commutes = True

    def not_creature(g, oid, c, src):
        o = g.s.objs.get(oid)
        if _impending_paid(o) and o.counters.get("time", 0) > 0 and "Creature" in c.types:
            fx.set_card_types(c, c.types - {"Creature"})  # 205.1a: its creature types go too
    layer = Static([LayerPart(4, "self", not_creature, p + " (not a creature)")], text=p + " (not a creature)")

    def cond(g, ctx):
        o = g.s.objs.get(ctx.get("source"))
        return _impending_paid(o) and o.counters.get("time", 0) > 0

    def remove(g, ctx):
        o = g.s.objs.get(ctx.get("source"))
        if o is not None and o.zone == "battlefield" and o.counters.get("time", 0) > 0:
            g.remove_counters(o.id, "time", 1)
    t = Triggered(tg.Step("End", "you"), fx.Fn_(remove, "remove a time counter"), condition=cond,
                  text=p + " (end step)")
    t.keyword = "impending"
    return [k, Static([rep], text=p + " (enters with time counters)"), layer, t]


def _unblocked_attacker(g, c, o, src, ctx):
    comb = g.s.combat
    return comb is not None and o is not None and o.id in comb.attackers and comb.blockers_declared \
        and o.id not in comb.blocked


class _Ninjutsu(Effect):
    """702.49a: "Put this card onto the battlefield from your hand tapped and attacking": the
    player, planeswalker or battle the returned creature was attacking (702.49c). A card no
    longer in its owner's hand isn't put onto the battlefield (it's a new object, 400.7)."""

    def run(self, g, ctx):
        s = g.s
        src = ctx.get("source")
        o = s.objs.get(src)
        if o is None or o.zone != "hand":
            return
        ret = ((ctx.get("paid") or {}).get("returned") or [None])[0]
        defender = ret[1] if ret else None
        if defender is not None and not isinstance(defender, tuple):
            d = s.objs.get(defender)
            # 508.4a: the planeswalker or battle it would attack is gone or changed.
            if d is None or d.zone != "battlefield" or not ({"Planeswalker", "Battle"} & g.chars(defender).types):
                defender = None
        nid = g.move(src, "battlefield", controller=ctx["controller"], tapped=True,
                     attacking=defender if s.combat is not None else None, cause="ninjutsu")
        if nid is not None and s.combat is not None and nid in s.combat.attackers:
            if "Creature" not in g.chars(nid).types:
                del s.combat.attackers[nid]  # 506.3a: a noncreature permanent never attacks
            s.touch()


def _ninjutsu(p, cx):
    """CR 702.49a: "Ninjutsu [cost]" means "[Cost], Reveal this card from your hand, Return an
    unblocked attacking creature you control to its owner's hand: Put this card onto the
    battlefield from your hand tapped and attacking." An activated ability that functions only
    in the hand (instant timing)."""
    m = re.fullmatch(r"ninjutsu " + _COST, p, re.I)
    if not m:
        return None
    f = Filter(types=["Creature"], controller="you", custom=_unblocked_attacker,
               text="unblocked attacking creature you control")
    cost = Cost(mana=parse_cost(m.group(1)), return_to_hand=(f, 1), text=p)
    a = Activated(cost, _Ninjutsu(), [], text=p, zones=("hand",))
    a.keyword = "ninjutsu"
    return [a]


class _Mobilize(Effect):
    """CR 702.181a: "Whenever this creature attacks, create N 1/1 red Warrior creature tokens.
    Those tokens enter tapped and attacking. Sacrifice them at the beginning of the next end
    step." 508.4: each token's controller chooses which defending player or planeswalker it
    attacks as it enters (a DeclareAttackers decision per token, `attack:<token>-><defender>`,
    as the EUT asks it). The delayed trigger's source is this creature (603.7e)."""

    def __init__(self, n):
        self.n = n

    def run(self, g, ctx):
        from .model import token_def
        s = g.s
        p = ctx["controller"]
        cd = token_def("Warrior", ("Creature",), ("Warrior",), ("R",), 1, 1)
        toks = g.create_token(cd, p, self.n, tapped=True,
                              attacking=("player", 1 - p) if s.combat is not None else None)
        if s.combat is not None:
            for t in toks:
                if t not in s.combat.attackers:
                    continue
                opts = [Opt(f"attack:{g.ref(t)}->p{1 - p}", ("player", 1 - p))]
                for d in g.defenders(p):
                    if not isinstance(d, tuple):
                        opts.append(Opt(f"attack:{g.ref(t)}->{g.ref(d)}", d, sig=("pw", g.obj_sig(d))))
                if len(opts) > 1:
                    s.combat.attackers[t] = g.ask(Decision(p, "DeclareAttackers", opts, subject=g.ref(t),
                                                           prompt="Choose what this token attacks (CR 508.4)."))
            s.touch()
        ctx.setdefault("vars", {})["token"] = toks
        src = ctx.get("source")
        if getattr(s, "patch_o6_dalkovan", False) and src in s.objs and g.chars(src).name == "Dalkovan Encampment":
            # Opt-in alignment for lockstep runs (State.patch_o6_dalkovan; off by default, never used
            # by ref.corpus): one queue of the Warriors this source made; the sacrifice trigger takes
            # the oldest ones in it. By default "them" are the tokens this resolution made (CR 603.7c).
            key = (src, "o6-dalkovan")
            s.linked[key] = list(s.linked.get(key, [])) + list(toks)
            fx.DelayedAtNextEndStep(fx.CaptureThen(fx.var("token"), lambda ids, key=key: _DalkovanFollow(ids, key))).run(g, ctx)
            return
        if getattr(s, "patch_o4_mobilize", False) and src in s.objs and g.chars(src).name != "Dalkovan Encampment":
            # Opt-in alignment for lockstep runs (State.patch_o4_mobilize; off by default): the same
            # queue for mobilize. By default "them" are the tokens this resolution made (CR 603.7c).
            key = (src, "o4-mobilize")
            s.linked[key] = list(s.linked.get(key, [])) + list(toks)
            fx.DelayedAtNextEndStep(fx.CaptureThen(fx.var("token"), lambda ids, key=key: _DalkovanFollow(
                ids, key, "patch:o4-mobilize-stale"))).run(g, ctx)
            return
        fx.DelayedAtNextEndStep(fx.CaptureThen(fx.var("token"), lambda ids: fx.SacrificeObjs(ids))).run(g, ctx)


class _DalkovanFollow(Effect):
    """Opt-in lockstep alignment (see above): sacrifice the first len(them) Warriors of the source's
    queue (s.linked[key], every Warrior it made, oldest first), which leave the queue whether or not
    they are still on the battlefield."""

    def __init__(self, ids, key, tag="patch:o6-dalkovan-stale"):
        self.ids = ids
        self.key = key
        self.tag = tag  # the tag it reports

    def run(self, g, ctx):
        q = list(g.s.linked.get(self.key, []))
        n = len(self.ids)
        ids, rest = tuple(q[:n]), q[n:]
        if rest:
            g.s.linked[self.key] = rest
        else:
            g.s.linked.pop(self.key, None)
        if set(ids) != set(self.ids):
            hook = getattr(getattr(g, "hooks", None), "patch_hit", None)
            if hook is not None:
                hook(self.tag)
        fx.SacrificeObjs(ids).run(g, ctx)


def _mobilize(p, cx):
    m = re.fullmatch(r"mobilize (\d+)", p, re.I)
    if not m:
        return None
    t = Triggered(tg.Attacks(self_=True), _Mobilize(int(m.group(1))), text=p)
    t.keyword = "mobilize"
    return [t]


_C.KEYWORD_HOOKS.extend([_plot, _warp, _impending, _ninjutsu, _mobilize])


def _cards_in(g, p, zone):
    """The cards in a player's zone: a token or a copy of a card that has just been put there
    (it ceases to exist at the next state-based action check, 704.5d-e) isn't a card (108.2,
    111.1), so "a card from a graveyard" can't be it."""
    return [x for x in g.s.players[p].zone(zone) if not (g.s.objs[x].token or g.s.objs[x].is_copy)]


# =============================================================================== prepared (722)

def _can_be_prepared(g, ev):
    """CR 722.3a: only a permanent with a prepare spell; the prepare spell is part of the
    copiable values (722.2b), so a permanent entering as a copy of a preparation card has one
    (Superior Spider-Man copying Emeritus of Ideation)."""
    from .model import DB
    cop = ev["opts"].get("copy_of")
    if cop is not None:
        return getattr(cop, "prepare", None) is not None
    # A token copy of a permanent that is itself a copy of a
    # preparation card (The Fire Crystal copying Superior Spider-Man as a copy of Emeritus of
    # Ideation): the token's copiable values are the copied permanent's (707.2, 707.3), which
    # include that prepare spell (722.2b), whatever card the token was created from.
    tok = ev.get("token_obj")
    if tok is not None and tok.flags.get("copiable") is not None:
        return getattr(tok.flags["copiable"], "prepare", None) is not None
    lo = ev.get("lki_obj")
    if lo is not None and lo.flags.get("copiable") is not None:
        return getattr(lo.flags["copiable"], "prepare", None) is not None
    return DB.get(ev["card"]).layout == "prepare"


@st.static_parser
def _enters_prepared(l, cx):
    """"This creature enters prepared." (CR 722.3a: a permanent with a prepare spell gains the
    prepared designation as it enters; a replacement effect, 614.1c). Its controller may then
    cast the copy of its prepare spell (722.3c, ref/engine/altcast.py)."""
    if l != "~ enters prepared.":
        return None

    def apply(g, ev, src):
        if not _can_be_prepared(g, ev):
            # Every copy whose copiable values hold "enters prepared" also holds the prepare
            # spell (722.2b; _can_be_prepared reads the token's or entering copy's values), so
            # this is a path not read there: fail loudly rather than guess (722.3a).
            raise Unsupported("a copy entering prepared (CR 722.2b, 722.3a)")
        ev["opts"]["prepared"] = True
    part = ReplacementPart("enter_self", lambda g, ev, src: True, apply, text=l)
    part.commutes = True
    return [Static([part], text=l)]


def _becomes_prepared(m, sc):
    return fx.Fn_(lambda g, ctx: g.become_prepared(ctx.get("source")), "becomes prepared")


class _ExileFromGraveyard(Effect):
    """"Exile N cards from your graveyard" (Emeritus of Ideation, as the action of "you may ...
    If you do"): the player chooses the cards (one pick at a time, PayCost). Returns False (not
    done) if there aren't N cards, so the player can't choose to do it (608.2c)."""

    def __init__(self, n):
        self.n = n

    def possible(self, g, ctx):
        return len(_cards_in(g, ctx["controller"], "graveyard")) >= self.n

    def run(self, g, ctx):
        p = ctx["controller"]
        gy = _cards_in(g, p, "graveyard")
        if len(gy) < self.n:
            return False
        chosen = []
        if len(gy) == self.n:
            chosen = gy
        else:
            while len(chosen) < self.n:
                cands = [x for x in gy if x not in chosen]
                opts = [Opt(f"choose:{g.ref(x)}", x, sig=("ex", g.obj_sig(x))) for x in cands]
                # Kind PayCost: the EUT's label for this choice (the format's choose:<ref> family
                # leaves it open, §3.1), so the harness matches its records by kind.
                chosen.append(g.ask(Decision(p, "PayCost", opts, prompt=f"Exile {self.n} cards from your graveyard.")))
        g.move_batch([(x, "exile", {}) for x in chosen])
        return True


et.PATTERNS[0:0] = [
    (r"~ becomes prepared\.", _becomes_prepared),
    (r"Exile (two|three|four|five|six|seven|eight|nine|ten) cards from your graveyard\.",
     lambda m, sc: _ExileFromGraveyard(num(m.group(1)))),
]


# =============================================================================== Kaito, Voice, Overlord

@st.static_parser
def _during_your_turn_creature(l, cx):
    """Kaito, Bane of Nightmares: "During your turn, as long as ~ has one or more loyalty counters
    on him, he's a 3/4 Ninja creature and has hexproof." Layer 4 sets his card type to creature
    (205.1a: he stops being a planeswalker, ruling 2024-09-20; the planeswalker subtype goes) with
    the creature type Ninja; layer 6 hexproof; layer 7b 3/4. A condition (611.3a)."""
    m = re.fullmatch(r"During your turn, as long as ~ has one or more (\w+) counters on (?:him|her|it), (?:he's|she's|it's) an? (\d+)/(\d+) ((?:[A-Z][a-z]+ )*)creature and has (\w+)\.", l)
    if not m:
        return None
    kind, p, t, subs, kw = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split(), m.group(5)
    from .compiler import SIMPLE_KEYWORDS
    if kw not in SIMPLE_KEYWORDS:
        raise Unsupported(f"granted ability {kw!r}")

    def typ(g, oid, c, src, subs=tuple(subs)):
        from .compiler import subtypes_of
        fx.set_card_types(c, {"Creature"})
        c.subtypes = (c.subtypes - subtypes_of("Creature")) | set(subs)

    def grant(g, oid, c, src, k=Keyword(kw)):
        c.abilities.append(k)

    def pt(g, oid, c, src, p=p, t=t):
        c.power, c.toughness = p, t

    def cond(g, src, kind=kind):
        o = g.s.objs.get(src)
        return o is not None and g.s.active == g.ctrl(src) and o.counters.get(kind, 0) > 0
    return [Static([LayerPart(4, "self", typ, l), LayerPart(6, "self", grant, l), LayerPart("7b", "self", pt, l)],
                   text=l, condition=cond)]


@st.static_parser
def _opponents_cant_cast_during_your_turn(l, cx):
    """Voice of Victory: "Your opponents can't cast spells during your turn." (CR 101.2)"""
    if l != "Your opponents can't cast spells during your turn.":
        return None
    return [Static([RulePart("cant_cast", "self", who="opp")], text=l,
                   condition=lambda g, src: g.s.active == g.ctrl(src))]


def _draw_per_opponent_lost_life(m, sc):
    """"Draw a card for each opponent who lost life this turn." (Kaito: whether they lost life,
    not how their life total changed, ruling 2024-09-20)."""
    def fn(g, ctx):
        p = ctx["controller"]
        n = sum(1 for q in (0, 1) if q != p and g.s.players[q].life_lost > 0)
        if n:
            g.draw(p, n)
    return fx.Fn_(fn, m.group(0))


class _ReturnFromGraveyardChoice(Effect):
    """"You may return a <A> card or a <B> card from your graveyard to your hand." Not targeted:
    the card is chosen on resolution (608.2c). Asked as the EUT does: OptionalEffect yes/no, then
    the card (Search: choose:<card>, done = none). With no such card in the graveyard, returning
    one is impossible, so "yes" can't be chosen (CR 608.2d) and nothing is asked (Overlord of the
    Balemurk ruling 2024-09-20: any such card in the graveyard, not only a milled one). Until
    2026-11-08 this asked anyway, as the EUT did before 6851b5b7 (harness: `_impossible_optional`)."""

    def __init__(self, filters, text):
        self.filters = filters
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        subj = g.ref(ctx["source"]) if ctx.get("source") in g.s.objs else None
        cands = [x for x in _cards_in(g, p, "graveyard")
                 if any(f.match(g, x, ctx.get("source"), ctx) for f in self.filters)]
        if not cands:
            return
        if not g.ask(Decision(p, "OptionalEffect", [Opt("yes", True), Opt("no", False)], subject=subj,
                              prompt=f"Use the optional effect? {self.text}")):
            return
        opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(x)}", x, sig=("rc", g.obj_sig(x))) for x in cands]
        x = g.ask(Decision(p, "Search", opts, subject=subj, prompt="Choose a card to return to your hand."))
        if x is not None:
            g.move(x, "hand", cause="effect")


def _return_card_choice(m, sc):
    from .nouns import parse_noun
    fs = []
    for noun in (m.group(1), m.group(2)):
        f = parse_noun(noun)
        f.owner = "you"
        fs.append(f)
    return _ReturnFromGraveyardChoice(fs, m.group(0))


def _mill_then(m, sc):
    a = et.parse_one(f"Mill {m.group(1)} cards.", sc)
    rest = m.group(2)
    b = et.parse_one(rest[0].upper() + rest[1:] + ".", sc)
    return Seq(a, b)


et.PATTERNS[0:0] = [
    (r"Draw a card for each opponent who lost life this turn\.", _draw_per_opponent_lost_life),
    (r"You may return an? (.+?) card or an? (.+?) card from your graveyard to your hand\.", _return_card_choice),
    (r"Mill (\w+) cards, then (.+)\.", _mill_then),
]


# =============================================================================== conceal / reveal modes

def _mode_name(m, sc):
    """A mode's name before " — " ("Conceal — ...", "Reveal — ..."): a flavor word (CR 207.2d)
    with no rules meaning. Keyword abilities and ability words aren't mode names."""
    head = m.group(1)
    if head.lower() in _C._KEYWORD_NAMES or head in _C.ABILITY_WORDS:
        raise Unsupported(f"keyword before a dash in a mode: {head!r}")
    return et.parse_one(m.group(2), sc)


def _becomes_subtype_base_pt(m, sc):
    """"<target> becomes a Citizen with base power and toughness 1/1 and gains hexproof until
    end of turn." The creature type is set (205.1a: it replaces the creature types; still a
    creature), base P/T set in layer 7b (613.4b), the keywords granted in layer 6."""
    from .compiler import _CREATURE_TYPES, subtypes_of
    sub = m.group(2)
    if sub not in _CREATURE_TYPES:
        raise Unsupported(f"becomes {sub!r}")
    sel = et.obj_ref(m.group(1), sc)
    sc.last = sel
    p, t = int(m.group(3)), int(m.group(4))
    kws = [k.strip() for k in re.split(r",? and |, ", m.group(5))]
    from .compiler import SIMPLE_KEYWORDS
    if any(k not in SIMPLE_KEYWORDS for k in kws):
        raise Unsupported(f"gains {m.group(5)!r}")

    def typ(g, oid, c, ce, sub=sub):
        c.subtypes = (c.subtypes - subtypes_of("Creature")) | {sub}

    def pt(g, oid, c, ce, p=p, t=t):
        c.power, c.toughness = p, t

    def grant(g, oid, c, ce, ks=tuple(Keyword(k) for k in kws)):
        c.abilities.extend(ks)
    return fx.AddEffect(sel, {4: typ, 6: grant, "7b": pt}, duration="eot", text=m.group(0))


et.PATTERNS[0:0] = [
    (r"(Target .+?) becomes an? ([A-Z][a-z]+) with base power and toughness (\d+)/(\d+) and gains (.+?) until end of turn\.", _becomes_subtype_base_pt),
    # One or more capitalised words: "Conceal — ...", "Solar Beam — ..." (The Vision).
    (r"([A-Z][a-z']+(?: [A-Z][a-z']+)*) — (.+)", _mode_name),
]


# =============================================================================== connive, anthems

class _Connive(Effect):
    """CR 701.50a: "To connive, that permanent's controller draws a card, then discards a card. If
    a nonland card is discarded this way, that player puts a +1/+1 counter on the conniving
    permanent." 701.50b: if it left the battlefield, its last known information says who
    controlled it (the draw and discard still happen; no counter)."""

    def run(self, g, ctx):
        s = g.s
        src = ctx.get("source")
        o = s.objs.get(src)
        on_bf = o is not None and o.zone == "battlefield"
        if on_bf:
            p = g.ctrl(src)
        else:
            lki = s.lki.get(src)
            p = lki.controller if lki is not None else ctx["controller"]
        g.draw(p, 1)
        hand = s.players[p].hand
        nid = None
        if hand:
            opts = [Opt(f"choose:{g.ref(x)}", x, sig=("disc", g.obj_sig(x))) for x in hand]
            x = g.ask(Decision(p, "Discard", opts, prompt="Choose a card to discard (connive)."))
            land = "Land" in g.chars(x).types
            nid = g.discard(p, x, cause="effect")
            if not land and on_bf and src in s.objs and s.objs[src].zone == "battlefield":
                g.add_counters(src, "+1/+1", 1, source=src)
        g.emit({"type": "connived", "obj": src, "player": p})


def _connives(m, sc):
    return _Connive()


et.PATTERNS[0:0] = [
    (r"~ connives\.", _connives),
]


@st.static_parser
def _opponents_anthem(l, cx):
    """"Creatures your opponents control get -1/-1." (layer 7c, CR 613.4c; the affected set
    changes as permanents enter or change control, 611.3a)."""
    m = re.fullmatch(r"(.+?) your opponents control get ([+\-]\d+)/([+\-]\d+)\.", l)
    if not m:
        return None
    from .nouns import parse_noun
    f = parse_noun(m.group(1)[0].lower() + m.group(1)[1:] + " your opponents control")
    return [st._anthem(f, int(m.group(2)), int(m.group(3)), [], l)]


# =============================================================================== The Theorist

def _for_each_opponent_bounce(m, sc):
    """"For each opponent, return up to one target artifact or creature that player controls to
    its owner's hand." One target word per opponent (CR 601.2c); two players: up to one target
    the opponent controls."""
    sel = et.target_noun(f"up to one target {m.group(1)} an opponent controls", sc)
    sc.last = sel
    return fx.MoveTo(sel, "hand", text=m.group(0))


def _where_x_cards_in_hand(m, sc):
    """"..., where X is the number of cards in your hand." (counted as the effect happens)."""
    expr = fx.Amount(lambda g, ctx: len(g.s.players[ctx["controller"]].hand), "cards in your hand")
    old, old_used = getattr(sc, "x_expr", None), getattr(sc, "x_used", False)
    sc.x_expr, sc.x_used = expr, False
    try:
        eff = et.parse_one(m.group(1) + ".", sc)
        if not sc.x_used:
            raise Unsupported(f"'where X is' not bound in {m.group(1)!r}")
        return eff
    finally:
        sc.x_expr, sc.x_used = old, old_used


et.PATTERNS[0:0] = [
    (r"For each opponent, return up to one target (.+?) that player controls to its owner's hand\.", _for_each_opponent_bounce),
    (r"(.+?), where X is the number of cards in your hand\.", _where_x_cards_in_hand),
]


# =============================================================================== behold (701.4)

def behold_candidates(g, p, sub, exclude=None):
    """CR 701.4a: "Behold a [quality]" means "Reveal a [quality] card from your hand or choose a
    [quality] permanent you control on the battlefield"."""
    s = g.s
    out = [x for x in s.battlefield if x != exclude and g.ctrl(x) == p and sub in g.chars(x).subtypes]
    out += [x for x in s.players[p].hand if x != exclude and sub in g.chars(x).subtypes]
    return out


def behold(g, p, sub, subject, exclude=None):
    """Choose what is beheld (PayCost: choose:<ref>, as the EUT asks it). Returns its id."""
    cands = behold_candidates(g, p, sub, exclude)
    opts = [Opt(f"choose:{g.ref(x)}", x, sig=("behold", g.s.objs[x].zone, g.obj_sig(x))) for x in cands]
    x = g.ask(Decision(p, "PayCost", opts, subject=subject, prompt=f"Behold a {sub} (CR 701.4a)."))
    g.emit({"type": "beheld", "player": p, "obj": x})
    return x


class BeholdCost(fx.CostPart):
    """"behold a Jace" as (part of) an additional cost (CR 118.8, 601.2b/h)."""
    name = "behold"

    def __init__(self, sub):
        self.sub = sub

    def payable(self, g, p, oid):
        return bool(behold_candidates(g, p, self.sub, exclude=oid))

    def pay(self, g, p, oid, ctx):
        ctx.setdefault("paid", {})["beheld"] = behold(g, p, self.sub, g.ref(oid), exclude=oid)


class _Behold(Effect):
    """"Behold a Dragon." as an effect (the action of "you may ... If you do", 608.2c)."""

    def __init__(self, sub):
        self.sub = sub

    def possible(self, g, ctx):
        return bool(behold_candidates(g, ctx["controller"], self.sub))

    def run(self, g, ctx):
        if not self.possible(g, ctx):
            return False
        src = ctx.get("source")
        behold(g, ctx["controller"], self.sub, g.ref(src) if src in g.s.objs else None)
        return True


@st.static_parser
def _behold_or_enters_tapped(l, cx):
    """Theorist's Sanctum: "As this land enters, you may behold a Jace. If you don't, this land
    enters tapped." (614.1c, 614.12): asked only if there's something to behold."""
    m = re.fullmatch(r"As ~ enters, you may behold an? (\w+)\. If you don't, ~ enters tapped\.", l)
    if not m:
        return None
    sub = m.group(1)

    def apply(g, ev, src, sub=sub):
        p = ev["opts"].get("controller", ev.get("owner"))
        old = ev.get("old")
        subj = f"key:{g.s.objs[old].key}" if old in g.s.objs else None
        if behold_candidates(g, p, sub, exclude=old) and g.ask(Decision(
                p, "PayUnless", [Opt("yes", True), Opt("no", False)], subject=subj,
                prompt=f"Behold a {sub}? If you don't, it enters tapped.")):
            behold(g, p, sub, subj, exclude=old)
        else:
            ev["opts"]["tapped"] = True
    part = ReplacementPart("enter_self", lambda g, ev, src: True, apply, text=l)
    return [Static([part], text=l)]


et.PATTERNS[0:0] = [
    (r"Behold an? ([A-Z][a-z]+)\.", lambda m, sc: _Behold(m.group(1))),
]


# =============================================================================== Empower Jace (701.71)

def _jace_tokens(g, p):
    return [x for x in g.s.battlefield if g.ctrl(x) == p and g.s.objs[x].token
            and "Planeswalker" in g.chars(x).types and "Jace" in g.chars(x).subtypes]


class _EmpowerJace(Effect):
    """CR 701.71a: "If you don't control a Jace planeswalker token, create a blue Jace
    planeswalker token with 0 loyalty, '[−1]: Surveil 1,' and '[−3]: Draw a card.' Choose a Jace
    planeswalker token you control. Put N loyalty counters on it." (Token named "Jace Token",
    111.4; a token doubler makes two, then one is chosen.)"""

    def __init__(self, n):
        self.n = n

    def run(self, g, ctx):
        from .model import token_def
        p = ctx["controller"]
        if not _jace_tokens(g, p):
            cd = token_def("Jace", ("Planeswalker",), ("Jace",), ("U",), text="−1: Surveil 1.\n−3: Draw a card.")
            g.create_token(cd, p, 1)
        cands = _jace_tokens(g, p)
        if not cands:
            return
        if len(cands) == 1:
            x = cands[0]
        else:
            opts = [Opt(f"choose:{g.ref(c)}", c, sig=("jace", g.obj_sig(c))) for c in cands]
            x = g.ask(Decision(p, "ChooseCard", opts, prompt="Choose a Jace planeswalker token you control (CR 701.71a)."))
        g.add_counters(x, "loyalty", self.n, source=ctx.get("source"))


et.PATTERNS[0:0] = [
    (r"Empower Jace (\d+)\.", lambda m, sc: _EmpowerJace(int(m.group(1)))),
]


def _loyalty_any_time(m, sc):
    """Jace's Machinations: "Until end of turn, you may activate loyalty abilities of Jace
    planeswalkers you control on any player's turn any time you could cast an instant." A rule
    effect for the turn (CR 606.3: still once each turn per permanent)."""
    sub = m.group(1)
    part = RulePart("loyalty_any_time", None, subtype=sub)
    return fx.AddEffect(None, {}, duration="eot", kind="rule", text=m.group(0),
                        data={"rule": "loyalty_any_time", "part": part})


et.PATTERNS[0:0] = [
    (r"(?:You may a|A)ctivate loyalty abilities of ([A-Z][a-z]+) planeswalkers you control on any player's turn any time you could cast an instant until end of turn\.", _loyalty_any_time),
]


# =============================================================================== target spell or creature

def _bounce_spell_or_creature(m, sc):
    """"Return target spell or creature to its owner's hand": one target word whose candidates
    are spells on the stack and creatures on the battlefield (CR 115.1)."""
    from ..engine.ir import TargetSpec

    def ok(g, c, o, src, ctx):
        return o is not None and (o.zone == "stack" or (o.zone == "battlefield" and "Creature" in c.types))
    spec = TargetSpec("object", filt=Filter(custom=ok, text="spell or creature"), zone="stack|battlefield",
                      text="target spell or creature")
    sel = sc.tc.add(spec)
    sc.last = sel
    return fx.MoveTo(sel, "hand", text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Return target spell or creature to its owner's hand\.", _bounce_spell_or_creature),
]


# =============================================================================== Sarkhan, Elspeth

def _becomes_type_and_gains(m, sc):
    """"~ becomes a Dragon in addition to his other types and gains flying until end of turn."
    (205.1b: keeps its types; layer 4 and layer 6)."""
    sel = fx.self_permanent() if m.group(1) == "~" else et.obj_ref(m.group(1).lower(), sc)
    sub = m.group(2)
    kws = [k.strip() for k in re.split(r",? and |, ", m.group(3))]
    from .compiler import SIMPLE_KEYWORDS
    if any(k not in SIMPLE_KEYWORDS for k in kws):
        raise Unsupported(f"gains {m.group(3)!r}")

    def typ(g, oid, c, ce, sub=sub):
        c.subtypes.add(sub)

    def grant(g, oid, c, ce, ks=tuple(Keyword(k) for k in kws)):
        c.abilities.extend(ks)
    return fx.AddEffect(sel, {4: typ, 6: grant}, duration="eot", text=m.group(0))


def _those_gain_until_next_turn(m, sc):
    if sc.last is None:
        raise Unsupported("'Those creatures' without antecedent")
    kws = [k.strip() for k in re.split(r",? and |, ", m.group(1))]
    from .compiler import SIMPLE_KEYWORDS
    if any(k not in SIMPLE_KEYWORDS for k in kws):
        raise Unsupported(f"gains {m.group(1)!r}")
    # "until your next turn" ends as its controller's next turn begins (CR 611.2b).
    return fx.Pump(sc.last, None, None, duration="until_your_next_turn", keywords=kws, text=m.group(0))


et.PATTERNS[0:0] = [
    (r"(~|It) becomes an? ([A-Z][a-z]+) in addition to (?:its|his|her) other types and gains (.+?) until end of turn\.", _becomes_type_and_gains),
    (r"Those creatures gain (.+?) until your next turn\.", _those_gain_until_next_turn),
]


# =============================================================================== teamwork (702.194)

class _TeamworkTap(fx.CostPart):
    """The creatures chosen for teamwork, tapped as the costs are paid (601.2h). `reserve`: they
    can't also tap for mana in this payment (118.3)."""
    name = "teamwork"

    def __init__(self, oids):
        self.oids = tuple(oids)
        self.reserve = set(oids)

    def payable(self, g, p, oid):
        return all(x in g.s.objs and not g.s.objs[x].tapped for x in self.oids)

    def pay(self, g, p, oid, ctx):
        for x in self.oids:
            g.tap(x)


class TeamworkChoice(fx.ChoiceCost):
    """CR 702.194a: "Teamwork N" means "As an additional cost to cast this spell, you may tap any
    number of creatures you control with total power N or more." Announced at 601.2b (AdditionalCost
    cost:teamwork / no); the creatures are chosen right away (PayCost choose:<ref>, one at a time,
    any remaining one each time, done once their total power is N or more) and kept out of the
    mana payment."""
    name = "teamwork"

    def __init__(self, n):
        super().__init__([self], optional=True, label=lambda i, a: "cost:teamwork")
        self.n = n

    def _cands(self, g, p, exclude=()):
        return [x for x in g.s.battlefield if g.ctrl(x) == p and x not in exclude and not g.s.objs[x].tapped
                and "Creature" in g.chars(x).types]

    def payable(self, g, p, oid):
        return True  # optional

    def _mana_ok(self, g, p, oid, tapped):
        """Is the spell's mana cost still payable with `tapped` kept out of the payment (they're
        tapped for this cost, so not for mana nor as a Springleaf Drum's helper; CR 601.2g-h,
        118.3)? (`casting.cast` keeps the cost being announced in `_base_cost`.)"""
        cost = getattr(g, "_base_cost", None)
        if cost is None or oid not in g.s.objs:
            return True
        spend = {"kind": "spell", "chars": g.chars(oid), "obj": oid}
        return g.mana_payable(p, cost, 0, spend, exclude=tuple(tapped))

    def _completable(self, g, p, oid, chosen, cands):
        """Can `chosen` grow into a set of creatures with total power N or more whose tapping
        leaves the mana payable (format §3: only completable options; a cost that can't then be
        paid can't be announced, 601.2h)? Only minimal sets need checking: tapping more only
        takes mana sources away."""
        import itertools
        pw = lambda x: max(0, g.chars(x).power or 0)  # noqa: E731
        base = sum(pw(x) for x in chosen)
        if base >= self.n:
            return self._mana_ok(g, p, oid, chosen)
        need = self.n - base
        rest = [x for x in cands if x not in chosen and pw(x) > 0]
        for k in range(1, min(len(rest), need) + 1):
            for extra in itertools.combinations(rest, k):
                if base + sum(pw(x) for x in extra) >= self.n and self._mana_ok(g, p, oid, list(chosen) + list(extra)):
                    return True
        return False

    def choose(self, g, p, oid, ctx, allowed=None):
        opts = []
        cands0 = self._cands(g, p, getattr(g, "_excluded", ()))
        if self._completable(g, p, oid, [], cands0):
            # Only when the spell stays castable with teamwork paid (campaign13 837297: Drake
            # Hatcher was also the creature Springleaf Drum needed for the {U}).
            opts.append(Opt("cost:teamwork", True))
        opts.append(Opt("no", False))
        if not g.ask(Decision(p, "AdditionalCost", opts, subject=g.ref(oid), prompt="Pay the teamwork cost?")):
            return None
        # Every remaining creature is a pick (format §3: the reference engine offers every
        # remaining pick; any set with total power N or more may be tapped), while the total can
        # still reach N. (A fixed name order, meant to follow the EUT's, cut off creatures the
        # EUT's own order puts later: campaign13 834227, a Demon token after Invasion
        # Reinforcements; its unrecorded forced picks are the harness's to infer.)
        order = sorted(cands0, key=lambda x: (g.chars(x).name or "", repr(g.obj_sig(x)), x))
        chosen, total = [], 0
        while True:
            rest = [x for x in order if x not in chosen]
            opts = []
            if total >= self.n and self._mana_ok(g, p, oid, chosen):
                opts.append(Opt("done", None))
            for x in rest:
                # Only picks the cost can still be completed with (format §3: completable).
                if self._completable(g, p, oid, chosen + [x], order):
                    opts.append(Opt(f"choose:{g.ref(x)}", x, sig=("tw", g.obj_sig(x))))
            if not opts or (len(opts) == 1 and opts[0].canon == "done"):
                break
            x = g.ask(Decision(p, "PayCost", opts, subject=g.ref(oid),
                               prompt=f"Teamwork {self.n}: tap creatures with total power {self.n} or more (total power {total})."))
            if x is None:
                break
            chosen.append(x)
            total += max(0, g.chars(x).power or 0)
        return _TeamworkTap(chosen)


def _first_identical(g, order, last, x):
    """In a set chosen one card at a time in a fixed order (later picks come after earlier ones),
    a pick of one of several identical cards is the first of them still available: they're
    interchangeable (format §3: identical objects are offered once, any of them names the pick),
    and taking a later twin would put the earlier one out of reach of the next picks (campaign12
    802410: two Firebending Lessons for collect evidence, the EUT exiled both)."""
    sig = g.obj_sig(x)
    return next((y for y in order[last + 1:] if g.obj_sig(y) == sig), x)


def _teamwork(p, cx):
    m = re.fullmatch(r"teamwork (\d+)", p, re.I)
    if not m:
        return None
    return [Keyword("additional_cost", value=TeamworkChoice(int(m.group(1))), text=p)]


_C.KEYWORD_HOOKS.append(_teamwork)


def _counter_unless_teamwork(m, sc):
    """"Counter target spell unless its controller pays {2}. Counter that spell unless its
    controller pays {4} instead if this spell was cast using teamwork." (702.194b: whether the
    caster declared the teamwork cost)."""
    sel = et.target_noun("target " + m.group(1), sc)
    base = fx.CounterUnlessPays(parse_cost(m.group(2)), 0, sel=sel)
    more = fx.CounterUnlessPays(parse_cost(m.group(3)), 0, sel=sel)
    return fx.If(lambda g, ctx: "teamwork" in (ctx.get("paid_additional") or []), more, base, text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Counter target (.+?) unless its controller pays ((?:\{[^}]+\})+)\. Counter that spell unless its controller pays ((?:\{[^}]+\})+) instead if (?:~|this spell) was cast using teamwork\.", _counter_unless_teamwork),
]
et.MERGE_NEXT = et.MERGE_NEXT + (r"^Counter that spell unless its controller pays .+ instead if (?:~|this spell) was cast using teamwork\.$",)


# =============================================================================== power-up (702.193)

def _power_up(line, cx):
    """CR 702.193a: "Power-up — [Cost]: [Effect]" means "[Cost]: [Effect]. If this permanent
    entered this turn, this ability's cost is reduced by this permanent's mana cost. Activate this
    ability only once." (cost reduction: ref/engine/altcast.py power_up_cost)."""
    m = re.fullmatch(r"Power-up — ((?:\{[^}]+\})+): (.+)", line)
    if not m:
        return None
    a = _C.compile_activated(m.group(1), m.group(2), line, cx)
    a.power_up = True
    a.once = True
    a.keyword = "power-up"
    return [a]


_C.LINE_HOOKS.append(_power_up)


def _two_counter_kinds(m, sc):
    """"Put a +1/+1 counter and an indestructible counter on ~." (keyword counters: CR 122.1b)."""
    sel = et.obj_ref(m.group(3), sc)
    sc.last = sel
    return fx.PutCountersMulti(sel, [(m.group(1), 1), (m.group(2), 1)], text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Put an? ([+\-]1/[+\-]1|flying|first strike|deathtouch|lifelink|vigilance|menace|reach|hexproof|indestructible|trample|haste) counter and an? ([+\-]1/[+\-]1|flying|first strike|deathtouch|lifelink|vigilance|menace|reach|hexproof|indestructible|trample|haste) counter on (~|it|target .+?)\.", _two_counter_kinds),
]


# =============================================================================== end the turn (724.1)

class _EndTheTurn(Effect):
    """CR 724.1: (a) triggered abilities that triggered before this process and aren't on the
    stack yet cease to exist; (b) every object on the stack is exiled, the resolving one too; (c)
    state-based actions are checked, no one gets priority and no triggers are put on the stack;
    (d) the current step and phase end (creatures leave combat) and the game skips to the cleanup
    step; (e) "at the beginning of the end step" abilities don't trigger (the end step is
    skipped); (f) triggers from this process go on the stack in the cleanup step, then the
    active player gets priority and there's another cleanup step (514.3a)."""

    def run(self, g, ctx):
        s = g.s
        s.pending = []                                    # 724.1a
        moves = [(it.obj, "exile", {"cause": "end_turn"}) for it in s.stack if it.kind == "spell" and it.obj in s.objs]
        s.stack = [it for it in s.stack if it.kind == "spell" and it.obj in s.objs]
        if moves:
            g.move_batch(moves)                           # 724.1b
        s.stack = []
        guard = 0
        while g.sba():                                    # 724.1c (704.3: until none apply)
            guard += 1
            if guard > 100:
                raise Unsupported("state-based actions don't settle while ending the turn")
            if s.result is not None:
                return
        for pl in s.players:
            pl.pool = []                                  # 106.4: the step ends
        if s.combat is not None:
            s.combat = None                               # 724.1d: removed from combat
            g.end_effects("eoc")
        s.step = "Cleanup"                                # 724.1d-e: straight to cleanup
        s.point = ("step",)
        s.passes = 0
        s.touch()
        ctx["stop"] = True


et.PATTERNS[0:0] = [
    (r"End the turn\.", lambda m, sc: _EndTheTurn()),
]


_KWL = r"(?:flying|first strike|double strike|deathtouch|haste|hexproof|indestructible|lifelink|menace|reach|trample|vigilance)"
et.PATTERNS[0:0] = [
    # "Another target creature or planeswalker you control gains hexproof until end of turn."
    # (Liliana the Faultless: "another" = other than the source, CR 109.5).
    (r"(Another target .+?) gains? (" + _KWL + r"(?:(?:,? and |, )" + _KWL + r")*) until end of turn\.", et._gains),
]


# =============================================================================== Sagas (714)

_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7}


class Chapter(tg.Trigger):
    """CR 714.2b: "{rN}—[Effect]" means "When one or more lore counters are put onto this Saga,
    if the number of lore counters on it was less than N and became at least N, [effect]."
    Counters it enters with are put onto it too (122.6), so chapter I triggers as it enters."""
    events = ("counter_added", "zone")
    lookback = False
    has_obj = False

    def __init__(self, n):
        self.n = n
        self.zones = ("battlefield",)

    def __deepcopy__(self, memo):
        return self

    def match(self, g, ev, src, c, ctrl):
        o = g.s.objs.get(src)
        if o is None:
            return None
        now = o.counters.get("lore", 0)
        if ev["type"] == "counter_added":
            if ev.get("obj") != src or ev.get("kind") != "lore":
                return None
            before = now - ev["n"]
        else:
            if ev.get("new") != src or ev.get("to") != "battlefield":
                return None
            before = 0
        return {"chapter": self.n} if before < self.n <= now else None


def _chapters(line, cx):
    """"I, II, III, IV — <effect>": one chapter ability per numeral (714.2c), each a triggered
    ability numbered in Oracle order (trigger:<ref>:n)."""
    m = re.fullmatch(r"((?:[IVX]+)(?:, [IVX]+)*) — (.+)", line)
    if not m or "Saga" not in cx.face.subtypes:
        return None
    nums = [_ROMAN.get(x.strip()) for x in m.group(1).split(",")]
    if None in nums:
        raise Unsupported(f"chapter numbers {m.group(1)!r}")
    out = []
    for n in nums:
        eff, specs, modes = _C.compile_effect(m.group(2), cx, ability=True)
        t = Triggered(Chapter(n), eff, specs, text=f"{m.group(1)} — {m.group(2)} (chapter {n})", modes=modes)
        t.chapter = (n,)
        out.append(t)
    return out


_C.LINE_HOOKS.append(_chapters)


class _SacSelfIfControl(Effect):
    """"Then if you control four or more Treasures, sacrifice ~." (an action that may not happen:
    the following "If you do" checks it, 608.2c)."""
    reports_done = True

    def __init__(self, filt, n):
        self.filt = filt
        self.n = n

    def run(self, g, ctx):
        p = ctx["controller"]
        k = sum(1 for x in g.s.battlefield if g.ctrl(x) == p and self.filt.match(g, x, ctx.get("source"), ctx))
        done = False
        src = ctx.get("source")
        o = g.s.objs.get(src)
        if k >= self.n and o is not None and o.zone == "battlefield" and g.ctrl(src) == p:
            g.sacrifice(src)
            done = True
        ctx.setdefault("vars", {})["last_done"] = done


def _if_control_sac_self(m, sc):
    from .nouns import parse_noun
    return _SacSelfIfControl(parse_noun(m.group(2)), num(m.group(1)))


et.PATTERNS[0:0] = [
    (r"If you control (two|three|four|five|six) or more (\w+), sacrifice ~\.", _if_control_sac_self),
]


# =============================================================================== collect evidence (701.59)

def _mv(g, x):
    from ..engine.mana import mana_value
    return mana_value(g.chars(x).mana_cost)


class CollectEvidenceCost(fx.CostPart):
    """CR 701.59a: "collect evidence N" = exile any number of cards from your graveyard with total
    mana value N or greater; 701.59b: impossible (so not offered) without enough. As an optional
    additional cost it's announced as `cost:exile` (the EUT's string; the format's cost kinds,
    §3.1, have no collect-evidence keyword). The cards are chosen one at a time in a fixed order
    (PayCost choose:<card>), `done` once the total is N or more."""
    name = "exile"

    def __init__(self, n):
        self.n = n

    def payable(self, g, p, oid):
        return sum(_mv(g, x) for x in _cards_in(g, p, "graveyard") if x != oid) >= self.n

    def pay(self, g, p, oid, ctx):
        # Fixed pick order (format §3): by name, the order the EUT uses, then identity.
        order = sorted((x for x in _cards_in(g, p, "graveyard") if x != oid), key=lambda x: (g.chars(x).name or "", repr(g.obj_sig(x)), x))
        chosen, total, last = [], 0, -1
        while True:
            opts = [Opt("done", None)] if total >= self.n else []
            for i, x in enumerate(order):
                if i <= last:
                    continue
                if total + sum(_mv(g, y) for y in order[i:]) >= self.n:
                    opts.append(Opt(f"choose:{g.ref(x)}", x, sig=("ev", g.obj_sig(x))))
            if not opts or (len(opts) == 1 and opts[0].canon == "done"):
                break
            x = g.ask(Decision(p, "PayCost", opts, subject=g.ref(oid),
                               prompt=f"Collect evidence {self.n}: exile cards with total mana value {self.n} or more."))
            if x is None:
                break
            x = _first_identical(g, order, last, x)
            chosen.append(x)
            total += _mv(g, x)
            last = order.index(x)
        g.move_batch([(x, "exile", {"cause": "cost"}) for x in chosen])
        ctx["evidence_collected"] = True  # 701.59c (linked to "if evidence was collected")


class _CoverUpExile(Effect):
    """Deadly Cover-Up: "If evidence was collected, exile a card from an opponent's graveyard.
    Then search its owner's graveyard, hand, and library for any number of cards with that name
    and exile them. That player shuffles, then draws a card for each card exiled from their hand
    this way." (701.23: searching for cards with a stated quality, any number of them)."""

    def run(self, g, ctx):
        if not ctx.get("evidence_collected"):
            return
        s = g.s
        p = ctx["controller"]
        q = 1 - p
        gy = _cards_in(g, q, "graveyard")
        if not gy:
            return
        opts = [Opt(f"choose:{g.ref(x)}", x, sig=("cu", g.obj_sig(x))) for x in gy]
        first = g.ask(Decision(p, "Search", opts, prompt="Exile a card from an opponent's graveyard."))
        name = g.chars(first).name
        g.move(first, "exile", cause="effect")
        # CR 701.23a: the searcher looks at every card of the searched hidden zones.
        g.s.know.looks(g, p, list(s.players[q].hand) + list(s.players[q].library))
        # Any number of them, zone by zone in the text's order (graveyard, hand, library): in each
        # zone one card at a time (identical cards are interchangeable, offered once) until
        # `done`, as the EUT asks it.
        chosen = []
        for z in ("graveyard", "hand", "library"):
            while True:
                cands = [x for x in _cards_in(g, q, z) if x not in chosen and g.chars(x).name == name]
                if not cands:
                    break
                opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(x)}", x, sig=("cu", z, name)) for x in cands]
                x = g.ask(Decision(p, "Search", opts, prompt=f"Exile cards named {name} from the {z}."))
                if x is None:
                    break
                chosen.append(x)
        from_hand = sum(1 for x in chosen if s.objs[x].zone == "hand")
        if chosen:
            g.move_batch([(x, "exile", {"cause": "effect"}) for x in chosen])
        g.shuffle(q)
        if from_hand:
            g.draw(q, from_hand)


def _cover_up(m, sc):
    return _CoverUpExile()


et.PATTERNS[0:0] = [
    (r"If evidence was collected, exile a card from an opponent's graveyard\. Then search its owner's graveyard, hand, and library for any number of cards with that name and exile them\. That player shuffles, then draws a card for each card exiled from their hand this way\.", _cover_up),
]
et.MERGE_NEXT = et.MERGE_NEXT + (
    r"^Then search its owner's graveyard, hand, and library for any number of cards with that name and exile them\.$",
    r"^That player shuffles, then draws a card for each card exiled from their hand this way\.$",
)


def _search_either_to_hand(m, sc):
    """"Search your library for a basic Plains card or a creature card with mana value 1 or less,
    reveal it, put it into your hand, then shuffle." (Starfield Shepherd): either noun, each with
    its own qualifiers (CR 701.23)."""
    from .nouns import parse_noun
    fa = parse_noun(m.group(1))
    fb = parse_noun(m.group(2) + (" " + m.group(3) if m.group(3) else ""))

    def either(g, c, o, src, ctx, fa=fa, fb=fb):
        return fa.match_chars(g, c, o, src, ctx) or fb.match_chars(g, c, o, src, ctx)
    f = Filter(custom=either, text=f"{m.group(1)} or {m.group(2)}")
    return fx.Search(f, 1, "hand", text=m.group(0))


et.PATTERNS[0:0] = [
    (r"Search your library for an? (.+?) card or an? (.+?) card(?: (with .+?))?, reveal it, put it into your hand, then shuffle\.", _search_either_to_hand),
]
