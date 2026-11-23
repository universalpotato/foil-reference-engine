"""Coverage batch 9, new mechanics (EUT build ad91628c): firebending (CR 702.189a), airbend (701.65a),
split cards (709) and the cards that use them: The Legend of Roku // Avatar Roku, Avatar's Wrath,
Appa, Steadfast Guardian, Airbender Ascension, Airbender's Reversal, Cease // Desist, Ill-Timed
Explosion, Malcolm, Alluring Scoundrel.

Engine pieces outside this file (each small and local):
- firebending mana: `FireUnit` here; turn.end_of_step keeps it through the combat steps (500.5a)
  and casting.pay_mana spends other mana of the color first (the payer's choice, see FireUnit);
- airbend: the exiled card's flag `airbent`; altcast.extra_cast_candidates offers its owner
  `cast:<ref>#airbend`; casting.alt_cost gives the {2} alternative cost (118.9); the rule
  `cant_cast_outside_hand` (Avatar's Wrath) is checked in casting.can_cast and
  batch7.castable_now;
- split cards: layers.base_chars combines the halves off the stack (709.4); casting.face_chars
  evaluates only the half being cast (709.3a); casting.legal_targets' `same_graveyard` target
  words (Cease).
Strict like the rest of the compiler: a sentence these patterns don't match exactly still raises
Unsupported.
"""
import re

from ..engine import effects as fx
from ..engine.effects import Filter, E, Sel
from ..engine.ir import (Keyword, Triggered, Static, RulePart, TargetSpec, Seq, Trigger)
from ..engine.decisions import Decision, Opt
from ..engine.mana import Unit, mana_value
from ..cards.model import DB
from .. import Unsupported
from . import compiler as C
from . import triggers as tg
from . import effects_text as et
from .nouns import parse_noun


def _nc(cls):
    cls.__deepcopy__ = lambda self, memo: self
    cls.__copy__ = lambda self: self
    return cls


# =============================================================================== firebending

class FireUnit(Unit):
    """Mana added by firebending (CR 702.189a): "Until end of combat, you don't lose this mana as
    steps and phases end." It empties when the combat phase ends (500.5, 500.5a: the effect
    expires at the end of the combat phase, then mana empties), i.e. as the end of combat step
    ends; turn.end_of_step keeps it as the earlier combat steps end. Unspent mana of one type is
    otherwise interchangeable, and which of it a payment uses is the payer's choice (106.4,
    601.2h); spending the mana that would be lost first is never worse, so casting.pay_mana takes
    other mana of the chosen type before this (rules call: the payer's free choice)."""
    __slots__ = ()
    until_eoc = True

    def __deepcopy__(self, memo):
        return FireUnit(self.color, self.source, self.restrict, self.rider)

    def __repr__(self):
        return f"FireUnit({self.color})"


class AddFirebendingMana(E):
    """"Whenever this creature attacks, add N {R}. Until end of combat, you don't lose this mana
    as steps and phases end." (CR 702.189a). With "X, where X is ~'s power": its power as the
    ability resolves, or as it last existed on the battlefield (608.2h)."""

    def __init__(self, n, text=""):
        self.n = n
        self.text = text

    def run(self, g, ctx):
        n = self.n(g, ctx) if callable(self.n) else self.n
        p = ctx["controller"]
        for _ in range(max(0, n)):
            g.s.players[p].pool.append(FireUnit("R", source=ctx.get("source")))
        g.s.touch()
        g.emit({"type": "firebend", "player": p, "obj": ctx.get("source")})  # 702.189b


def _firebending_amount(lp):
    if "where" in lp:
        def amount(g, ctx):
            c = fx.self_chars(g, ctx)
            return max(0, (c.power or 0) if c is not None else 0)
        return amount
    return int(re.search(r"\d+", lp).group(0))


def _firebending_keyword(p, cx):
    lp = p.lower().rstrip(".")
    if not (re.fullmatch(r"firebending \d+", lp) or re.fullmatch(r"firebending x, where x is ~'s power", lp)):
        if lp.startswith("firebending"):
            raise Unsupported(f"firebending form {p!r}")
        return None
    t = Triggered(tg.Attacks(self_=True), AddFirebendingMana(_firebending_amount(lp), text=p), [], text=p)
    t.keyword = "firebending"
    return [t]


# =============================================================================== airbend (701.65a)

# Layouts whose card may be cast as another face (MDFC 712.11b, split 709.3): either non-land face
# (airbend_candidates). Adventurer and omen cards (layout
# "adventure", CR 715.3, 720.3) may be cast as either face (airbend_candidates). A preparation card
# can't be cast as its prepare spell (CR 722.3, 722.4): only its normal face, like any card.
_MULTI_CAST = ("adventure", "omen", "modal_dfc", "split")


def airbend_objects(g, oids):
    """CR 701.65a: exile those objects; for each card exiled this way, for as long as it remains
    exiled, its owner may cast it by paying {2} rather than its mana cost (an alternative cost,
    118.9). A token exiled this way ceases to exist (704.5d; rulings 2025-10-02); a card that
    leaves exile is a new object without the permission (400.7). Lands can't be cast (ruling
    2025-10-02: lands exiled this way can't be played)."""
    s = g.s
    moves = []
    for oid in oids:
        o = s.objs.get(oid)
        if o is None:
            continue
        if o.zone != "battlefield":
            raise Unsupported("airbending a spell (CR 701.65a)")
        moves.append((oid, "exile", {"cause": "effect"}))
    if not moves:
        return []
    res = g.move_batch(moves)
    out = []
    for r in res:
        if r is None or r not in s.objs:
            continue
        n = s.objs[r]
        if n.zone == "exile" and not n.token and not n.is_copy:
            n.flags["airbent"] = True
        out.append(r)
    s.touch()
    g.emit({"type": "airbend", "player": None, "objs": list(out)})  # 701.65b
    return out


class Airbend(E):
    """"Airbend <objects>" (CR 701.65a)."""
    reports_done = True

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        done = airbend_objects(g, fx.objs_of(self.sel, g, ctx))
        ctx.setdefault("vars", {})["last_done"] = bool(done)
        return bool(done)


class AirbendAllOther(E):
    """Avatar's Wrath: "Choose up to one target creature, then airbend all other creatures." The
    creatures are exiled at once (one action); the chosen one (if still a legal target, 608.2b)
    stays."""

    def __init__(self, keep, text=""):
        self.keep = keep
        self.text = text

    def run(self, g, ctx):
        keep = set(fx.objs_of(self.keep, g, ctx))
        others = [o for o in list(g.s.battlefield) if o not in keep and "Creature" in g.chars(o).types]
        airbend_objects(g, others)


def airbend_candidates(g, p):
    """(oid, how, face, canon) for the airbent cards p owns (altcast.extra_cast_candidates)."""
    s = g.s
    out = []
    for oid in s.players[p].exile:
        o = s.objs[oid]
        if not o.flags.get("airbent") or o.owner != p or o.token:
            continue
        card = DB.get(o.card)
        if card.layout == "adventure":
            # An adventurer card (CR 715) or an omen card (720; layout "adventure" in the Oracle
            # data): airbend's permission is to "cast it" with no restriction on its
            # characteristics, and as a player casts such a card they choose whether to cast it
            # normally or as an Adventure / Omen (715.3, 720.3), evaluating only the chosen
            # characteristics (715.3a, 720.3a, 601.3e); 715.3d: "other effects that allow a
            # player to cast it may allow a player to cast it as an Adventure". Either way for
            # {2} rather than the chosen face's mana cost (118.9; rulings 2019-10-04 / 2023-10-13:
            # "Casting a card as an Adventure isn't casting it for an alternative cost. Effects
            # that allow you to cast a spell for an alternative cost [...] may allow you to apply
            # those to the Adventure"). A land adventurer (Midgar, City of Mako) can't be cast as
            # its land (305.9; airbend rulings 2025-10-02: lands exiled this way can't be
            # played), but it can be cast as its Adventure. Format: `cast:<ref>@<face>#airbend`.
            if "Land" not in card.faces[0].types:
                out.append((oid, "airbend", 0, f"cast:{g.ref(oid)}#airbend"))
            if len(card.faces) > 1:
                out.append((oid, "airbend", 1, f"cast:{g.ref(oid)}@{card.faces[1].name}#airbend"))
            continue
        if card.layout in ("split", "modal_dfc"):
            # Either half / face of an airbent split or modal DFC card.
            # Airbend's permission is to "cast it" (CR 701.65a) with no restriction on which
            # half/face; a player casting a split card chooses which half they cast (709.3) and a
            # player casting a modal double-faced card chooses which face (712.11b), evaluating
            # only that half/face (709.3a, 712.11c). Either way for {2} rather than the chosen
            # half's mana cost (118.9). A land face can't be cast and airbend gives no
            # permission to play it (305.9; ruling 2025-10-02), so an MDFC's land face is skipped.
            # Fuse (702.102c) needs the card in hand, so a fused split spell isn't offered.
            # Rooms (709.5) are split cards: either door may be cast this way (709.5d then gives
            # the cast door its unlocked designation). Format: `cast:<ref>[@<face>]#airbend`.
            for fi, f in enumerate(card.faces):
                if "Land" in f.types:
                    continue
                canon = f"cast:{g.ref(oid)}#airbend" if fi == 0 else f"cast:{g.ref(oid)}@{f.name}#airbend"
                out.append((oid, "airbend", fi, canon))
            continue
        if card.layout in _MULTI_CAST:
            raise Unsupported(f"airbent {card.layout} card: which faces airbend lets its owner cast (CR 701.65a)")
        if "Land" in card.faces[0].types:
            continue  # lands are played, not cast (ruling 2025-10-02)
        out.append((oid, "airbend", 0, f"cast:{g.ref(oid)}#airbend"))
    return out


AIRBEND_COST = [2]


def _airbend_sentence(m, sc):
    np = m.group(1)
    if np.startswith("any number of "):
        rest = np[len("any number of "):]
        sel = et.target_noun(rest, sc)
        sc.tc.specs[-1].n = (0, 1000)
    else:
        sel = et.target_noun(np, sc)
    sc.last = sel
    return Airbend(sel, text=m.group(0))


def _avatars_wrath_choose(m, sc):
    sel = et.target_noun("up to one target creature", sc)
    sc.last = sel
    return AirbendAllOther(sel, text=m.group(0))


class CantCastOutsideHand(E):
    """"Until your next turn, your opponents can't cast spells from anywhere other than their
    hands." (CR 101.2; 611.2b: a resolved effect, ending as its controller's next turn begins;
    turn.next_turn ends "until_your_next_turn" effects)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        fx.AddEffect(None, {}, duration="until_your_next_turn", kind="rule",
                     data={"rule": "cant_cast_outside_hand",
                           "part": RulePart("cant_cast_outside_hand", None, who="opp")}).run(g, ctx)


def cast_outside_hand_forbidden(g, p, oid):
    """True if p can't cast object oid because a "can't cast spells from anywhere other than
    their hands" effect applies to p and oid isn't in p's hand."""
    o = g.s.objs.get(oid)
    if o is None:
        return False
    if o.zone == "hand" and o.owner == p:
        return False
    return g.rule_applies_player(p, "cant_cast_outside_hand")


class ExileThisSpell(E):
    """"Exile <this spell>." as the last instruction of an instant or sorcery (CR 608.2n: it's
    put into exile instead of its owner's graveyard as the last step of its resolution)."""
    text = "Exile this spell."

    def run(self, g, ctx):
        ctx["spell_dest"] = "exile"


@_nc
class CastFromExile(Trigger):
    """"Whenever you cast a spell from exile" (the spell was in exile when it became cast: an
    airbent card, a plotted or warped card, a card exiled with a permission to play it, ...)."""
    events = ("cast",)

    def __init__(self):
        self.zones = ("battlefield",)

    def match(self, g, ev, src, c, ctrl):
        if ev["player"] != ctrl or ev.get("from") != "exile":
            return None
        return {"obj": ev["obj"], "player": ev["player"], "item": ev["item"]}


# =============================================================================== Sagas: Roku

class ExileReturnTransformed(E):
    """The Legend of Roku III: "Exile this Saga, then return it to the battlefield transformed
    under your control." (CR 701.27a-c; 712.14a: a card that isn't a transforming double-faced
    card stays in exile; 400.7: only if it's still the same object on the battlefield, the
    exiled card is the one returned). "Your": the chapter ability's controller."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        src = ctx.get("source")
        o = g.s.objs.get(src)
        if o is None or o.zone != "battlefield":
            return
        nid = g.move(src, "exile", cause="effect")
        if nid is None or nid not in g.s.objs:
            return
        n = g.s.objs[nid]
        if n.zone != "exile" or n.token or DB.get(n.card).layout != "transform" or n.flags.get("copiable") is not None:
            return
        res = g.move_batch([(nid, "battlefield", {"controller": ctx["controller"], "face": 1})])
        ctx.setdefault("vars", {})["returned"] = [r for r in res if r is not None]


def _add_one_any(m):
    from .batch7 import AddManaCombination
    return AddManaCombination(1, text=m.group(0))


_DRAGON_TEXT = "Flying\nFirebending 4"


def _roku_dragon(m, sc):
    from .model import token_def
    def cd():
        return token_def("Dragon Token", ("Creature",), ("Dragon",), ("R",), 4, 4, _DRAGON_TEXT)
    sc.last = fx.var("token")
    return fx.CreateToken(cd, 1, text=m.group(0), remember="token")


# =============================================================================== Cease // Desist

def _cease_targets(m, sc):
    """"Exile up to two target cards from a single graveyard." (CR 115.1, 601.2c: after the first
    target is chosen, only cards in that graveyard can be the second; 608.2b: a target that left
    its graveyard is illegal and isn't exiled)."""
    spec = TargetSpec("card", filt=Filter(text="card"), zone="graveyard", n=(0, 2),
                      text="up to two target cards from a single graveyard")
    spec.same_graveyard = True
    sel = sc.tc.add(spec)
    sc.last = sel
    return fx.MoveTo(sel, "exile", text=m.group(0))


def _target_player_gains_draws(m, sc):
    who = et.player_ref("target player", sc)
    return Seq(fx.GainLife(int(m.group(1)), who=who), fx.Draw(1, who=who, text=m.group(0)))


def _split_post(abilities, cx):
    """Split cards (CR 709): only instant and sorcery halves without fuse or aftermath (a Room's
    shared type line and unlock rules, 709.5, and fuse, 702.102, aren't implemented)."""
    f = cx.face
    if f.card.layout != "split":
        return abilities
    if len(f.card.faces) != 2:
        raise Unsupported("split card with more than two halves")
    if all("Room" in face.subtypes and set(face.types) == {"Enchantment"} for face in f.card.faces):
        return abilities  # Rooms (CR 709.5): ref/cards/round2_a2.py
    for face in f.card.faces:
        if not ({"Instant", "Sorcery"} & set(face.types)) or ({"Creature", "Artifact", "Enchantment", "Planeswalker",
                                                               "Battle", "Land"} & set(face.types)):
            raise Unsupported("split card with a permanent half (Room, CR 709.5)")
        if re.search(r"\b(Fuse|Aftermath)\b", face.text):
            raise Unsupported("fuse / aftermath split card (CR 702.102, 702.127)")
    return abilities


# =============================================================================== Ill-Timed Explosion

class DiscardTwoReflexiveDamage(E):
    """Ill-Timed Explosion: "Then you may discard two cards. When you do, ~ deals X damage to each
    creature, where X is the greatest mana value among cards discarded this way." With fewer than
    two cards in hand the player can't discard two, so can't choose to (608.2d; no trigger). The
    reflexive trigger (603.12) is created only if the two cards were discarded; X is computed as
    it resolves from the discarded cards (their mana value in the graveyard: a split card's is
    its combined cost's, 709.4b; {X} counts 0, 202.3e, ruling 2024-02-02)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        pl = g.s.players[p]
        if len(pl.hand) < 2:
            return
        # Posed as the EUT does: a PayCost yes/no ("you may [action]. When you do" works like an
        # optional cost of the reflexive trigger), then the discards one at a time.
        a = g.ask(Decision(p, "PayCost", [Opt("yes", True), Opt("no", False)],
                           subject=g.ref(ctx["source"]) if ctx.get("source") in g.s.objs else None,
                           prompt="Discard two cards?"))
        if not a:
            return
        out = []
        for _ in range(2):
            opts = [Opt(f"choose:{g.ref(o)}", o, sig=("disc", g.obj_sig(o))) for o in pl.hand]
            o = g.ask(Decision(p, "Discard", opts, prompt="Choose a card to discard."))
            out.append(g.discard(p, o, cause="effect"))
        ids = tuple(x for x in out if x is not None)
        data = dict(ctx.get("data") or {})
        data["discarded"] = ids
        from ..engine.triggers import PendingTrigger
        ab = Triggered(None, _ExplosionDamage(), [], text="When you do, ~ deals X damage to each creature")
        sc = ctx.get("source_chars") or ctx.get("chars")
        g.s.pending.append(PendingTrigger(ab, ctx.get("source"), sc, p, ctx.get("event"), data,
                                          g.after_own_triggers(ctx.get("source"), sc)))


class _ExplosionDamage(E):
    text = "~ deals X damage to each creature"

    def run(self, g, ctx):
        ids = (ctx.get("data") or {}).get("discarded") or ()
        mvs = []
        for x in ids:
            if x in g.s.objs:
                mvs.append(g.chars(x).mana_value)
            elif x in g.s.lki:
                mvs.append(g.s.lki[x].mana_value)
        n = max(mvs) if mvs else 0
        fx.Damage(n, fx.all_matching(Filter(types=["Creature"], text="creature"))).run(g, ctx)


# =============================================================================== Malcolm

class CastDiscardedFree(E):
    """Malcolm: "If there are four or more chorus counters on Malcolm, you may cast the
    discarded card without paying its mana cost." Malcolm's counters as it is, or as it last
    existed on the battlefield (ruling 2023-11-10). The card is cast while the ability resolves
    (608.2g, timing ignored, ruling); only the card discarded by this ability, still in the
    graveyard (400.7); a land can't be cast (ruling); {X} is 0 (107.3b, ruling); additional
    costs may / must be paid (ruling). A split card: either half (709.3)."""

    def __init__(self, n=4, text=""):
        self.n = n
        self.text = text

    def run(self, g, ctx):
        src = ctx.get("source")
        o = g.s.objs.get(src)
        if o is not None and o.zone == "battlefield":
            k = o.counters.get("chorus", 0)
        else:
            lo = g.s.lki_obj.get(src)
            k = lo.counters.get("chorus", 0) if lo is not None else 0
        if k < self.n:
            return
        p = ctx["controller"]
        ids = (ctx.get("vars") or {}).get("discarded") or []
        for oid in ids:
            d = g.s.objs.get(oid)
            if d is None or d.zone != "graveyard":
                continue
            card = DB.get(d.card)
            # Each half / face that can be cast from the card (709.3, 715.3, 712.11; never a
            # prepare spell, 722.3; a land isn't cast, ruling).
            faces = [i for i, f in enumerate(card.faces) if "Land" not in f.types
                     and (i == 0 or card.layout in ("split", "adventure", "omen", "modal_dfc"))]
            ok = [i for i in faces if castable_free(g, p, oid, i)]
            if not ok:
                continue
            # Posed as the EUT does (and as paradigm's copy, batch7): ChooseCard `done` | the card
            # (`choose:<ref>`, its first half) | another half (`cast:<ref>@<half>`).
            opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(oid)}" if i == 0 else f"cast:{g.ref(oid)}@{card.faces[i].name}", i)
                                          for i in ok]
            a = g.ask(Decision(p, "ChooseCard", opts, subject=g.ref(src) if src in g.s.objs else None,
                               prompt="Cast the discarded card without paying its mana cost (or done)?"))
            if a is not None:
                g.cast(p, oid, "free", a)


def castable_free(g, p, oid, face):
    """A spell p may cast now during a resolution without paying its mana cost (608.2g: timing
    permissions don't matter): not forbidden (101.2), with its required targets (601.2c) and its
    other costs payable."""
    from .batch7 import castable_now
    from ..engine.effects import ChoiceCost
    o = g.s.objs[oid]
    saved = o.face
    o.face = face
    g.s.touch()
    try:
        if not castable_now(g, p, oid, "free"):
            return False
        # Mandatory additional costs must be payable (ruling 2023-11-10; 601.2h).
        c = g.face_chars(oid, face)
        _, extra = g.spell_cost(p, oid, c, "free", face)
        from .batch8 import GiftTargetChoice
        for pt in extra.get("parts", []):
            # A spell whose targets depend on the gift (Long
            # River's Pull) has its required targets only through its gift choice (601.2c,
            # 702.174m); that optional cost isn't skipped here (rt3-f 1214243: Malcolm offered
            # the Pull with only its own resolving trigger on the stack).
            if isinstance(pt, GiftTargetChoice) and not (pt._has(g, p, oid, pt.specs[0]) or pt._has(g, p, oid, pt.specs[1])):
                return False
        return all(pt.payable(g, p, oid) for pt in extra.get("parts", [])
                   if not (isinstance(pt, ChoiceCost) and pt.optional))
    finally:
        o.face = saved
        g.s.touch()


def _chorus_counter(m, sc):
    return fx.PutCounters(fx.self_permanent(), "chorus", 1, text=m.group(0))


# =============================================================================== flicker "up to one"

def _flicker_up_to_one(m, sc):
    sel = et.target_noun(m.group(1), sc)
    sc.last = fx.var("returned")
    return fx.Flicker(sel, controller="owner", text=m.group(0))


# =============================================================================== registration

et.PATTERNS[0:0] = [
    (r"Airbend ((?:up to one |any number of other |any number of )?target .+?)\.", _airbend_sentence),
    (r"Choose up to one target creature, then airbend all other creatures\.", _avatars_wrath_choose),
    (r"Until your next turn, your opponents can't cast spells from anywhere other than their hands\.",
     lambda m, sc: CantCastOutsideHand(text=m.group(0))),
    # An instant or sorcery's "Exile ~." exiles the resolving spell (608.2n: instead of putting it
    # into the graveyard); a permanent's is effects_text's ExileSelf.
    (r"Exile ~\.", lambda m, sc: ExileThisSpell() if sc.cx.is_spell else fx.ExileSelf(text=m.group(0))),
    (r"Exile the top three cards of your library\. Until the end of your next turn, you may play those cards\.",
     lambda m, sc: fx.ExileTopMayPlay(3, "end of your next turn", text=m.group(0))),
    (r"Exile ~, then return it to the battlefield transformed under your control\.",
     lambda m, sc: ExileReturnTransformed(text=m.group(0))),
    (r"Create a 4/4 red Dragon creature token with flying and firebending 4\.", _roku_dragon),
    # A chapter ability adding mana: not a mana ability (it uses the stack, 605.1b); the mana
    # empties as the step ends (106.4).
    (r"Add one mana of any color\.", lambda m, sc: _add_one_any(m)),
    (r"Exile up to two target cards from a single graveyard\.", _cease_targets),
    (r"Target player gains (\d+) life and draws a card\.", _target_player_gains_draws),
    (r"(?:Then )?[Yy]ou may discard two cards\. When you do, ~ deals X damage to each creature, "
     r"where X is the greatest mana value among cards discarded this way\.",
     lambda m, sc: DiscardTwoReflexiveDamage(text=m.group(0))),
    (r"Put a chorus counter on it\.", _chorus_counter),
    (r"If there are four or more chorus counters on ~, you may cast the discarded card without paying its mana cost\.",
     lambda m, sc: CastDiscardedFree(4, text=m.group(0))),
    (r"Exile (up to one target creature you control), then return it to the battlefield under its owner's control\.",
     _flicker_up_to_one),
]

et.MERGE_PAIRS.extend([
    (r"Exile the top three cards of your library\.$", r"^Until the end of your next turn, you may play those cards\.$"),
    (r"(?:^|Then )you may discard two cards\.$", r"^When you do, ~ deals X damage to each creature, where X is the greatest mana value among cards discarded this way\.$"),
])


def _trigger_hook(line, cx):
    m = re.match(r"^Whenever you cast a spell from exile, (.+)$", line)
    if m:
        return CastFromExile(), m.group(1)
    return None


C.KEYWORD_HOOKS.append(_firebending_keyword)
C.POST_HOOKS.append(_split_post)
tg.TRIGGER_HOOKS.append(_trigger_hook)
