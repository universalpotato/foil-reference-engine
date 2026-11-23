"""Card-support round 2, list b: graveyard, library, exile and hand zones.

Mechanics, each from its rule (cited in the code):
- Uldaros Theorix: "up to one target nonland card of each card type" (one target word, CR 115.1,
  601.2c: a card is chosen once, as any one of its card types, 205.2b; Atraxa and Aminatou's
  Augury rulings), copies of cards created in exile and cast during the resolution without paying
  their mana costs (707.12, 707.12a, 608.2g, 118.9; {X} is 0, 107.3b and the card's ruling), a cap
  on the total mana value of the spells cast, and permanent spells cast this way becoming tokens
  (608.3f; zones._do_move).
- Returns from the graveyard (Zoraline, Raise the Past, Helping Hand, Glimpse the Core, Faith &
  Grief); cards outside the game (400.11a: none without a sideboard, ref.decklist.Deck); hand
  reveals (Stinging Vitriol, Thought-Stalker Warlock); Opus mill instead (Exhibition Tidecaller).
- Impulse draw and permissions: cast during a resolution (Chandra, 608.2g), play permissions in
  exile and in the graveyard (Heroes' Hangout, Thor, Tablet of Discovery, Tersa; 601.3), lands
  played from the graveyard (Icetill Explorer), flashback granted until end of turn (702.34a;
  layers.OFF_BATTLEFIELD_HOOKS, casting.ALT_COST_HOOKS).
- Searches (701.23: may search, Fabled/Elven Passage, Enroot, Repurposing Bay), the top N cards
  (United Battlefront, Pictures of Spider-Man, Fomori Vault), graveyard exile (Pit of Offerings'
  linked colors, Soul-Guide Lantern, Dauntless Scrapbot, Lord Skitter, Ghost Vacuum).
- Roles (303.7, 111.10k-q, 303.4i, 704.5z; Royal from round2_c2), random graveyard exile (Rules.random_hook), Esper Origins
  (cast from a graveyard, returned transformed, 712.14), naming any card (201.4, Ancient Vendetta),
  Swallowed by Leviathan, Rapid Rescue, Flush Out.

Registered into the compiler's hooks (compiler.py, above KEYWORD_HOOKS) like the other card
modules. Strict like the rest of the compiler: a sentence these patterns don't match exactly still
raises Unsupported.
"""
import re

from ..engine import effects as fx
from ..engine.effects import Filter, E, Sel
from ..engine.ir import TargetSpec, Seq
from ..engine.decisions import Decision, Opt
from ..engine.objects import Obj
from .. import Unsupported
from . import compiler as C
from . import triggers as tg
from . import effects_text as et
from . import statics as st
from .nouns import num
from .model import DB


# =============================================================================== Uldaros Theorix

# CR 205.2a: the card types, in the rule's (alphabetical) order. "Nonland": every type but land.
NONLAND_CARD_TYPES = ("Artifact", "Battle", "Conspiracy", "Creature", "Dungeon", "Enchantment",
                      "Instant", "Kindred", "Phenomenon", "Plane", "Planeswalker", "Scheme",
                      "Sorcery", "Vanguard")


def castable_faces(g, p, oid):
    """The faces of a card (or a copy of one) that p may cast now during a resolution without
    paying its mana cost: a split card's halves (709.3), an Adventure (715.3), an Omen (720.3), an
    MDFC's back face (712.11); never a land face (305.9) nor a prepare spell from the card itself
    (722.3). batch9a.castable_free checks targets, prohibitions and mandatory additional costs."""
    from .batch9a import castable_free
    card = DB.get(g.s.objs[oid].card)
    faces = [i for i, f in enumerate(card.faces) if "Land" not in f.types
             and (i == 0 or card.layout in ("split", "adventure", "omen", "modal_dfc"))]
    return [i for i in faces if castable_free(g, p, oid, i)]


class ExileCopyCastTotalMV(E):
    """Uldaros Theorix: "exile up to one target nonland card of each card type from your
    graveyard. Copy those cards. You may cast any number of spells with total mana value 6 or less
    from among the copies without paying their mana costs."

    - The targets still legal are exiled together (608.2b); "those cards" are the cards exiled.
    - Each copy is created in exile, where its card is (707.12), owned and controlled by the
      ability's controller; copies not cast cease to exist (704.5e) at the next state-based action
      check, after the resolution.
    - The copies are cast one at a time during the resolution (608.2g: timing permissions don't
      apply; 707.12a: each copy individually), as an alternative cost of {0} (118.9: additional
      costs are paid; {X} is 0, 107.3b, ruling 2026-08-21), each spell's mana value on the stack
      counting towards the total (202.3); a copy with no legal targets or forbidden to be cast
      isn't offered (601.2c, 101.2). Posed as the EUT poses it: a ChooseCard `done` | the copy, named
      by the card it copies (`choose:<ref>`; another castable face `cast:<ref>@<face>`).
    - A permanent spell cast this way becomes a token as it resolves (608.3f)."""

    def __init__(self, total, text=""):
        self.total = total
        self.text = text

    def run(self, g, ctx):
        s = g.s
        p = ctx["controller"]
        src = ctx.get("source")
        targets = [r for grp in (ctx.get("legal") or []) for r in grp if not isinstance(r, tuple)]
        targets = [r for r in targets if r in s.objs and s.objs[r].zone == "graveyard"]
        if not targets:
            return
        res = g.move_batch([(o, "exile", {"cause": "effect"}) for o in targets])
        exiled = [r for r in res if r is not None and r in s.objs and s.objs[r].zone == "exile"]
        # "Copy those cards" (707.12: in the zone the card is in).
        copies = []
        for orig in exiled:
            ob = s.objs[orig]
            n = Obj(s.new_id(), ob.card, p, "exile")
            n.is_copy = True
            n.controller = p
            n.face = 0
            n.key = g.new_key(g.base_chars(ob).name or ob.card)
            n.timestamp = s.new_ts()
            s.objs[n.id] = n
            s.players[p].exile.append(n.id)
            copies.append((n.id, orig))
        s.touch()
        left = self.total
        while True:
            opts = [Opt("done", None)]
            for cid, orig in copies:
                if cid not in s.objs or s.objs[cid].zone != "exile":
                    continue
                card = DB.get(s.objs[cid].card)
                for fi in castable_faces(g, p, cid):
                    if g.face_chars(cid, fi).mana_value > left:
                        continue
                    canon = f"choose:{g.ref(orig)}" if fi == 0 else f"cast:{g.ref(orig)}@{card.faces[fi].name}"
                    opts.append(Opt(canon, (cid, fi), sig=("uldaros", g.obj_sig(orig), fi)))
            if len(opts) == 1:
                return
            a = g.ask(Decision(p, "ChooseCard", opts, subject=g.ref(src) if src in s.objs else None,
                               prompt=f"Cast a copy without paying its mana cost (total mana value {left} or less left), or done."))
            if a is None:
                return
            cid, fi = a
            left -= g.face_chars(cid, fi).mana_value
            g.cast(p, cid, "free", fi)


def _uldaros(m, sc):
    """The targets: one target word (CR 115.1d) with a slot per nonland card type (205.2a), each
    "up to one", a card at most once among them (601.2c; distinct_from)."""
    first = len(sc.tc.specs)
    for i, t in enumerate(NONLAND_CARD_TYPES):
        f = Filter(types=[t], nonland=True, owner="you", text=f"nonland {t.lower()} card from your graveyard")
        spec = TargetSpec("card", filt=f, zone="graveyard", n=(0, 1),
                          text=f"up to one target nonland card of each card type ({t.lower()}) from your graveyard",
                          distinct_from=list(range(first, first + i)) or None)
        sc.tc.add(spec)
    return ExileCopyCastTotalMV(num(m.group(1)) if not m.group(1).isdigit() else int(m.group(1)), text=m.group(0))


# =============================================================================== returns from the graveyard

def _gy_noun(noun):
    """"nonland permanent card with mana value 3 or less" / "artifact and/or enchantment cards" ->
    Filter (a card's mana value counts {X} as 0, CR 202.3e; Raise the Past and Helping Hand
    rulings)."""
    n = re.sub(r"\bcards?\b", "", noun).replace("  ", " ").strip()
    from .nouns import parse_noun
    return parse_noun(n) if n else Filter(text="card")


def _return_target_from_gy(m, sc):
    """"Return [up to N] target <card> from your graveyard to your hand / the battlefield [tapped]
    [with a finality counter on it]." The cards are your own (CR 400.3; a graveyard card's owner);
    on the battlefield under your control (110.2a); "tapped" and the counter are how it enters
    (614.1c, 122.6; finality: 122.1h)."""
    upto, noun, dest, tapped, finality = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    f = _gy_noun(noun)
    f.owner = "you"
    n = (1, 1) if upto is None else (0, num(upto))
    sel = sc.tc.add(TargetSpec("card", filt=f, zone="graveyard", n=n, text=m.group(0)))
    sc.last = sel
    if dest == "your hand":
        if tapped or finality:
            raise Unsupported(f"return to hand {m.group(0)!r}")
        return fx.MoveTo(sel, "hand", text=m.group(0))
    counters = {"finality": 1} if finality else {}
    eff = fx.PutOntoBattlefield(sel, ("graveyard",), controller="you", tapped=bool(tapped), counters=counters,
                                text=m.group(0))
    sc.last = fx.var("returned")
    return eff


def _gy_all(filt):
    """Every card in your graveyard matching filt, as the effect happens."""
    def fn(g, ctx):
        p = ctx["controller"]
        return [o for o in list(g.s.players[p].graveyard) if filt.match(g, o, ctx.get("source"), ctx)]
    return Sel(fn, f"all {filt.text} in your graveyard", single=False)


def _return_all_from_gy(m, sc):
    """Raise the Past: "Return all creature cards with mana value 2 or less from your graveyard to
    the battlefield." They enter at the same time (CR 101.4-style one event; 603.6a: each sees the
    others enter), under your control."""
    f = _gy_noun(m.group(1))
    sc.last = fx.var("returned")
    return fx.PutOntoBattlefield(_gy_all(f), ("graveyard",), controller="you", tapped=bool(m.group(2)), text=m.group(0))


def _search_put_that_card(m, sc):
    """Glimpse the Core: "Search your library for a basic Forest card, put that card onto the
    battlefield tapped, then shuffle." (CR 701.23a-b)."""
    from .nouns import parse_noun
    return fx.Search(parse_noun(m.group(1)), 1, "battlefield", tapped=bool(m.group(2)), text=m.group(0))


# =============================================================================== outside the game

class FromOutsideTheGame(E):
    """Extrapolate the Impossible: "You may reveal exactly two cards you own with different names
    from outside the game. An opponent chooses one of them. You put that card into your hand."
    North Wind Avatar: "you may put a card you own from outside the game into your hand."
    Cards outside the game are a player's sideboard (CR 400.11a; the cards' rulings: "In a
    tournament event, a card you choose from outside the game must come from your sideboard").
    A game from decklists without a sideboard has none: the action is impossible and nothing
    happens (no choice is offered, 608.2d). A player with a sideboard (ref.decklist.Deck): not
    implemented, fail loudly."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        if g.s.players[p].flags.get("sideboard"):
            raise Unsupported("a card from outside the game with a sideboard (CR 400.11a)")


# =============================================================================== hands

def _damage_reveal_discard(m, sc):
    """Stinging Vitriol: "~ deals 2 damage to target opponent. That player reveals their hand. You
    choose a nonland card from it. They discard that card." (CR 701.20a, 701.9b: you choose the
    discarded card)."""
    who = sc.tc.add(TargetSpec("opponent", text="target opponent"))
    sc.last_player = who
    f = et._hand_card_filter(m.group(2))
    return Seq(fx.Damage(int(m.group(1)), who, text=f"~ deals {m.group(1)} damage to target opponent"),
               fx.ChooseFromHand(who, f, "discard", kind="Discard", text=m.group(0)))


def _lost_life_reveal_or_discard(m, sc):
    """Thought-Stalker Warlock: "choose target opponent. If they lost life this turn, they reveal
    their hand, you choose a nonland card from it, and they discard that card. Otherwise, they
    discard a card." Whether they lost life this turn (not how their life total changed: ruling
    2024-07-26) is checked as the ability resolves (608.2c)."""
    who = sc.tc.add(TargetSpec("opponent", text="target opponent"))
    sc.last_player = who
    f = et._hand_card_filter(m.group(1))

    def lost(g, ctx):
        ps = fx.players_of(who, g, ctx)
        return bool(ps) and g.s.players[ps[0]].life_lost > 0
    return fx.If(lost, fx.ChooseFromHand(who, f, "discard", kind="Discard", text=m.group(0)),
                 fx.Discard(1, who, text="they discard a card"), text=m.group(0))


def _mill_instead(m, sc):
    """Exhibition Tidecaller: "target player mills three cards. If five or more mana was spent to
    cast that spell, that player mills ten cards instead." (608.2c: the mana spent to cast the
    triggering spell, as it last existed if it left the stack, Tidecaller ruling 2026-03-20)."""
    who = sc.tc.add(TargetSpec("player", text="target player"))
    sc.last_player = who
    k = num(m.group(2))
    return fx.If(lambda g, ctx, k=k: fx.spell_mana_spent(g, ctx) >= k, fx.Mill(num(m.group(3)), who),
                 fx.Mill(num(m.group(1)), who), text=m.group(0))


# =============================================================================== impulse draw, play permissions

# Alternative costs a card may be cast with from any zone (CR 702.74a evoke, 702.176a impending,
# 702.188a web-slinging: "You may cast this spell by paying [cost] rather than its mana cost", no
# zone named; unlike warp 702.185a "from your hand" or sneak 702.190a, which needs the declare
# blockers step). A card cast during a resolution with its normal costs (Chandra's +1) may also be
# cast for these (Chandra ruling 2026-08-21: "You may pay alternative costs such as emerge rather
# than the card's mana cost"; CR 608.2g: 601.2a-i are followed, 601.2b announces the alternative
# cost, 118.9a only one).
ANY_ZONE_ALT_COSTS = ("evoke", "impending", "web-slinging")


def castable_during_resolution(g, p, oid, face, how="normal"):
    """p may cast this card now as an effect instructs during a resolution (608.2g: timing
    permissions don't apply; everything else of 601.2 does: targets, prohibitions, costs)."""
    o = g.s.objs[oid]
    saved = o.face
    o.face = face
    g.s.touch()
    g.timing_ok = lambda p_, c_, how_="normal": True  # 608.2g (an instance override, removed below)
    try:
        return g.can_cast(p, oid, how, face)
    finally:
        del g.timing_ok
        o.face = saved
        g.s.touch()


def _faces_of(g, oid):
    card = DB.get(g.s.objs[oid].card)
    return card, [i for i, f in enumerate(card.faces) if "Land" not in f.types
                  and (i == 0 or card.layout in ("split", "adventure", "omen", "modal_dfc"))]


class ExileTopMayCastElse(E):
    """Chandra, Torch of Defiance: "Exile the top card of your library. You may cast that card. If
    you don't, Chandra deals 2 damage to each opponent." The card is cast as part of the
    resolution (608.2g; ruling 2026-08-21: timing permissions ignored, other restrictions not),
    paying its costs; a land can't be cast (ruling), so then the damage is dealt. Posed as the
    EUT poses it: ChooseCard `done` | `choose:<ref>` (another castable face `cast:<ref>@<face>`).
    Not cast, the card stays in exile."""

    def __init__(self, dmg, text=""):
        self.dmg = dmg
        self.text = text

    def run(self, g, ctx):
        s = g.s
        p = ctx["controller"]
        pl = s.players[p]
        cast = False
        if pl.library:
            g.top_hook(p, 1, "exile")
            res = g.move_batch([(pl.library[0], "exile", {"cause": "effect"})])
            nid = res[0] if res else None
            if nid is not None and nid in s.objs and s.objs[nid].zone == "exile":
                card, faces = _faces_of(g, nid)
                ok = [i for i in faces if castable_during_resolution(g, p, nid, i)]
                # The exiled card may be cast for an alternative cost its
                # own keyword gives (evoke, impending, web-slinging) instead of its mana cost
                # (Chandra ruling 2026-08-21; CR 608.2g, 601.2b, 118.9, 118.9a), if that way is
                # castable now (601.2a-i: targets, the alternative cost's own payability; timing
                # ignored, 608.2g). Offered as `cast:<ref>#<method>` (format §3.1's Priority form;
                # 0 = the card's face with the keyword: these cards have one castable face).
                alts = []
                if faces and 0 in faces:
                    alts = [how for how in g.alt_methods(nid, "hand")
                            if how.split(":")[0] in ANY_ZONE_ALT_COSTS
                            and castable_during_resolution(g, p, nid, 0, how)]
                if ok or alts:
                    opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(nid)}" if i == 0 else f"cast:{g.ref(nid)}@{card.faces[i].name}", (i, "normal"))
                                                  for i in ok]
                    opts += [Opt(f"cast:{g.ref(nid)}#{how}", (0, how)) for how in alts]
                    src = ctx.get("source")
                    a = g.ask(Decision(p, "ChooseCard", opts, subject=g.ref(src) if src in s.objs else None,
                                       prompt="Cast the exiled card (or done)?"))
                    if a is not None:
                        g.cast(p, nid, a[1], a[0])
                        cast = True
        if not cast:
            fx.Damage(self.dmg, fx.opponent(), text=self.text).run(g, ctx)


def _until(g, p, duration):
    """The end of a play permission (effects.play_permission_valid's record): "this turn", "until
    the end of your next turn" (CR 611.2a; turns alternate, no extra turns pending)."""
    s = g.s
    if duration == "this turn":
        return (s.turn, None)
    if duration == "end of your next turn":
        if s.extra_turns:
            raise Unsupported("play-permission durations with extra turns pending")
        return (s.turn + (2 if s.active == p else 1), None)
    raise Unsupported("play permission duration " + repr(duration))


def grant_play(g, oid, p, duration, zone="exile"):
    """A permission to play the card (CR 601.3, 305.1: a land card too; its normal costs and
    timing, Heroes' Hangout ruling 2025-09-19), for that object only (400.7)."""
    o = g.s.objs.get(oid)
    if o is None or o.zone != zone:
        return
    rec = {"player": p, "until": _until(g, p, duration), "any_type": False, "lands": True}
    if zone != "exile":
        rec["zone"] = zone
    o.flags["may_play"] = rec
    g.s.touch()


class ExileTopChooseMayPlay(E):
    """Heroes' Hangout (Date Night): "Exile the top two cards of your library. Choose one of them.
    Until the end of your next turn, you may play that card." The other stays exiled (ruling
    2025-09-19). Posed as the EUT poses it: ChooseCard `choose:<ref>` (no `done`)."""

    def __init__(self, n, duration, text=""):
        self.n = n
        self.duration = duration
        self.text = text

    def run(self, g, ctx):
        s = g.s
        p = ctx["controller"]
        pl = s.players[p]
        n = min(self.n, len(pl.library))
        if n <= 0:
            return
        g.top_hook(p, n, "exile")
        res = g.move_batch([(o, "exile", {"cause": "effect"}) for o in list(pl.library[:n])])
        ex = [r for r in res if r is not None and r in s.objs and s.objs[r].zone == "exile"]
        if not ex:
            return
        opts = [Opt(f"choose:{g.ref(o)}", o, sig=("hh", g.obj_sig(o))) for o in ex]
        a = g.ask(Decision(p, "ChooseCard", opts, prompt="Choose one of them: you may play it until the end of your next turn."))
        grant_play(g, a, p, self.duration)


class MillMayPlay(E):
    """Tablet of Discovery: "mill a card. You may play that card this turn." The milled card, in
    the graveyard (CR 701.17a; every card milled this way, ruling 2026-03-20), may be played
    from there this turn (601.3, 305.1). A milled card a replacement put into another public zone
    (Rest in Peace: exile) is still "that card" (701.17c: an effect that refers to a milled card
    finds it in the zone it moved to, if public; 701.17d), so it may be played from there."""

    def __init__(self, n, text=""):
        self.n = n
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        for oid in g.mill(p, self.n):
            o = g.s.objs.get(oid)
            if o is not None and o.zone in ("graveyard", "exile") and o.owner == p and not o.face_down:
                grant_play(g, oid, p, "this turn", zone=o.zone)


def _exile_target_may_play(m, sc):
    """Thor, God of Thunder: "exile target Equipment, instant, or sorcery card from your
    graveyard. Until the end of your next turn, you may play that card." (608.2b: an illegal target
    isn't exiled; the permission is for the card exiled, 400.7)."""
    f = _gy_noun(m.group(1))
    f.owner = "you"
    sel = sc.tc.add(TargetSpec("card", filt=f, zone="graveyard", text=f"target {m.group(1)} from your graveyard"))
    dur = m.group(2)

    def run(g, ctx, sel=sel, dur=dur):
        p = ctx["controller"]
        objs = [o for o in fx.objs_of(sel, g, ctx) if o in g.s.objs and g.s.objs[o].zone == "graveyard"]
        res = g.move_batch([(o, "exile", {"cause": "effect"}) for o in objs])
        for r in res:
            if r is not None:
                grant_play(g, r, p, dur)
    return fx.Fn_(run, text=m.group(0))


def _dmg_spell_mv(m, sc):
    """Thor: "~ deals damage equal to that spell's mana value to any target." The triggering spell's
    mana value (CR 202.3; {X} as chosen on the stack, 202.3e), as it last existed if it left the
    stack (608.2h)."""
    to = sc.tc.add(TargetSpec("any", text="any target"))

    def mv(g, ctx):
        ev = ctx.get("event") or {}
        oid = ev.get("obj")
        o = g.s.objs.get(oid)
        if o is not None and o.zone == "stack":
            from ..engine.mana import mana_value
            return mana_value(g.chars(oid).mana_cost, o.x or 0)
        c = ev.get("chars") or g.s.lki.get(oid)
        lo = g.s.lki_obj.get(oid)
        if c is None:
            return 0
        from ..engine.mana import mana_value
        return mana_value(c.mana_cost, (lo.x if lo is not None else 0) or 0)
    return fx.Damage(fx.Amount(mv, "that spell's mana value"), to, text=m.group(0))


class GainFlashback(E):
    """Stingcaster Mage, Flashback: "Target instant or sorcery card in your graveyard gains
    flashback until end of turn. The flashback cost is equal to its mana cost." The card has
    flashback (CR 702.34a: cast it from the graveyard for that cost, exiled as it leaves the
    stack) until end of turn (514.2), while it is that object (400.7). layers: the granted keyword
    off the battlefield (round2_b2.granted_off_battlefield)."""

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        for oid in fx.objs_of(self.sel, g, ctx):
            o = g.s.objs.get(oid)
            if o is not None and o.zone == "graveyard":
                o.flags["gains_flashback"] = g.s.turn
                g.s.touch()


def granted_off_battlefield(g, o, c):
    """Abilities an effect grants a card off the battlefield (called by
    layers._chars_off_battlefield): flashback "until end of turn" (GainFlashback)."""
    t = o.flags.get("gains_flashback")
    # It ends at a cleanup step's 514.2 (turn.cleanup
    # clears the flag there), not for the whole cleanup step: granted after 514.2 (a Flashback cast
    # while a cleanup trigger is on the stack, CR 514.3a), it lasts until the next cleanup step's
    # 514.2 (campaign22 1364214: the EUT offered the flashback cast, ours didn't).
    if t is None or o.zone != "graveyard" or t != g.s.turn:
        return
    # A card with its own flashback has two instances, after it (granted: timestamp order); its
    # controller chooses which cost to pay (casting.alt_methods: `flashback:2`, CR 702.34a, 118.9,
    # 601.2b; Flashback ruling 2026-03-20).
    from ..engine.ir import Keyword
    k = Keyword("flashback", value=list(c.mana_cost), text="flashback (granted: its mana cost)")
    k.alt_zone = "graveyard"
    c.abilities.append(k)


def _granted_flashback_cost(g, oid, how, c):
    """The spell of a card cast with a flashback granted to it in the graveyard (GainFlashback):
    a new object on the stack (400.7) without the granted keyword, whose flashback cost is its
    mana cost (the card's text). A card without printed flashback is cast "flashback" only that
    way (casting.alt_methods offers it from the granted keyword)."""
    o = g.s.objs.get(oid)
    if how != "flashback" or o is None or o.zone != "stack" or getattr(o, "cast_from", None) != "graveyard":
        return None
    return {"mana": list(c.mana_cost), "extra": {}}


def _gains_flashback(m, sc):
    f = _gy_noun(m.group(1))
    f.owner = "you"
    sel = sc.tc.add(TargetSpec("card", filt=f, zone="graveyard", text=f"target {m.group(1)} in your graveyard"))
    return GainFlashback(sel, text=m.group(0))


def _icetill(l, cx):
    """Icetill Explorer: "You may play lands from your graveyard." (CR 601.3, 305.1: a land play;
    the land cards its controller owns in their graveyard; casting.play_permissions)."""
    if l != "You may play lands from your graveyard.":
        return None
    from ..engine.ir import Static, RulePart
    return [Static([RulePart("play_lands_from_graveyard", "self")], text=l)]


def _pump_first_strike_one_or_two(m, sc):
    """Heroes' Hangout (Patrol Night): "One or two target creatures each get +1/+0 and gain first
    strike until end of turn." (one target word with one or two targets, chosen as cast:
    ruling 2025-09-19)."""
    sel = sc.tc.add(TargetSpec("creature", filt=Filter(types=["Creature"], text="creature"), n=(1, 2),
                               text="one or two target creatures"))
    sc.last = sel
    return fx.Pump(sel, int(m.group(1)), int(m.group(2)), keywords=[m.group(3)], text=m.group(0))


# =============================================================================== searches

def _destroy_controller_may_search(m, sc):
    """Assassin's Trophy: "Destroy target permanent an opponent controls. Its controller may search
    their library for a basic land card, put it onto the battlefield, then shuffle." Volatile
    Fault: "Destroy target nonbasic land an opponent controls. That player may search ...". The
    target's controller as it last existed (608.2h) may search even if it wasn't destroyed
    (indestructible: rulings 2018-10-05, 2023-11-10); one who doesn't search doesn't shuffle
    (ruling 2018-10-05; CR 701.23). The EUT poses Volatile Fault's as one Search with
    `done`; the harness maps our yes/no."""
    from .nouns import parse_noun
    sel = et.target_noun("target " + m.group(1), sc)
    sc.last = sel
    return Seq(fx.Destroy(sel, text=f"Destroy target {m.group(1)}."),
               fx.PlayerSearch(fx.lki_controllers(sel), parse_noun("basic land"), 1, "battlefield",
                               tapped=bool(m.group(2)), optional=True, text=m.group(0)))


def _search_untap_if_lands(m, sc):
    """Fabled Passage: "Search your library for a basic land card, put it onto the battlefield
    tapped, then shuffle. Then if you control four or more lands, untap that land." The land
    enters tapped, then is untapped (ruling 2020-06-23); it counts toward the four (Fabled
    Passage itself, sacrificed as a cost, doesn't)."""
    from .nouns import parse_noun
    k = num(m.group(1))
    found = fx.var("found")
    land = Filter(types=["Land"], text="land")

    def enough(g, ctx, k=k):
        p = ctx["controller"]
        return sum(1 for x in g.s.battlefield if g.ctrl(x) == p and "Land" in g.chars(x).types) >= k
    return Seq(fx.Search(parse_noun("basic land"), 1, "battlefield", tapped=True, text="Search your library for a basic land card"),
               fx.If(enough, fx.Tap(found, untap=True), text=m.group(0)))


def _search_behold_untap(m, sc):
    """Elven Passage: "Search your library for a basic land card, put it onto the battlefield
    tapped, then shuffle. You may behold an Elf. If you do, untap that land." (CR 701.4a behold:
    choose an Elf you control or reveal an Elf card from your hand; asked only if possible, 608.2d)."""
    from .nouns import parse_noun
    from .recent import _Behold
    return Seq(fx.Search(parse_noun("basic land"), 1, "battlefield", tapped=True, text="Search your library for a basic land card"),
               fx.MayThen(_Behold(m.group(1)), fx.Tap(fx.var("found"), untap=True), text=m.group(0)))


def _search_to_graveyard(m, sc):
    """Enroot: "Search your library for a land card, put it into your graveyard, then shuffle."
    (CR 701.23a-b)."""
    from .nouns import parse_noun
    return fx.PlayerSearch(fx.you(), parse_noun(m.group(1)), 1, "graveyard", text=m.group(0))


def _search_mv_plus_sacrificed(m, sc):
    """Repurposing Bay: "Search your library for an artifact card with mana value equal to 1 plus
    the sacrificed artifact's mana value, put that card onto the battlefield, then shuffle." The
    sacrificed artifact's mana value as it last existed on the battlefield (608.2h; {X} is 0,
    202.3e)."""
    from .nouns import parse_noun
    base = parse_noun(m.group(1))
    plus = int(m.group(2))

    def mv_ok(g, c, o, src, ctx):
        sac = ((ctx or {}).get("paid") or {}).get("sacrificed") or []
        return bool(sac) and c.mana_value == plus + sac[0].mana_value
    f = Filter(types=base.types, custom=mv_ok, text=f"{m.group(1)} card with mana value 1 plus the sacrificed artifact's")
    return fx.Search(f, 1, "battlefield", text=m.group(0))


# =============================================================================== the top N cards

def _reveal_up_to(kind, k, filt, dest):
    """A Dig callback: choose up to k cards matching filt among the cards looked at (one at a time,
    `done` to stop; the EUT's Dig), then put them into dest at the same time (CR 701.20a: a card
    put into the hand this way is revealed first)."""
    def cb(g, ctx, p, top, chosen):
        while len(chosen) < k:
            cands = [o for o in top if o not in chosen and filt.match(g, o, ctx.get("source"), ctx)]
            if not cands:
                break
            opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("dig", g.obj_sig(o))) for o in cands]
            a = g.ask(Decision(p, "Dig", opts, prompt=f"Choose up to {k}: {filt.text} (or done)."))
            if a is None:
                break
            chosen.append(a)
        if not chosen:
            return
        if dest == "hand":
            for o in chosen:
                g.emit({"type": "reveal", "obj": o, "player": p})
            g.move_batch([(o, "hand", {}) for o in chosen])
        else:
            g.move_batch([(o, "battlefield", {"controller": p}) for o in chosen])
    return cb


def _united_battlefront(m, sc):
    """United Battlefront: "Look at the top seven cards of your library. Put up to two
    noncreature, nonland permanent cards with mana value 3 or less from among them onto the
    battlefield. Put the rest on the bottom of your library in a random order." (CR 401.4: a
    random order is no choice)."""
    f = _gy_noun(m.group(3))
    return fx.Dig(num(m.group(1)), 0, order="random", reveal_creature=_reveal_up_to("Dig", num(m.group(2)), f, "battlefield"),
                  text=m.group(0))


def _pictures_of_spider_man(m, sc):
    """Pictures of Spider-Man: "look at the top five cards of your library. You may reveal up to
    two creature cards from among them and put them into your hand. Put the rest on the bottom of
    your library in a random order." (CR 701.20a)."""
    from .nouns import parse_noun
    f = parse_noun(m.group(3))
    return fx.Dig(num(m.group(1)), 0, order="random", reveal_creature=_reveal_up_to("Dig", num(m.group(2)), f, "hand"),
                  text=m.group(0))


def _dig_x_one_random(m, sc):
    """Fomori Vault: "Look at the top X cards of your library, where X is the number of artifacts
    you control. Put one of those cards into your hand and the rest on the bottom of your library
    in a random order." (X as the ability resolves, 608.2h)."""
    x = et.parse_amount_expr(m.group(1), sc)
    return fx.Dig(x, 1, order="random", text=m.group(0))


# =============================================================================== graveyard exile

def _opp_graveyards():
    def fn(g, ctx):
        p = ctx["controller"]
        return [o for q in (0, 1) if q != p for o in g.s.players[q].graveyard]
    return Sel(fn, "each opponent's graveyard", single=False)


def _exile_opp_graveyards(m, sc):
    """"Exile each opponent's graveyard." (CR 404: every card in it, at the same time)."""
    return fx.MoveTo(_opp_graveyards(), "exile", text=m.group(0))


def _exile_upto_from_graveyards(m, sc):
    """Pit of Offerings: "exile up to three target cards from graveyards." Lord Skitter: "exile up
    to one target card from an opponent's graveyard." The exile is linked to the permanent when
    another of its abilities refers to the exiled cards (CR 607.2a: Pit's mana ability)."""
    n = num(m.group(1))
    where = m.group(2)
    f = Filter(text="card")
    if where == "an opponent's graveyard":
        f.owner = "opp"
    sel = sc.tc.add(TargetSpec("card", filt=f, zone="graveyard", n=(0, n), text=m.group(0)))
    link = "exiled" if getattr(sc, "_link_exile", False) or sc.cx.name in LINK_EXILE_CARDS else None
    return fx.MoveTo(sel, "exile", link=link, text=m.group(0))


# Cards whose other ability refers to "the exiled cards" without "exiled with ~" (CR 607.2a).
LINK_EXILE_CARDS = {"Pit of Offerings"}


def _pit_mana_line(l, cx):
    if l != "{T}: Add one mana of any of the exiled cards' colors.":
        return None
    from ..engine.ir import ManaProduce, Activated, Cost, Noop

    def choices(g, oid):
        cols = set()
        for x in fx.exiled_with_ids(g, oid):
            cols |= set(g.chars(x).colors)
        return [(c,) for c in "WUBRG" if c in cols]
    return [Activated(Cost(tap=True, text="{T}"), Noop(), [], text=l, mana=ManaProduce(choices))]


def _token_cant_block(m, sc):
    """Lord Skitter: "create a 1/1 black Rat creature token with "This token can't block."" (CR
    111.4: named Rat Token; its ability is part of its definition)."""
    from .model import token_def
    p, t = int(m.group(1)), int(m.group(2))
    col = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}[m.group(3)]
    sub = m.group(4)
    mk = lambda p=p, t=t, col=col, sub=sub: token_def(sub, ("Creature",), (sub,), [col], p, t, "This token can't block.")  # noqa: E731
    sc.last = fx.var("token")
    return fx.CreateToken(mk, 1, text=m.group(0), remember="token")


class PutLinkedOntoBattlefieldAsSpirits(E):
    """Ghost Vacuum: "Put each creature card exiled with this artifact onto the battlefield under
    your control with a flying counter on it. Each of them is a 1/1 Spirit in addition to its
    other types." The cards exiled by its first ability (CR 607.2a; the link outlives the
    sacrificed artifact), together (one event); the type and base P/T effect has no duration
    (611.2a) and applies as they enter (layer 4 and 7b, 613.1d, 613.4b; PutOntoBattlefield
    enters_as), the flying counter is put on as they enter (122.1b, 614.1c)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        src = ctx.get("source")
        ids = [o for o in fx.exiled_with_ids(g, src) if "Creature" in g.chars(o).types]
        if not ids:
            return

        def spirit(g2, oid, c, ce):
            c.subtypes.add("Spirit")

        def one_one(g2, oid, c, ce):
            c.power, c.toughness = 1, 1
        sel = Sel(lambda g2, c2, ids=tuple(ids): [x for x in ids if x in g2.s.objs], "exiled creature cards", single=False)
        fx.PutOntoBattlefield(sel, ("exile",), controller="you", counters={"flying": 1},
                              enters_as={4: spirit, "7b": one_one}, text=self.text).run(g, ctx)


# =============================================================================== Roles, hands, mills

ROLES = {
    # CR 111.10j-r: a colorless Aura Role enchantment token named <name> with enchant creature and
    # its abilities ("When this token is put into a graveyard ..." for Wicked). Royal (111.10m):
    # its "has ward {1}" grant is round2_c2's static parser `_enchanted_gets_and_ward`.
    "Wicked": "Enchant creature\nEnchanted creature gets +1/+1.\n"
              "When this token is put into a graveyard from the battlefield, each opponent loses 1 life.",
    "Monster": "Enchant creature\nEnchanted creature gets +1/+1 and has trample.",
    "Royal": "Enchant creature\nEnchanted creature gets +1/+1 and has ward {1}.",
}


def role_token(name):
    from .model import token_def
    if name not in ROLES:
        raise Unsupported(f"{name} Role token")
    return token_def(name, ("Enchantment",), ("Aura", "Role"), (), None, None, ROLES[name], named=name)


def _role_compiles(name):
    """The card is supported only if its Role token's abilities are (compiled now)."""
    for f in role_token(name).faces:
        try:
            f.abilities()
        except Unsupported as e:
            raise Unsupported(f"{name} Role token: {e}")


class CreateRoleAttached(E):
    """"Create a Wicked Role token attached to target creature you control." / "Create a Royal
    Role token attached to that creature." (CR 303.7, 111.10j-r; the effect says what the Aura
    enchants, 303.4f; not cast and not targeting, so hexproof doesn't matter). An object that is
    gone or isn't a creature any more is one the Aura can't legally enchant: the token isn't
    created (303.4i). Two of one player's Roles on one permanent: the older goes to the graveyard
    (704.5z, turn.sba). Protection (702.16c, a Role it can't enchant) and several Roles created at
    once (token doubling: the controller chooses which to keep, Charming Scoundrel ruling
    2023-09-01) fail loudly. (Round 2 merge: b2's Wicked/Monster and c2's Royal Role code, one
    implementation.)"""

    def __init__(self, name, sel, text=""):
        self.name = name
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        for t in fx.objs_of(self.sel, g, ctx):
            if t not in g.s.objs or g.s.objs[t].zone != "battlefield" or "Creature" not in g.chars(t).types:
                continue  # 303.4i
            if any(a.kind == "keyword" and a.keyword == "protection" for a in g.chars(t).abilities):
                raise Unsupported("a Role created attached to a creature with protection (CR 303.4i, 702.16c)")
            ids = g.create_token(role_token(self.name), ctx["controller"], 1, attach_to=t)
            if len(ids) > 1:
                order_simultaneous_roles(g, ids)


def order_simultaneous_roles(g, ids):
    """Several Roles of one player becoming attached to one permanent at the same time (a
    token-doubling effect): their relative timestamps are in the order of their controller's
    choice (CR 613.7m, 613.7d-e), so that player chooses which one is newest and stays; the
    704.5z state-based action (CR 303.7a, turn.sba) puts the others into the graveyard
    (Charming Scoundrel ruling 2023-09-01: "that player chooses which one to keep"). The choice
    gives the kept Role the latest timestamp of the group. Identical Roles are offered once
    (obj_sig), so the usual case (copies of one Role) is a forced choice."""
    s = g.s
    groups = {}
    for i in ids:
        o = s.objs.get(i)
        if o is None or o.zone != "battlefield" or o.attached_to is None:
            continue
        groups.setdefault((o.attached_to, o.controller), []).append(i)
    for (host, p), grp in groups.items():
        if len(grp) < 2:
            continue
        opts = [Opt(f"choose:{g.ref(i)}", i, sig=("role", g.obj_sig(i))) for i in grp]
        keep = g.ask(Decision(p, "ChooseCard", opts,
                              prompt="Roles attached at the same time: choose the one to keep (CR 613.7m, 704.5z)."))
        newest = max(grp, key=lambda i: s.objs[i].timestamp)
        if keep != newest:
            s.objs[keep].timestamp, s.objs[newest].timestamp = s.objs[newest].timestamp, s.objs[keep].timestamp
            s.touch()


def _create_role(m, sc):
    from .nouns import parse_noun
    _role_compiles(m.group(1))
    f = parse_noun(m.group(2))
    sel = sc.tc.add(TargetSpec("creature", filt=f, text="target " + m.group(2)))
    return CreateRoleAttached(m.group(1), sel, text=m.group(0))


def _create_role_that(m, sc):
    _role_compiles(m.group(1))
    sel = et.obj_ref(m.group(2), sc)
    return CreateRoleAttached(m.group(1), sel, text=m.group(0))


class DiscardIfDraw(E):
    """Flush Out: "Discard a card. If you do, draw two cards." (CR 701.9a; 608.2c: no card
    discarded, no draw)."""

    def __init__(self, n, text=""):
        self.n = n
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        pl = g.s.players[p]
        if not pl.hand:
            return
        opts = [Opt(f"choose:{g.ref(o)}", o, sig=("disc", g.obj_sig(o))) for o in pl.hand]
        o = g.ask(Decision(p, "Discard", opts, prompt="Choose a card to discard."))
        g.discard(p, o, cause="effect")
        g.draw(p, self.n)


class DiscardUpToDrawThatMany(E):
    """Tersa Lightshatter: "discard up to two cards, then draw that many cards." (CR 701.9a: the
    cards are chosen one at a time, `done` to stop; "that many" are the cards discarded)."""

    def __init__(self, n, text=""):
        self.n = n
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        pl = g.s.players[p]
        k = 0
        while k < self.n and pl.hand:
            opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("disc", g.obj_sig(o))) for o in pl.hand]
            o = g.ask(Decision(p, "Discard", opts, prompt="Choose a card to discard (or done)."))
            if o is None:
                break
            g.discard(p, o, cause="effect")
            k += 1
        if k:
            g.draw(p, k)


class ExileRandomFromGraveyardMayPlay(E):
    """Tersa Lightshatter: "exile a card at random from your graveyard. You may play that card this
    turn." (CR 701.9-style random choice; the harness aligns the random pick with the EUT's,
    Rules.random_hook). Its controller pays costs and follows timing as usual (rulings 2025-04-04)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        gy = list(g.s.players[p].graveyard)
        if not gy:
            return
        hook = getattr(g, "random_hook", None)
        pick = hook(p, gy, "exile") if hook is not None else None
        if pick is None:
            pick = g.s.rng.choice(gy)
        res = g.move_batch([(pick, "exile", {"cause": "effect"})])
        if res and res[0] is not None:
            grant_play(g, res[0], p, "this turn")


class MillMayReturnPermanent(E):
    """Rapid Rescue: "Mill two cards. You may put a permanent card from among the milled cards into
    your hand." (CR 701.17a; the milled cards still in the graveyard, 400.7, or in the public zone
    a replacement put them into instead, 701.17c: exile under Rest in Peace). Posed as a Search
    with `done` (like Anzrag's Rampage's choice among exiled cards), as round2_d2.PutMilledIntoHand
    poses "from among them" (Something Worth Saving, Bristlebud Farmer)."""

    def __init__(self, n, text=""):
        self.n = n
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        milled = g.mill(p, self.n)
        perm = {"Artifact", "Battle", "Creature", "Enchantment", "Land", "Planeswalker"}
        cands = [o for o in milled if o in g.s.objs and g.s.objs[o].zone in ("graveyard", "exile") and not g.s.objs[o].face_down
                 and perm & g.chars(o).types]
        if not cands:
            return
        opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("rr", g.obj_sig(o))) for o in cands]
        a = g.ask(Decision(p, "Search", opts, subject=g.ref(ctx["source"]) if ctx.get("source") in g.s.objs else None,
                           prompt="You may put a permanent card from among the milled cards into your hand."))
        if a is not None:
            g.move(a, "hand", cause="effect")


class SurveilCounterUnlessPerCard(E):
    """Swallowed by Leviathan: "Choose target spell. Surveil 2, then counter the chosen spell unless
    its controller pays {1} for each card in your graveyard." The number is counted after the
    surveil (608.2c); with none, the cost is {0}, which may be paid (118.5)."""

    def __init__(self, sel, n, text=""):
        self.sel = sel
        self.n = n
        self.text = text

    def run(self, g, ctx):
        fx.Scry(self.n, surveil=True).run(g, ctx)
        k = len(g.s.players[ctx["controller"]].graveyard)
        fx.CounterUnlessPays([k] if k else [], 0, sel=self.sel, text=f"{{{k}}}").run(g, ctx)


def _swallowed(m, sc):
    sel = sc.tc.add(TargetSpec("spell", filt=Filter(text="spell"), zone="stack", text="target spell"))
    return SurveilCounterUnlessPerCard(sel, int(m.group(1)), text=m.group(0))


# =============================================================================== naming a card

_CARD_NAMES = None


def all_card_names():
    """Every name a player may choose for "choose a card name" (CR 201.4: a card in the Oracle card
    reference, not a token's name; 201.4b a split card's half, not both; 201.4c-f a flip card's
    alternative name, a double-faced card's back face, a meld back face, an adventurer card's
    Adventure name). Art series cards, emblems and tokens aren't cards of the game."""
    global _CARD_NAMES
    if _CARD_NAMES is None:
        import gzip
        import json
        from .model import FULL
        skip = {"token", "double_faced_token", "emblem", "art_series", "front_card"}
        out = set()
        with gzip.open(FULL, "rt") as f:
            for line in f:
                r = json.loads(line)
                if r.get("layout") in skip or "Token" in (r.get("type_line") or "").split("—")[0]:
                    continue
                faces = r.get("card_faces") or []
                if faces:
                    out.update(fc["name"] for fc in faces if fc.get("name"))
                    if r.get("layout") not in ("split", "adventure", "flip", "transform", "modal_dfc", "meld", "prepare", "omen"):
                        out.add(r["name"])
                else:
                    out.add(r["name"])
        if not out:
            raise Unsupported("choose a card name: no Oracle card list available")
        _CARD_NAMES = sorted(out)
    return _CARD_NAMES


def _has_name(g, oid, name):
    """CR 201.2: an object's name; a split card off the stack has both halves' names (709.4)."""
    c = g.chars(oid)
    n = c.name or ""
    if DB.get(g.s.objs[oid].card).layout == "split" and " // " in n:
        return name in n.split(" // ")
    return n == name


class NameSearchExile(E):
    """Ancient Vendetta: "Choose a card name. Search target opponent's graveyard, hand, and library
    for up to four cards with that name and exile them. Then that player shuffles." The name is
    chosen as the spell resolves (ruling 2025-02-07), any card name (201.4; a basic land's too:
    ruling); the cards found needn't all be exiled (ruling; 701.23b: a hidden zone may yield fewer)."""

    def __init__(self, sel, n, text=""):
        self.sel = sel
        self.n = n
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        name = g.ask(Decision(p, "NameCard", [Opt("name:" + n, n) for n in all_card_names()], prompt="Choose a card name."))
        for q in fx.players_of(self.sel, g, ctx):
            pl = g.s.players[q]
            # CR 701.23a: the searcher looks at every card of the searched hidden zones.
            g.s.know.looks(g, p, list(pl.hand) + list(pl.library))
            found = []
            while len(found) < self.n:
                cands = [o for z in (pl.graveyard, pl.hand, pl.library) for o in z
                         if o not in found and _has_name(g, o, name)]
                if not cands:
                    break
                opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("av", g.obj_sig(o))) for o in cands]
                a = g.ask(Decision(p, "Search", opts, prompt=f"Exile up to {self.n} cards named {name} (or done)."))
                if a is None:
                    break
                found.append(a)
            if found:
                g.move_batch([(o, "exile", {"cause": "effect"}) for o in found])
            g.shuffle(q)


def _ancient_vendetta(m, sc):
    sel = sc.tc.add(TargetSpec("opponent", text="target opponent"))
    sc.last_player = sel
    return NameSearchExile(sel, num(m.group(1)), text=m.group(0))


# =============================================================================== Esper Origins

class ExileSpellReturnTransformed(E):
    """Esper Origins: "exile it, then put it onto the battlefield transformed under its owner's
    control with a finality counter on it." The resolving spell is exiled (it leaves the stack,
    608.2n doesn't apply), then that card (400.7: the exiled object) enters with its back face up
    (712.14: a transforming double-faced card), the finality counter put on as it enters (614.1c,
    122.1h); a Saga back face also enters with its lore counter (714.3a)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        src = ctx.get("source")
        o = g.s.objs.get(src)
        if o is None or o.zone != "stack":
            return
        owner = o.owner
        nid = g.move(src, "exile", cause="effect")
        if nid is None or nid not in g.s.objs or g.s.objs[nid].zone != "exile":
            return
        if DB.get(g.s.objs[nid].card).layout != "transform" or g.s.objs[nid].is_copy:
            return  # 712.14a: not a transforming double-faced card: it stays in exile
        g.move(nid, "battlefield", controller=owner, face=1, counters={"finality": 1}, cause="effect")


class RevealTopPutIfPermanent(E):
    """Summon: Esper Maduin (chapter I): "Reveal the top card of your library. If it's a permanent
    card, put it into your hand." (CR 701.20a; a permanent card: 110.4b). The harness aligns the
    revealed card with the EUT's (Rules.reveal_top_hook)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        lib = g.s.players[p].library
        if not lib:
            return
        hook = getattr(g, "reveal_top_hook", None)
        if hook is not None:
            hook(p)
        top = lib[0]
        g.emit({"type": "reveal", "obj": top, "player": p})
        if {"Artifact", "Battle", "Creature", "Enchantment", "Land", "Planeswalker"} & g.chars(top).types:
            g.move(top, "hand", cause="effect")


# =============================================================================== registration

et.PATTERNS[0:0] = [
    (r"Exile up to one target nonland card of each card type from your graveyard\. Copy those cards\. "
     r"You may cast any number of spells with total mana value (\d+) or less from among the copies without paying their mana costs\.",
     _uldaros),
    (r"Return (?:up to (one|two|three) )?target (.+? cards?(?: with mana value \d+ or (?:less|greater))?) from your graveyard to "
     r"(your hand|the battlefield)( tapped)?( with a finality counter on it)?\.", _return_target_from_gy),
    (r"Return all (.+? cards(?: with mana value \d+ or less)?) from your graveyard to the battlefield( tapped)?\.", _return_all_from_gy),
    (r"Search your library for an? (basic land|land|basic Forest|basic Plains|basic Island|basic Swamp|basic Mountain) card, "
     r"put that card onto the battlefield( tapped)?, then shuffle\.", _search_put_that_card),
    (r"You may reveal exactly two cards you own with different names from outside the game\. An opponent chooses one of them\. "
     r"You put that card into your hand\.", lambda m, sc: FromOutsideTheGame(text=m.group(0))),
    (r"You may put a card you own from outside the game into your hand\.", lambda m, sc: FromOutsideTheGame(text=m.group(0))),
    (r"~ deals (\d+) damage to target opponent\. That player reveals their hand\. You choose an? ([a-z, -]+ card) from it\. "
     r"They discard that card\.", _damage_reveal_discard),
    (r"Choose target opponent\. If they lost life this turn, they reveal their hand, you choose an? ([a-z, -]+ card) from it, "
     r"and they discard that card\. Otherwise, they discard a card\.", _lost_life_reveal_or_discard),
    (r"Target player mills (\w+) cards\. If (\w+) or more mana was spent to cast that spell, that player mills (\w+) cards instead\.",
     _mill_instead),
    (r"Exile the top card of your library\. You may cast that card\. If you don't, ~ deals (\d+) damage to each opponent\.",
     lambda m, sc: ExileTopMayCastElse(int(m.group(1)), text=m.group(0))),
    (r"Exile the top two cards of your library\. Choose one of them\. Until the end of your next turn, you may play that card\.",
     lambda m, sc: ExileTopChooseMayPlay(2, "end of your next turn", text=m.group(0))),
    (r"Mill a card\. You may play that card this turn\.", lambda m, sc: MillMayPlay(1, text=m.group(0))),
    (r"Exile target (.+? card) from your graveyard\. Until the (end of your next turn), you may play that card\.", _exile_target_may_play),
    (r"~ deals damage equal to that spell's mana value to any target\.", _dmg_spell_mv),
    (r"Target (instant or sorcery card) in your graveyard gains flashback until end of turn\. The flashback cost is equal to its mana cost\.",
     _gains_flashback),
    (r"One or two target creatures each get \+(\d+)/\+(\d+) and gain (first strike|flying|trample|haste|vigilance|lifelink|deathtouch) until end of turn\.",
     _pump_first_strike_one_or_two),
    (r"Destroy target (permanent an opponent controls|nonbasic land an opponent controls)\. (?:Its controller|That player) may search their library "
     r"for a basic land card, put it onto the battlefield( tapped)?, then shuffle\.", _destroy_controller_may_search),
    (r"Search your library for a basic land card, put it onto the battlefield tapped, then shuffle\. Then if you control (\w+) or more lands, untap that land\.",
     _search_untap_if_lands),
    (r"Search your library for a basic land card, put it onto the battlefield tapped, then shuffle\. You may behold an? (\w+)\. If you do, untap that land\.",
     _search_behold_untap),
    (r"Search your library for an? (land|creature|artifact) card, put it into your graveyard, then shuffle\.", _search_to_graveyard),
    (r"Search your library for an? (artifact) card with mana value equal to (\d+) plus the sacrificed artifact's mana value, "
     r"put that card onto the battlefield, then shuffle\.", _search_mv_plus_sacrificed),
    (r"Look at the top (\w+) cards of your library\. Put up to (\w+) (.+? cards(?: with mana value \d+ or less)?) from among them onto the battlefield\. "
     r"Put the rest on the bottom of your library in a random order\.", _united_battlefront),
    (r"Look at the top (\w+) cards of your library\. You may reveal up to (\w+) (creature|land|artifact|instant and/or sorcery) cards from among them "
     r"and put them into your hand\. Put the rest on the bottom of your library in a random order\.", _pictures_of_spider_man),
    (r"Exile each opponent's graveyard\.", _exile_opp_graveyards),
    (r"Exile up to (one|two|three) target cards? from (graveyards|a graveyard|an opponent's graveyard)\.", _exile_upto_from_graveyards),
    (r"Create an? (\d+)/(\d+) (white|blue|black|red|green) ([A-Z][a-z]+) creature token with \"~ can't block\.\"", _token_cant_block),
    (r"Put each creature card exiled with ~ onto the battlefield under your control with a flying counter on it\. "
     r"Each of them is a 1/1 Spirit in addition to its other types\.", lambda m, sc: PutLinkedOntoBattlefieldAsSpirits(text=m.group(0))),
    (r"Create an? (Wicked|Monster|Royal) Role token attached to target (creature you control|creature)\.", _create_role),
    (r"Create an? (Wicked|Monster|Royal) Role token attached to (that creature)\.", _create_role_that),
    (r"Discard a card, then draw a card\.", lambda m, sc: Seq(fx.Discard(1, fx.you()), fx.Draw(1))),
    (r"Discard a card\. If you do, draw (two) cards\.", lambda m, sc: DiscardIfDraw(num(m.group(1)), text=m.group(0))),
    (r"Discard up to (two) cards, then draw that many cards\.", lambda m, sc: DiscardUpToDrawThatMany(num(m.group(1)), text=m.group(0))),
    (r"Exile a card at random from your graveyard\. You may play that card this turn\.", lambda m, sc: ExileRandomFromGraveyardMayPlay(text=m.group(0))),
    (r"Mill (two) cards\. You may put a permanent card from among the milled cards into your hand\.",
     lambda m, sc: MillMayReturnPermanent(num(m.group(1)), text=m.group(0))),
    (r"Choose target spell\. Surveil (\d+), then counter the chosen spell unless its controller pays \{1\} for each card in your graveyard\.", _swallowed),
    (r"Choose a card name\. Search target opponent's graveyard, hand, and library for up to (\w+) cards with that name and exile them\. "
     r"Then that player shuffles\.", _ancient_vendetta),
    (r"Exile it, then put it onto the battlefield transformed under its owner's control with a finality counter on it\.",
     lambda m, sc: ExileSpellReturnTransformed(text=m.group(0))),
    (r"Reveal the top card of your library\. If it's a permanent card, put it into your hand\.",
     lambda m, sc: RevealTopPutIfPermanent(text=m.group(0))),
    (r"You create (an? (?:Treasure|Food|Clue|Map|Blood|Lander|Mutagen) token)\.", lambda m, sc: fx.CreateToken(et.parse_token(m.group(1)), 1, text=m.group(0))),
    (r"Look at the top X cards of your library, where X is (.+?)\. Put one of those cards into your hand and the rest on the bottom of "
     r"your library in a random order\.", _dig_x_one_random),
]

et.MERGE_PAIRS.extend([
    (r"^[Ee]xile up to one target nonland card of each card type from your graveyard\.$", r"^Copy those cards\.$"),
    (r"of each card type from your graveyard\. Copy those cards\.$",
     r"^You may cast any number of spells with total mana value \d+ or less from among the copies without paying their mana costs\.$"),
    (r"^You may reveal exactly two cards you own with different names from outside the game\.$", r"^An opponent chooses one of them\.$"),
    (r"from outside the game\. An opponent chooses one of them\.$", r"^You put that card into your hand\.$"),
    (r"^~ deals \d+ damage to target opponent\.$", r"^That player reveals their hand\.$"),
    (r"You choose an? [a-z, -]+ card from it\.$", r"^They discard that card\.$"),
    (r"^[Cc]hoose target opponent\.$", r"^If they lost life this turn, they reveal their hand, "),
    (r"^[Ee]xile the top card of your library\.$", r"^You may cast that card\.$"),
    (r"^[Ee]xile the top card of your library\. You may cast that card\.$", r"^If you don't, ~ deals \d+ damage to each opponent\.$"),
    (r"\b[Ee]xile the top two cards of your library\.$", r"^Choose one of them\.$"),
    (r"\b[Ee]xile the top two cards of your library\. Choose one of them\.$", r"^Until the end of your next turn, you may play that card\.$"),
    (r"^[Mm]ill a card\.$", r"^You may play that card this turn\.$"),
    (r"^[Ee]xile target .+? card from your graveyard\.$", r"^Until the end of your next turn, you may play that card\.$"),
    (r"gains flashback until end of turn\.$", r"^The flashback cost is equal to its mana cost\.$"),
    (r"^Destroy target (?:permanent|nonbasic land) an opponent controls\.$", r"^(?:Its controller|That player) may search their library for a basic land card"),
    (r"^Search your library for a basic land card, put it onto the battlefield tapped, then shuffle\.$",
     r"^(?:Then if you control \w+ or more lands, untap that land|You may behold an? \w+)\.$"),
    (r"then shuffle\. You may behold an? \w+\.$", r"^If you do, untap that land\.$"),
    (r"onto the battlefield under your control with a flying counter on it\.$", r"^Each of them is a 1/1 Spirit in addition to its other types\.$"),
    (r"^[Dd]iscard a card\.$", r"^If you do, draw two cards\.$"),
    (r"^[Ee]xile a card at random from your graveyard\.$", r"^You may play that card this turn\.$"),
    (r"^[Mm]ill two cards\.$", r"^You may put a permanent card from among the milled cards into your hand\.$"),
    (r"^[Cc]hoose target spell\.$", r"^Surveil \d+, then counter the chosen spell unless its controller pays"),
    (r"^Choose a card name\.$", r"^Search target opponent's graveyard, hand, and library for up to \w+ cards with that name and exile them\.$"),
    (r"for up to \w+ cards with that name and exile them\.$", r"^Then that player shuffles\.$"),
    (r"\b[Rr]eveal the top card of your library\.$", r"^If it's a permanent card, put it into your hand\.$"),
    (r"^[Tt]arget player mills \w+ cards\.$", r"^If \w+ or more mana was spent to cast that spell, that player mills \w+ cards instead\.$"),
])

st.EXTRA_STATICS[0:0] = [_icetill]
C.LINE_HOOKS.append(_pit_mana_line)


def _condition_hook(t, cx):
    if t == "~ was cast from a graveyard":
        # Esper Origins: the spell resolving was cast from a graveyard (CR 601.2a, flashback).
        return lambda g, ctx: getattr(g.s.objs.get(ctx.get("source")), "cast_from", None) == "graveyard"
    m = re.fullmatch(r"there are (\w+) or more cards in your graveyard", t)
    if m:
        # Tersa Lightshatter (CR 603.4: checked as it triggers and as it resolves, ruling 2025-04-04).
        k = num(m.group(1))
        return lambda g, ctx, k=k: len(g.s.players[ctx["controller"]].graveyard) >= k
    return None


tg.CONDITION_HOOKS.append(_condition_hook)

from ..engine import layers as _layers  # noqa: E402
from ..engine import casting as _casting  # noqa: E402
_layers.OFF_BATTLEFIELD_HOOKS.append(granted_off_battlefield)
_casting.ALT_COST_HOOKS.append(_granted_flashback_cost)
