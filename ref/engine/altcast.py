"""Alternative ways to cast and put cards onto the battlefield: plot (CR 702.170, 116.2k), warp
(702.185), impending (702.176), preparation cards (722), ninjutsu (702.49).

Canonical option strings (interfaces/scenario-and-state.md §3.1): `special:<ref>:plot` (the plot
special action), `cast:<ref>#plot` (a plotted card from exile), `cast:<ref>#warp`,
`cast:<ref>#impending`, `cast:<permanent ref>#prepared` (the prepared permanent's prepare-spell
copy), `cast:<ref>` (a warped card from exile: its normal cost). Ninjutsu is an activated
ability that functions in the hand (`activate:<ref>:<n>`).
"""
from .decisions import Decision, Opt, IllegalAction
from .ir import Effect
from ..cards.model import DB
from .. import Unsupported

# Casting methods whose cost is the spell's mana cost (not an alternative cost, CR 118.9):
# a prepare-spell copy (722.3c) and a warped card cast from exile (702.185a).
NORMAL_COST = ("normal", "prepared", "warped")
# Casting without paying the mana cost (an alternative cost of {0}, CR 118.9, 601.2b).
FREE = ("plot", "free")  # "free": cast during a resolution (paradigm's copy, 702.192a)


def _kw(chars, name):
    for a in chars.abilities:
        if a.kind == "keyword" and a.keyword == name:
            return a
    return None


class AltCastMixin:
    # ------------------------------------------------------------------ priority options
    def extra_priority_options(self, p):
        """Special actions: plot (CR 702.170a, 116.2k: during your main phase while the stack is
        empty, any time you have priority)."""
        s = self.s
        opts = []
        if s.active == p and s.step in ("Main1", "Main2") and not s.stack:
            for oid in s.players[p].hand:
                k = _kw(self.chars(oid), "plot")
                if k is None:
                    continue
                if self.mana_payable(p, list(k.value), 0, {"kind": "special", "obj": oid}):
                    opts.append(Opt(f"special:{self.ref(oid)}:plot", ("special", oid, "plot"),
                                    sig=("plot", self.obj_sig(oid))))
        opts.extend(self.room_unlock_options(p))
        return opts

    def extra_cast_candidates(self, p):
        """(oid, how, face, canon) beyond the hand and alt_zone keywords: plotted cards (702.170d)
        and warped cards (702.185a) their owner may cast from exile, and the prepare-spell copies
        of p's prepared permanents (722.3c)."""
        s = self.s
        out = []
        for oid in s.players[p].exile:
            o = s.objs[oid]
            if o.owner != p:
                continue
            t = o.flags.get("plotted")
            if t is not None and s.turn > t:
                out.append((oid, "plot", 0, f"cast:{self.ref(oid)}#plot"))
            t = o.flags.get("warped")
            if t is not None and s.turn > t:
                out.append((oid, "warped", 0, f"cast:{self.ref(oid)}"))
        for oid in s.battlefield:
            o = s.objs[oid]
            if o.flags.get("prepared") and self.ctrl(oid) == p:
                out.append((oid, "prepared", 1, f"cast:{self.ref(oid)}#prepared"))
        # Airbent cards their owner may cast for {2} (CR 701.65a; ref/cards/batch9a.py).
        from ..cards.batch9a import airbend_candidates
        out.extend(airbend_candidates(self, p))
        return out

    def alt_timing(self, p, how):
        """A casting permission's own timing, or None for the card's normal timing. Plotted
        cards: during their owner's main phase while the stack is empty, on a later turn
        (702.170d; "Cast it as a sorcery" in the reminder)."""
        if how == "plot":
            s = self.s
            return s.active == p and s.step in ("Main1", "Main2") and not s.stack
        if how == "sneak":
            # CR 702.190a: "any time you could cast an instant during your declare blockers step"
            # (ref/cards/round2_c2.py).
            s = self.s
            return s.active == p and s.step == "DeclareBlockers"
        return None

    # ------------------------------------------------------------------ special actions
    def room_unlock_options(self, p):
        """CR 709.5e: a player who controls a permanent with a locked half may pay that half's mana
        cost to unlock it, any time they have priority and the stack is empty during a main phase
        of their turn (a special action, 116.2). `special:<ref>:unlock-left|unlock-right` (the
        EUT's labels). Rooms: ref/cards/round2_a2.py."""
        s = self.s
        opts = []
        if s.active != p or s.step not in ("Main1", "Main2") or s.stack:
            return opts
        for oid in s.battlefield:
            o = s.objs[oid]
            if self.ctrl(oid) != p or o.face_down:
                continue
            card = self.room_card(o)
            if card is None:
                continue
            for i, side in ((0, "left"), (1, "right")):
                if i in (o.flags.get("unlocked") or ()):
                    continue
                if self.mana_payable(p, list(card.faces[i].mana_cost), 0, {"kind": "special", "obj": oid}):
                    opts.append(Opt(f"special:{self.ref(oid)}:unlock-{side}", ("special", oid, f"unlock-{side}"),
                                    sig=("unlock", self.obj_sig(oid), oid, i)))
        return opts

    def room_card(self, o):
        """The Room card whose halves permanent o has (CR 709.5), or None: its own card, or, for a
        copy of a Room, the copied Room's (709.5b: the halves are copiable values;
        a token copy of a Room has locked halves it may unlock, 709.5d-e)."""
        cop = o.flags.get("copiable")
        if cop is not None:
            return DB.get(cop.room[0]) if cop.room is not None else None
        card = DB.get(o.card)
        if card.layout != "split" or "Room" not in card.faces[0].subtypes:
            return None
        return card

    def do_special(self, p, a):
        _, oid, what = a
        if what in ("unlock-left", "unlock-right"):
            # CR 709.5e, 709.5h: pay the half's mana cost; it becomes unlocked (no stack); "when
            # you unlock this door" abilities trigger.
            i = 0 if what == "unlock-left" else 1
            card = self.room_card(self.s.objs[oid])
            self.pay_mana(p, list(card.faces[i].mana_cost), 0, {"kind": "special", "obj": oid}, oid)
            o = self.s.objs[oid]
            o.flags["unlocked"] = tuple(sorted(set(o.flags.get("unlocked") or ()) | {i}))
            self.s.touch()
            self.emit({"type": "unlocked", "player": p, "obj": oid, "half": i})
            return
        if what != "plot":
            raise Unsupported(f"special action {what}")
        # CR 702.170a: exile it from your hand and pay the plot cost; it becomes plotted. A
        # special action (116.2k): no stack, no priority for anyone in between.
        k = _kw(self.chars(oid), "plot")
        cost = list(k.value)
        nid = self.move(oid, "exile", cause="plot")
        self.s.objs[nid].flags["plotted"] = self.s.turn
        self.s.touch()
        self.pay_mana(p, cost, 0, {"kind": "special", "obj": nid}, nid)
        self.emit({"type": "plotted", "player": p, "obj": nid})

    # ------------------------------------------------------------------ casting hooks
    def prepared_copy(self, p, perm):
        """CR 722.3c: the prepared permanent's controller may cast the copy of it in exile that
        has only its prepare spell's characteristics. The copy is made here, as it is cast: it
        exists exactly while the permanent stays prepared on the battlefield and nothing else
        can interact with it (a copy, not a card; it ceases to exist off the stack, 704.5e)."""
        s = self.s
        o = s.objs[perm]
        # The prepare spell of its copiable values (722.2b): its own card's, or the card it
        # copies (Superior Spider-Man as a copy of Emeritus of Ideation), "ignoring other
        # exceptions to the copying process" (722.3c).
        pname = getattr(self.base_chars(o), "prepare", None)
        if pname is None:
            raise Unsupported("prepare spell of a transformed permanent (CR 722.2b, 722.3c)")
        card = DB.get(pname)
        from .objects import Obj
        n = Obj(s.new_id(), pname, p, "exile")
        n.is_copy = True
        n.face = 1
        n.key = self.new_key(card.faces[1].name)
        n.timestamp = s.new_ts()
        n.controller = p
        # 722.3c: the prepare spell's characteristics become the copy's normal characteristics
        # (its copiable values in every zone, so also after it leaves the stack).
        n.flags["copiable"] = self.base_chars(n)
        self._prepared_from = perm
        s.objs[n.id] = n
        s.players[p].exile.append(n.id)
        s.touch()
        return n.id

    def after_cast(self, nid, it, how):
        """At 601.2i (the spell becomes cast)."""
        if how == "warp":
            # CR 702.185c: "a spell was warped this turn" (any player's; ref/cards/round2_a2.py).
            self.s.turn_flags["spell_warped"] = True
        if how == "prepared":
            po = self.s.objs.get(getattr(self, "_prepared_from", None))
            if po is not None:
                po.flags.pop("prepared", None)  # 722.3c: unprepared as the copy becomes cast
                self.s.touch()

    def permanent_resolved(self, nid, it):
        """A permanent spell became a permanent (608.3). Warp (702.185a): if its warp cost was
        paid, exile the permanent at the beginning of the next end step (a delayed triggered
        ability, 603.7; it affects only that object, 603.7c)."""
        if nid is not None and (it.ctx or {}).get("how") == "sneak":
            # CR 702.190b, 506.3a: a noncreature permanent put onto the battlefield attacking
            # is never an attacking permanent (ref/cards/round2_c2.py).
            s = self.s
            if s.combat is not None and nid in s.combat.attackers and "Creature" not in self.chars(nid).types:
                del s.combat.attackers[nid]
                s.touch()
        if nid is None or (it.ctx or {}).get("how") != "warp":
            return
        from .ir import Triggered
        from ..cards.triggers import Step
        ab = Triggered(Step("End", "each"), _WarpExile(nid, self.s.objs[nid].cast or {}), text="Warp: exile it at the beginning of the next end step")
        self.s.delayed.append({"trigger": ab.trigger, "ability": ab, "controller": it.controller,
                               "source": nid, "source_chars": self.chars(nid), "once": True,
                               "data": {}})

    # ------------------------------------------------------------------ alternative ability costs
    def free_alt_cost(self, p, oid, ab):
        """Is a {0} alternative to this ability's cost available (Kíli the Resourceful: "you may
        pay {0} rather than pay the equip cost of the first equip ability you activate each
        turn")? ref/cards/round2_c2.py."""
        from ..cards.round2_c2 import free_equip_available
        return free_equip_available(self, p, oid, ab)

    def choose_free_alt_cost(self, p, oid, ab):
        """As the ability is activated (CR 602.2b, 601.2b): whether the {0} alternative is used."""
        from ..cards.round2_c2 import choose_free_equip
        return choose_free_equip(self, p, oid, ab)

    # ------------------------------------------------------------------ power-up (702.193)
    def power_up_cost(self, oid, cost):
        """CR 702.193a-b: if this permanent entered this turn, the cost is reduced by its mana
        cost: generic by generic, each colored (or colorless) symbol by the same symbol, any
        excess reducing generic mana."""
        o = self.s.objs.get(oid)
        if o is None or o.zone != "battlefield" or o.entered_turn != self.s.turn:
            return cost
        cost = list(cost)
        excess = 0
        for sym in self.chars(oid).mana_cost:
            if isinstance(sym, int):
                excess += sym
            elif isinstance(sym, str) and sym in "WUBRGC" and len(sym) == 1:
                if sym in cost:
                    cost.remove(sym)
                else:
                    excess += 1
            else:
                raise Unsupported(f"power-up reduction by the mana symbol {sym!r} (CR 702.193b)")
        return self._reduce_generic(cost, excess) if excess else cost

    # ------------------------------------------------------------------ loyalty timing
    def loyalty_any_time(self, p, chars):
        """A rule effect lets p activate loyalty abilities of these planeswalkers any time they
        could cast an instant (Jace's Machinations; CR 606.3 still limits them to once each
        turn)."""
        if "Planeswalker" not in chars.types:
            return False
        for part, src in self.rule_parts("loyalty_any_time"):
            ctrl = src.controller if not isinstance(src, int) else self.ctrl(src)
            if ctrl == p and part.data.get("subtype") in chars.subtypes:
                return True
        return False

    # ------------------------------------------------------------------ prepared (722.3)
    def become_prepared(self, oid):
        """CR 722.3a: only a permanent with a prepare spell, and not one already prepared."""
        o = self.s.objs.get(oid)
        if o is None or o.zone != "battlefield" or o.flags.get("prepared"):
            return
        if getattr(self.base_chars(o), "prepare", None) is None:
            return  # no prepare spell among its copiable values (722.2b)
        o.flags["prepared"] = True
        self.s.touch()


class _WarpExile(Effect):
    """The warp delayed trigger's effect: exile that permanent (if it's still the same object on
    the battlefield, 603.7c). Its owner may cast it after the turn it was cast has ended, for as
    long as it remains exiled (702.185a)."""

    def __init__(self, oid, cast):
        self.oid = oid
        self.turn = cast.get("turn")

    def run(self, g, ctx):
        o = g.s.objs.get(self.oid)
        if o is None or o.zone != "battlefield":
            return
        nid = g.move(self.oid, "exile", cause="warp")
        if nid is not None and nid in g.s.objs:
            g.s.objs[nid].flags["warped"] = self.turn if self.turn is not None else g.s.turn  # 702.185a-b
            g.s.touch()
