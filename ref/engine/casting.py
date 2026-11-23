"""Priority actions: playing lands (305), casting spells (601), activating abilities (602, 605,
606), targets (115), costs (118) and mana payment (106, 601.2g-h); resolution (608)."""
from .decisions import Decision, Opt, IllegalAction, NeedDecision
from .objects import StackItem
from .mana import (normalize, expand_choices, payable, unit_assignments, Unit, mana_value,
                   COLORS)
from ..cards.model import DB
from .. import Unsupported

ALL_COLORS = ("W", "U", "B", "R", "G")
ANY_TYPE = frozenset("WUBRGC")  # a unit spendable as though it were mana of any type (CR 609.4b)



# hook(g, oid, how, chars) -> {"mana": [...], "extra": {...}} | None: alternative costs an effect
# granted to a card that its spell no longer shows (casting.alt_cost).
ALT_COST_HOOKS = []


class CastingMixin:
    # ================================================================== priority options
    def priority_options(self, p):
        s = self.s
        opts = [Opt("pass", "pass")]
        sorcery_ok = (s.active == p and s.step in ("Main1", "Main2") and not s.stack)
        # Lands (CR 305.1-305.3).
        pl = s.players[p]
        if sorcery_ok and pl.lands_played < self.land_plays(p):
            # From hand, or from exile with a permission to play it (CR 305.1, 601.3).
            for oid in list(pl.hand) + self.play_permitted(p, lands=True):
                c = self.chars(oid)
                if "Land" in c.types or self._face_has_land(oid):
                    for face in self.playable_land_faces(oid):
                        canon = f"play:{self.ref(oid)}" if face == 0 else f"play:{self.ref(oid)}@{self.face_name(oid, face)}"
                        opts.append(Opt(canon, ("play", oid, face), sig=("play", self.obj_sig(oid), face)))
            for oid in self.adventure_exiled(p):
                # CR 715.3d: an adventurer card exiled by its resolving Adventure may be played;
                # for a land card that's playing the land (305.1-305.2).
                o = s.objs[oid]
                if "Land" in DB.get(o.card).faces[0].types:
                    opts.append(Opt(f"play:{self.ref(oid)}", ("play", oid, 0), sig=("play", self.obj_sig(oid), 0)))
        # Spells (CR 601; timing 117.1a, 304.5, 307.5).
        for oid, how, face, canon in self.castable_candidates(p):
            if self.can_cast(p, oid, how, face):
                opts.append(Opt(canon, ("cast", oid, how, face), sig=("cast", self.obj_sig(oid), how, face)))
        # Special action: turn a face-down permanent face up (CR 116.2b, 702.37e, 701.40b).
        for oid in s.battlefield:
            o = s.objs[oid]
            if o.face_down and self.ctrl(oid) == p:
                cost = self.face_up_cost(oid)
                if cost is not None and self.mana_payable(p, cost, 0, {"kind": "special", "obj": oid}):
                    opts.append(Opt(f"special:{self.ref(oid)}:turn-face-up", ("special", oid, "turn-face-up"),
                                    sig=("tfu", self.obj_sig(oid), oid)))
        # Other special actions (plot, CR 116.2k): ref/engine/altcast.py.
        opts.extend(self.extra_priority_options(p))
        # Activated abilities (CR 602) and mana abilities (605.3a).
        for oid, n, ab, zone in self.activatable(p):
            if ab.is_mana:
                if not getattr(s, "mana_at_priority", False):
                    continue  # convention (scenario-and-state.md §3.1): off unless enabled
                # Not when it couldn't add mana now (Pit of Offerings' "one mana of any of the
                # exiled cards' colors" with no colored card exiled: no type can be defined, CR
                # 106.7, so it isn't a mana ability then, 605.1a). (Activating it as an ability
                # that adds nothing isn't offered either.)
                if self.can_activate_mana(p, oid, ab) and self.productions(ab, oid):
                    # Two identical mana abilities of one object (granted twice: two Great Divide
                    # Guides' "{T}: Add one mana of any color" on a Forest) are one choice, the
                    # other its alternative, as for activate: below (format §3.1).
                    # (Same text and same mana only: a shock land's intrinsic {W} and {U} abilities
                    # share a text.)
                    mkey = ("text", ab.text, tuple(sorted(self.productions(ab, oid)))) if ab.text else n
                    opts.append(Opt(f"mana:{self.ref(oid)}:{n}", ("mana", oid, ab), sig=("mana", self.obj_sig(oid), mkey)))
            elif self.can_activate(p, oid, ab):
                # Two identical abilities of one object (the same ability granted twice, e.g. by
                # two Sanctum Lurkers) are interchangeable: one option, the other its alternative.
                key = ("text", ab.text, getattr(ab, "loyalty", None)) if ab.text else n
                opts.append(Opt(f"activate:{self.ref(oid)}:{n}", ("activate", oid, ab), sig=("act", self.obj_sig(oid), key)))
        return opts

    def face_up_cost(self, oid):
        o = self.s.objs[oid]
        card = DB.get(o.card)
        front = card.faces[0]
        kind = o.face_down_kind
        if kind in ("manifest", "cloak"):
            if "Creature" not in front.types:
                return None  # 701.40b: only a creature card can be turned up this way
            return list(front.mana_cost)
        if kind in ("disguise", "morph"):
            for ab in front.abilities():
                if ab.kind == "keyword" and ab.keyword == kind:
                    return list(ab.value)
        return None

    def special_action(self, p, a):
        _, oid, what = a
        if what != "turn-face-up":
            return self.do_special(p, a)
        cost = self.face_up_cost(oid)
        self.pay_mana(p, cost, 0, {"kind": "special", "obj": oid}, oid)
        o = self.s.objs[oid]
        o.face_down = False  # 708.8: not entering the battlefield; no ETB
        o.face_down_kind = None
        self.s.touch()
        self.emit({"type": "turned_face_up", "obj": oid, "player": p})

    def land_plays(self, p):
        # CR 305.2, 305.2a: effects may allow additional land plays: "You may play an additional
        # land this turn" (turn_flags, ref/cards/round2_c2.py) and "You may play an additional land
        # on each of your turns" (the rule part "extra_land" of a permanent p controls, statics.py;
        # it was compiled but never counted: Icetill Explorer, ref/cards/round2_b2.py).
        n = self.s.players[p].land_plays + self.s.turn_flags.get(("extra_land_plays", p), 0)
        if self.s.active == p:
            for part, src in self.rule_parts("extra_land"):
                if (src.controller if not isinstance(src, int) else self.ctrl(src)) == p:
                    n += 1
        return n

    def _face_has_land(self, oid):
        o = self.s.objs[oid]
        card = DB.get(o.card)
        return any("Land" in f.types for f in card.faces)

    def playable_land_faces(self, oid):
        o = self.s.objs[oid]
        card = DB.get(o.card)
        out = []
        for i, f in enumerate(card.faces):
            if "Land" in f.types and (i == 0 or card.layout == "modal_dfc"):
                out.append(i)
        return out

    def face_name(self, oid, face):
        return DB.get(self.s.objs[oid].card).faces[face].name

    def take_action(self, p, a):
        kind = a[0]
        if kind == "play":
            self.play_land(p, a[1], a[2])
        elif kind == "cast":
            self.cast(p, a[1], a[2], a[3])
        elif kind == "activate":
            self.activate(p, a[1], a[2])
        elif kind == "mana":
            self.activate_mana(p, a[1], a[2], None)
        elif kind == "special":
            self.special_action(p, a)
        else:
            raise Unsupported(f"action {a}")

    # ================================================================== lands
    def play_land(self, p, oid, face=0):
        s = self.s
        s.players[p].lands_played += 1
        nid = self.move(oid, "battlefield", controller=p, face=face, cause="play")
        self.emit({"type": "land_played", "player": p, "obj": nid})

    # ================================================================== spells
    def castable_candidates(self, p):
        """(oid, how, face, canon) for cards p might cast: from hand; plus permissions."""
        s = self.s
        out = []
        for oid in s.players[p].hand:
            o = s.objs[oid]
            card = DB.get(o.card)
            for fi, f in enumerate(card.faces):
                if "Land" in f.types:
                    continue
                # CR 722.3: a prepare spell is never cast from the card itself (only its copy).
                if fi > 0 and card.layout not in ("modal_dfc", "adventure", "split", "omen"):
                    continue
                canon = f"cast:{self.ref(oid)}" if fi == 0 else f"cast:{self.ref(oid)}@{f.name}"
                out.append((oid, "normal", fi, canon))
            for how in self.alt_methods(oid, "hand"):
                out.append((oid, how, 0, f"cast:{self.ref(oid)}#{how}"))
        for zname in ("graveyard", "exile"):
            for oid in s.players[p].zone(zname):
                for how in self.alt_methods(oid, zname):
                    out.append((oid, how, 0, f"cast:{self.ref(oid)}#{how}"))
        for oid in self.adventure_exiled(p):
            o = s.objs[oid]
            if "Land" not in DB.get(o.card).faces[0].types:
                out.append((oid, "normal", 0, f"cast:{self.ref(oid)}"))  # CR 715.3d (a land is played, above)
        # Permissions to play cards from exile (any owner's exile): "you may play that card"
        # (Bonehoard Dracosaur, Cori Mountain Monastery), "you may look at and play those cards"
        # (Outrageous Robbery), "you may cast that card / the exiled card" (Cruelclaw's Heist,
        # Null Summoner): cast it as from a hand, with its normal costs and timing (CR 601.2,
        # 601.3, 117.1a). Land cards are played (priority_options above).
        for oid in self.play_permitted(p):
            card = DB.get(s.objs[oid].card)
            for fi, f in enumerate(card.faces):
                if "Land" in f.types:
                    continue
                # An Adventure or an Omen may be cast from exile with such a permission (CR 715.3,
                # 715.3d); a prepare spell never from the card itself (722.3).
                if fi > 0 and card.layout not in ("modal_dfc", "adventure", "split", "omen"):
                    continue
                canon = f"cast:{self.ref(oid)}" if fi == 0 else f"cast:{self.ref(oid)}@{f.name}"
                out.append((oid, "normal", fi, canon))
        out.extend(self.extra_cast_candidates(p))  # plot, warp, prepared (altcast.py)
        return out

    def adventure_exiled(self, p):
        """Cards exiled by their resolving Adventure that p may play (CR 715.3d: "its controller
        exiles it. For as long as that card remains exiled, that player may play it": the
        Adventure spell's controller, who isn't the owner when it was cast from another player's
        card, e.g. with Cruelclaw's Heist; campaign12 811867)."""
        s = self.s
        return [oid for q in (0, 1) for oid in s.players[q].exile
                if (s.objs[oid].flags.get("adventure_exile") or {}).get("player") == p]

    def play_permitted(self, p, lands=False):
        """Cards in exile p may play now (with lands=True: those whose permission covers playing
        a land card, CR 305.1)."""
        s = self.s
        # Graveyard cards too: a permission for that card (Tablet of Discovery) or "You may play
        # lands from your graveyard" (Icetill Explorer; ref/cards/round2_b2.py).
        # A lands-only permission ("only") doesn't let p cast the card's spell faces.
        return [oid for q in (0, 1) for zn in ("exile", "graveyard") for oid in s.players[q].zone(zn)
                if any((l if lands else l != "only") for _, l in self.play_permissions(p, oid))]

    def play_permissions(self, p, oid):
        """Permissions for player p to play card oid from exile: list of (any_type, lands_too).
        - flags['may_play'] (effects.play_permission_valid: player, duration, any_type, lands),
          set by the effect that exiled it or a later one; a card that leaves exile is a new
          object without it (CR 400.7);
        - rule 'may_cast_linked' of a permanent p controls: "you may cast the exiled card"
          (cards exiled by that object's linked ability, CR 607.2a)."""
        from .effects import play_permission_valid
        s = self.s
        o = s.objs.get(oid)
        if o is None or o.zone not in ("exile", "graveyard"):
            return []
        out = []
        if play_permission_valid(self, o, p):
            mp = o.flags["may_play"]
            out.append((bool(mp.get("any_type")), bool(mp.get("lands"))))
        if o.zone == "graveyard":
            # "You may play lands from your graveyard" (CR 601.3, 305.1: land cards p owns there;
            # Icetill Explorer) - a permission to play lands only.
            # An MDFC with a land face may be played as that land (712.12; a permission to play a
            # land from among cards covers any face that fits). The permission covers no spell.
            if o.owner == p and self._face_has_land(oid) and any(
                    (src.controller if not isinstance(src, int) else self.ctrl(src)) == p
                    for part, src in self.rule_parts("play_lands_from_graveyard")):
                out.append((False, "only"))
            return out
        if not any(oid in ids for ids in s.linked.values()):
            return out
        for part, src in self.rule_parts("may_cast_linked"):
            if isinstance(src, int) and self.ctrl(src) == p and oid in s.linked.get((src, "exiled"), ()):
                out.append((bool(part.data.get("any_type")), False))
        return out

    def _spend_any_type(self, spend):
        """CR 609.4b: the spell being paid for may be paid with mana as though it were mana of
        any type (it doesn't change the cost or what mana was spent)."""
        if not spend or spend.get("kind") != "spell":
            return False
        o = self.s.objs.get(spend.get("obj"))
        if o is None:
            return False
        if o.flags.get("any_type"):
            return True
        if o.zone == "exile":
            # Only the permitted player is offered the card (castable_candidates).
            return any(a for q in (0, 1) for a, _ in self.play_permissions(q, o.id))
        return False

    def alt_methods(self, oid, zone):
        """The ways to cast oid from `zone` its keywords give (`flashback`, `harmonize`, ...). A
        card with more than one instance of a keyword (its own flashback and one an effect
        granted, Stingcaster Mage, Flashback): each is an alternative cost its controller may
        choose (CR 702.34a, 118.9, 601.2b; Flashback ruling 2026-03-20 "If a card has multiple
        instances of flashback, you may choose any of its flashback costs to pay"). The n-th
        instance (printed first, then granted ones in timestamp order) is method `<keyword>:<n>`
        (format §3.1 `cast:<ref>#flashback:<n>`); one whose cost equals an earlier instance's is
        the same choice and isn't offered again."""
        c = self.chars(oid)
        out = []
        seen = {}
        for ab in c.abilities:
            if ab.kind == "keyword" and getattr(ab, "alt_zone", None) == zone:
                costs = seen.setdefault(ab.keyword, [])
                cost = sorted(map(repr, ab.value or []))
                costs.append(cost)
                if len(costs) == 1:
                    out.append(ab.keyword)
                elif cost not in costs[:-1]:
                    out.append(f"{ab.keyword}:{len(costs)}")
        return out

    def spell_face(self, oid, face):
        o = self.s.objs[oid]
        card = DB.get(o.card)
        if face and o.zone == "battlefield" and o.flags.get("copiable") is not None:
            # A prepared copy's prepare spell (CR 722.2b, 722.3c): its copiable values' card.
            pname = getattr(o.flags["copiable"], "prepare", None)
            if pname is not None:
                card = DB.get(pname)
        return card.faces[face]

    def spell_ability(self, face):
        for ab in face.abilities():
            if ab.kind == "spell":
                return ab
        return None

    def timing_ok(self, p, chars, how="normal"):
        s = self.s
        alt = self.alt_timing(p, how)
        if alt is not None:
            return alt
        if "Instant" in chars.types or chars.has_kw("flash") or self.has_flash_permission(p, chars):
            return True
        return s.active == p and s.step in ("Main1", "Main2") and not s.stack  # CR 307.5

    def has_flash_permission(self, p, chars):
        for part, src in self.rule_parts("flash_for"):
            if self.ctrl(src) == p and part.data["filter"](self, chars):
                return True
        return False

    def face_chars(self, oid, face):
        """Characteristics of a card as the chosen face would be on the stack."""
        o = self.s.objs[oid]
        if o.face_down and o.zone == "exile":
            # CR 406.3a: a card exiled face down is turned face up just before it's played.
            saved = (o.face, o.face_down)
            o.face, o.face_down = face, False
            self.s.touch()
            try:
                c = self.base_chars(o)
            finally:
                o.face, o.face_down = saved
                self.s.touch()
            return c
        if face and o.zone == "battlefield" and o.flags.get("copiable") is not None \
                and getattr(o.flags["copiable"], "prepare", None) is not None:
            # A permanent that is a copy of a preparation card: its prepare spell (CR 722.2b,
            # 722.3c) has that card's prepare-spell characteristics.
            from .objects import Obj
            t = Obj(-1, o.flags["copiable"].prepare, o.owner, "exile")
            t.face = face
            t.controller = o.controller
            return self.base_chars(t)
        if o.zone != "stack" and DB.get(o.card).layout == "split":
            # CR 709.3a-b: only the half being cast is evaluated (off the stack the card has both
            # halves' characteristics, 709.4; layers.base_chars).
            saved = o.face
            o.face = face
            o.flags["split_half"] = True
            self.s.touch()
            try:
                c = self.base_chars(o)
            finally:
                o.face = saved
                o.flags.pop("split_half", None)
                self.s.touch()
            return c
        if face == o.face:
            return self.chars(oid)
        saved = o.face
        o.face = face
        self.s.touch()
        try:
            c = self.base_chars(o)
        finally:
            o.face = saved
            self.s.touch()
        return c

    def can_cast(self, p, oid, how="normal", face=0):
        try:
            c = self.face_chars(oid, face)
            if not self.timing_ok(p, c, how):
                return False
            if self.rule_applies_player(p, "cant_cast", chars=c):
                return False
            o_ = self.s.objs[oid]
            if not (o_.zone == "hand" and o_.owner == p) and self.rule_applies_player(p, "cant_cast_outside_hand"):
                return False  # Avatar's Wrath (CR 101.2; ref/cards/batch9a.py)
            f = self.spell_face(oid, face)
            sa = self.spell_ability(f) if not ("Creature" in f.types or "Artifact" in f.types or "Enchantment" in f.types or "Planeswalker" in f.types or "Battle" in f.types) else self.spell_ability(f)
            ctx = {"source": oid, "controller": p, "x": 0, "chars": c, "casting": True}
            # Targets (CR 601.2c): some legal choice must exist.
            if not self.targets_possible(p, f, sa, ctx):
                return False
            spree_modes = ()
            if sa is not None and sa.modes is not None and sa.modes.spree:
                spree_modes = self.spree_payable_mode(p, oid, c, how, face, sa, ctx)
                if spree_modes is None:
                    return False
            cost, extra = self.spell_cost(p, oid, c, how, face, x=0, kicked=0, modes=spree_modes)
            spend = {"kind": "spell", "chars": c, "obj": oid}
            from .effects import ChoiceCost, ManaCostPart
            fixed, choices = [], []
            for part in extra.get("parts", []):
                if hasattr(part, "castable"):
                    # A cost choice that changes the mana to pay checks the mana itself.
                    if not part.castable(self, p, oid, cost, spend):
                        return False
                    return all(pt is part or pt.payable(self, p, oid) for pt in extra.get("parts", []))
                if isinstance(part, ChoiceCost):
                    if not part.optional:
                        choices.append(part.alts)
                else:
                    fixed.append(part)
            # Each way of paying the "A or B" additional costs (CR 601.2b) is checked together
            # with the mana (601.2g-h): a permanent sacrificed for one part can't also make mana
            # (e.g. "pay {4} or sacrifice an artifact" with a Treasure as the only mana source).
            # Additional mana is part of the total the reductions apply to (601.2f).
            cost_with = lambda add: self.spell_cost(p, oid, c, how, face, x=0, kicked=0, modes=spree_modes, add=add)[0]  # noqa: E731
            return self.cost_parts_feasible(p, oid, cost, fixed, choices, spend, (), life=self.cost_life(extra),
                                            cost_with=cost_with)
        except Unsupported:
            raise

    def spree_payable_mode(self, p, oid, c, how, face, sa, ctx):
        """CR 702.172a: a spree spell needs one or more modes chosen, and each chosen mode's cost
        is an additional cost, so it is paid even when the spell is cast without paying its mana
        cost (118.9d). Returns (i,) for a mode whose targets exist and whose total is payable, or
        None when there is none (the spell can't be cast)."""
        ok_modes = [i for i, m in enumerate(sa.modes.modes)
                    if all(t.n[0] == 0 or self.legal_targets(t, p, ctx, []) for t in m.targets)]
        for i in ok_modes:
            cst, _ = self.spell_cost(p, oid, c, how, face, x=0, kicked=0, modes=(i,))
            if self.mana_payable(p, cst, 0, {"kind": "spell", "chars": c, "obj": oid}):
                return (i,)
        return None

    def targets_possible(self, p, face, sa, ctx):
        if "Aura" in face.subtypes:
            ench = self.enchant_spec(face)
            return bool(self.legal_targets(ench, p, ctx, []))
        if sa is None:
            return True
        if sa.modes is not None:
            ok = [m for m in sa.modes.modes if all(self.legal_targets(t, p, ctx, []) or t.n[0] == 0 for t in m.targets)]
            return len(ok) >= max(1, sa.modes.min)
        for t in sa.targets:
            mn = t.n[0] if isinstance(t.n[0], int) else 0
            if mn > 0 and len(self.legal_targets(t, p, ctx, [])) < mn:
                return False
        return True

    def enchant_spec(self, face):
        from .ir import TargetSpec
        for ab in face.abilities():
            if ab.kind == "keyword" and ab.keyword == "enchant":
                return TargetSpec("enchant", filt=ab.value, zone="battlefield", text="enchant")
        raise Unsupported("Aura without enchant")

    def spell_cost(self, p, oid, c, how, face, x=0, kicked=0, modes=(), add=()):
        """CR 601.2f: mana cost (or alternative), plus additional costs and increases, minus
        reductions. `add`: additional mana chosen at 601.2b (e.g. "discard a card or pay {2}"),
        part of the total the reductions apply to. Returns (mana symbols, extra cost dict)."""
        f = self.spell_face(oid, face)
        extra = {}
        from .altcast import NORMAL_COST, FREE
        if how in NORMAL_COST:
            cost = list(f.mana_cost)
        elif how in FREE:
            cost = []  # without paying its mana cost (CR 118.9, 702.170d)
        else:
            alt = self.alt_cost(oid, how)
            cost = list(alt["mana"])
            extra.update(alt.get("extra", {}))
        cost += list(add)
        sa = self.spell_ability(f)
        for ab in f.abilities():
            if ab.kind == "keyword" and ab.keyword == "kicker" and kicked:
                cost += list(ab.value)
            if ab.kind == "keyword" and ab.keyword == "additional_cost":
                extra.setdefault("parts", []).append(ab.value)
        if sa is not None and sa.modes is not None and sa.modes.spree:
            for mi in modes:
                cost += list(sa.modes.modes[mi].cost)
        own = 0
        for ab in f.abilities():
            if ab.kind == "static":
                for part in ab.parts:
                    if part.kind == "rule" and part.rule == "self_cost_mod":
                        own += part.data["n"](self, oid, p)
        cost = self.apply_cost_mods(p, c, cost, oid, own=own)
        return cost, extra

    def alt_cost(self, oid, how):
        if how == "airbend":
            return {"mana": [2], "extra": {}}  # CR 701.65a, 118.9: {2} rather than its mana cost
        o = self.s.objs.get(oid)
        pre = o.flags.get("alt_cost") if o is not None else None
        if pre is not None and pre[0] == how:
            return pre[1]  # the instance chosen as it was cast (`cast`; gone from the new object, 400.7)
        kw, _, k = how.partition(":")  # `<keyword>:<n>`: its n-th instance (alt_methods)
        idx = int(k) if k.isdigit() else 1
        c = self.chars(oid)
        for ab in c.abilities:
            if ab.kind == "keyword" and ab.keyword == kw:
                idx -= 1
                if idx == 0:
                    return {"mana": ab.value or [], "extra": getattr(ab, "extra", {}) or {}}
        for hook in ALT_COST_HOOKS:  # an alternative cost granted to the card (ref/cards/round2_b2.py)
            r = hook(self, oid, how, c)
            if r is not None:
                return r
        raise Unsupported(f"alternative cost {how}")

    def apply_cost_mods(self, p, c, cost, oid, own=0):
        """Cost increases then reductions (601.2f; generic only; never below {0}). own: the
        spell's own cost modification (negative = reduction)."""
        inc = max(own, 0)
        red = max(-own, 0)
        for part, src in self.rule_parts("cost_mod"):
            d = part.data
            who = d.get("who", "you")
            sc = self.ctrl(src)
            if who == "you" and sc != p:
                continue
            if who == "opp" and sc == p:
                continue
            if d["filter"](self, c, oid, src):
                n = d["n"](self, src) if callable(d["n"]) else d["n"]
                if n > 0:
                    inc += n
                else:
                    red += -n
        cost = list(cost)
        if inc:
            cost.append(inc)
        if red:
            out = []
            for sym in cost:
                if isinstance(sym, int) and red > 0:
                    k = min(sym, red)
                    red -= k
                    if sym - k:
                        out.append(sym - k)
                else:
                    out.append(sym)
            cost = out
            if red and "X" in cost:
                # CR 601.2f: the total cost (with X's value) is reduced; X's generic mana counts.
                cost.append(("XR", red))
        return cost

    def extra_costs_payable(self, p, extra, oid):
        for part in extra.get("parts", []):
            if not self.cost_part_payable(p, part, oid):
                return False
        return True

    def cost_life(self, extra):
        return sum(getattr(pt, "life", 0) or 0 for pt in extra.get("parts", []) if not callable(getattr(pt, "life", 0)))

    def cast(self, p, oid, how="normal", face=0):
        """CR 601.2a-i."""
        s = self.s
        if how == "prepared":
            oid = self.prepared_copy(p, oid)  # CR 722.3c: the copy in exile is cast
        o = s.objs[oid]
        from_zone = o.zone
        f = self.spell_face(oid, face)
        sa = self.spell_ability(f)
        any_type = from_zone == "exile" and any(a for a, _ in self.play_permissions(p, oid))
        # A later instance of a keyword's alternative cost (`flashback:2`, alt_methods): its cost
        # as the card has it now; the spell is a new object without a granted instance (400.7).
        pre_alt = self.alt_cost(oid, how) if ":" in how else None
        # 601.2a: move to the stack.
        nid = self.move(oid, "stack", controller=p, face=face, cause="cast")
        n = s.objs[nid]
        n.cast_from = from_zone
        if pre_alt is not None:
            n.flags["alt_cost"] = (how, pre_alt)
        if any_type:
            n.flags["any_type"] = True  # CR 609.4b, for this spell's costs
        it = StackItem("spell", p, s.new_id())
        it.obj = nid
        it.key = n.key
        s.stack.append(it)
        s.touch()
        c = self.chars(nid)
        ctx = {"source": nid, "controller": p, "x": 0, "chars": c, "how": how, "item": it.id,
               "kicked": 0}
        it.ctx = ctx
        # 601.2b: modes, X, optional costs.
        modes = []
        if sa is not None and sa.modes is not None:
            modes = self.choose_modes(p, sa.modes, it, "spell")
            if modes is None:
                raise IllegalAction("no legal modes")
        ctx["modes"] = modes
        n.modes = modes
        kicked = 0
        kick = next((a for a in f.abilities() if a.kind == "keyword" and a.keyword == "kicker"), None)
        if kick is not None:
            cost_k, extra_k = self.spell_cost(p, nid, c, how, face, 0, 1, modes)
            if self.mana_payable(p, cost_k, 0, {"kind": "spell", "chars": c, "obj": nid}):
                a = self.ask(Decision(p, "OptionalCost", [Opt("cost:kicker", 1), Opt("no", 0)], subject=self.ref(nid),
                                      prompt="Pay the kicker cost?"))
                kicked = a
        ctx["kicked"] = kicked
        n.kicked = kicked
        cost, extra = self.spell_cost(p, nid, c, how, face, 0, kicked, modes)
        self._x_target_cap = None
        if sa is not None:
            for t in self.target_specs(sa, ctx):
                if t.n[0] == "X":
                    # CR 601.2c: X targets must be chosen; X can't exceed the legal targets.
                    self._x_target_cap = len(self.legal_targets(t, p, ctx, []))
        # 601.2b: announce choices among additional/optional costs (paid at 601.2h).
        from .effects import ChoiceCost, HarmonizeTap, HarmonizeChoice
        parts = []
        reserved = set()  # permanents a non-mana cost will tap (not usable for mana)
        self._base_cost = cost
        # The total with some additional mana, reductions applied to all of it (601.2f).
        self._cost_with = lambda add: self.spell_cost(p, nid, c, how, face, 0, kicked, modes, add=add)[0]
        harmonize = 0
        all_parts = extra.get("parts", [])
        spend0 = {"kind": "spell", "chars": c, "obj": nid}
        for idx, part in enumerate(all_parts):
            if isinstance(part, HarmonizeChoice):
                alt = part.choose(self, p, nid, ctx)  # checks the reduced total itself
                if alt is not None:
                    parts.append(alt)
                    ctx.setdefault("paid_additional", []).append(alt.name)
                    cost = self.reduce_total(cost, alt.power)  # 601.2f
                    harmonize += alt.power
                    reserved.add(alt.oid)
            elif getattr(part, "reserves_many", False):
                # "you may sacrifice any number of ... This spell costs {1} less to cast for each
                # permanent sacrificed this way" (Rottenmouth Viper): the permanents are chosen
                # as the cost is announced (601.2b) and reduce the total (601.2f); they may still
                # be tapped for mana (601.2g) before they're sacrificed (601.2h), except those
                # whose mana ability sacrifices them (118.3); the part checks the reduced total.
                alt = part.choose(self, p, nid, ctx, cost, reserved)
                if alt is not None:
                    parts.append(alt)
                    ctx.setdefault("paid_additional", []).append(alt.name)
                    cost = self.reduce_total(cost, alt.power)  # 601.2f
                    harmonize += alt.power
                    reserved |= set(getattr(alt, "no_mana", alt.oids))
            elif isinstance(part, ChoiceCost):
                # Offer only alternatives that leave the whole cost payable (a permanent
                # sacrificed for bargain can't also make the mana, CR 601.2b, 601.2h, 118.3).
                rest = all_parts[idx + 1:]
                later = [pt.alts + ([None] if pt.optional else []) for pt in rest if isinstance(pt, ChoiceCost)]
                fixed_rest = [pt for pt in rest if not isinstance(pt, ChoiceCost)]
                cw = lambda add, h=harmonize: self.reduce_total(self._cost_with(add), h)  # noqa: E731
                allowed = [a for a in part.alts if isinstance(a, HarmonizeTap) or
                           self.cost_parts_feasible(p, nid, cost, parts + [a] + fixed_rest, later, spend0, reserved,
                                                    cost_with=cw)]
                alt = part.choose(self, p, nid, ctx, allowed=allowed)
                if alt is not None:
                    parts.append(alt)
                    ctx.setdefault("paid_additional", []).append(alt.name)
                    # Permanents a chosen cost part will tap (teamwork) can't also make mana (118.3).
                    reserved |= set(getattr(alt, "reserve", None) or ())
                    if isinstance(alt, HarmonizeTap):
                        cost = self.reduce_total(cost, alt.power)  # 601.2f
                        harmonize += alt.power
                        reserved.add(alt.oid)
            else:
                parts.append(part)
                ctx.setdefault("paid_additional", []).append(part.name)
        self._cost_with = None
        from .effects import ManaCostPart
        add = []
        for part in list(parts):
            if isinstance(part, ManaCostPart):
                add += list(part.cost)  # 601.2f: additional mana is part of the total cost
                parts.remove(part)
        if add:
            cost = self.reduce_total(self.spell_cost(p, nid, c, how, face, 0, kicked, modes, add=add)[0], harmonize)
        extra = dict(extra)
        extra["parts"] = parts
        x = 0
        saved_ex = self._excluded
        self._excluded = set(saved_ex) | reserved
        try:
            if "X" in cost:
                x = self.choose_x(p, cost, {"kind": "spell", "chars": c, "obj": nid}, nid, extra)
        finally:
            self._excluded = saved_ex
        ctx["x"] = x
        n.x = x
        s.touch()
        # 601.2c: targets.
        if "Aura" in f.subtypes:
            spec = self.enchant_spec(f)
            if not self.choose_targets(p, [spec], it):
                raise IllegalAction("no legal target for the Aura")
        else:
            specs = self.target_specs(sa, ctx) if sa is not None else []
            if specs and not self.choose_targets(p, specs, it):
                raise IllegalAction("no legal targets")
        # A cost part that must be chosen before the mana is paid (web-slinging's returned creature
        # may be one tapped for mana in this payment, 601.2g-h; ref/cards/batch8.py).
        for part in extra.get("parts", []):
            pre = getattr(part, "pre_mana", None)
            if pre is not None:
                pre(self, p, nid, ctx, cost, x)
        # 601.2g: mana abilities are activated before costs are paid; 601.2h: pay all costs.
        self._pending_extra = extra.get("parts", [])
        saved_ex = self._excluded
        self._excluded = set(saved_ex) | reserved
        try:
            spent = self.pay_mana(p, cost, x, {"kind": "spell", "chars": c, "obj": nid}, nid)
        finally:
            self._pending_extra = []
            self._excluded = saved_ex
        self.pay_extra(p, extra, nid, ctx)
        n.mana_spent = spent
        # 601.2i: becomes cast.
        s.players[p].spells_cast.append(c.name)
        # The characteristics of each spell cast this turn as it became cast (601.2i), for "the
        # first <spell> you cast each turn" (Momo, Friendly Flier). A tuple: the state clone
        # copies turn_flags shallowly.
        s.turn_flags["cast_chars"] = tuple(s.turn_flags.get("cast_chars", ())) + ((p, self.chars(nid)),)
        if "Creature" not in c.types:
            ts = s.players[p].turn_stats  # noncreature spells cast this turn (Queen of Dale, Magebane Lizard)
            ts["noncreature_cast"] = ts.get("noncreature_cast", 0) + 1
        n.cast = {"from": from_zone, "how": how, "x": x, "kicked": kicked, "turn": s.turn}
        if ctx.get("paid_additional"):
            # Optional additional costs paid (bargain, offspring): the permanent the spell becomes
            # knows them ("if it was bargained", 702.166b; offspring 702.175a).
            n.cast["paid_additional"] = tuple(ctx["paid_additional"])
        if ctx.get("gift_to") is not None:
            n.cast["gift_to"] = ctx["gift_to"]  # CR 702.174k: the gift was promised (to that opponent)
        self.after_cast(nid, it, how)
        s.touch()
        self.emit({"type": "cast", "player": p, "obj": nid, "item": it.id, "from": from_zone,
                   "chars": self.chars(nid)})
        if it.targets:
            self.became_targets(it)

    def choose_x(self, p, cost, spend, oid, extra):
        # CR 107.3a, 601.2b: any X whose total cost the player can pay (601.2f-h). Payability is
        # monotone in X, so search for the largest one (galloping, then bisection): a fixed cap
        # of 30 hid X = 31 from 32 available mana (campaign11m 905792, Goldvein Hydra).
        cap = getattr(self, "_x_target_cap", None)
        top = cap if cap is not None else 1000

        def ok(x):
            return self.mana_payable(p, cost, x, spend, life=self.cost_life(extra))
        mx, bad, step = 0, None, 1
        while mx < top:
            x = min(mx + step, top)
            if ok(x):
                mx, step = x, step * 2
            else:
                bad = x
                break
        while bad is not None and bad - mx > 1:
            mid = (mx + bad) // 2
            if ok(mid):
                mx = mid
            else:
                bad = mid
        if cap is None and mx >= top:
            raise Unsupported(f"X of {top} or more")
        opts = [Opt(f"x:{i}", i) for i in range(mx + 1)]
        return self.ask(Decision(p, "ChooseX", opts, subject=self.ref(oid), prompt="Choose X."))

    def choose_modes(self, p, modes, it, what):
        """CR 700.2: choose modes (one at a time, then done)."""
        ctx = it.ctx
        avail = []
        for i, m in enumerate(modes.modes):
            if all((t.n[0] in (0, "X")) or self.legal_targets(t, p, ctx, []) for t in m.targets):
                avail.append(i)
        once_key = getattr(modes, "once_per_turn", None)
        if once_key is not None:
            # "Choose one that hasn't been chosen this turn": per object (a new object after a
            # zone change, CR 400.7) and per ability; modes are chosen as the trigger is put on
            # the stack (603.3c). None left: the ability is removed from the stack.
            fk = ("modes_chosen", it.source, once_key)
            before = self.s.turn_flags.get(fk, ())
            avail = [i for i in avail if i not in before]
        if len(avail) < modes.min:
            return None
        # A number of modes that depends on the game as the spell is cast (CR 601.2b: "If you
        # descended this turn, you may choose both instead", descend 8; ref/cards/batch9b.py).
        mmax = modes.max_for(self, p) if hasattr(modes, "max_for") else modes.max
        chosen = []
        while True:
            opts = []
            if len(chosen) >= modes.min:
                opts.append(Opt("done", None))
            if len(chosen) < mmax:
                for i in avail:
                    if i in chosen and not modes.repeat:
                        continue
                    if chosen and i < max(chosen):
                        continue  # canonical order: each set of modes is chosen one way
                    if modes.spree:
                        trial = chosen + [i]
                        cost, extra = self.spell_cost(p, it.obj, self.chars(it.obj), ctx.get("how", "normal"), 0, 0, 0, trial)
                        if not self.mana_payable(p, cost, 0, {"kind": "spell", "chars": self.chars(it.obj), "obj": it.obj}):
                            continue
                    opts.append(Opt(f"mode:{i + 1}", i))
            if not opts:
                break
            a = self.ask(Decision(p, "ChooseMode", opts, subject=self.ref(it.obj) if it.obj else None))
            if a is None:
                break
            chosen.append(a)
            if len(chosen) >= mmax:
                break
        if once_key is not None:
            self.s.turn_flags[fk] = tuple(before) + tuple(chosen)
        return sorted(chosen)

    # ================================================================== targets (CR 115)
    def target_specs(self, ab, ctx):
        if ab is None:
            return []
        if getattr(ab, "modes", None) is not None and ctx.get("modes") is not None:
            out = []
            for mi in ctx["modes"]:
                out.extend(ab.modes.modes[mi].targets)
            return out
        return list(ab.targets)

    def legal_targets(self, spec, p, ctx, chosen_same_word):
        """Candidates for one target word (refs: obj ids or ('player', i))."""
        s = self.s
        out = []
        if spec.what in ("player", "any", "opponent", "creature_or_player", "player_or_pw"):
            for i in (0, 1):
                if spec.what == "opponent" and i == p:
                    continue
                if spec.player_filter is not None and not spec.player_filter(self, i, ctx):
                    continue
                if self.player_hexproof(i, p):
                    continue
                if ("player", i) in chosen_same_word:
                    # CR 115.3, a player chosen once for
                    # this target word isn't offered again for "any" / creature-or-player words
                    # either (Prismari Charm's "each of one or two targets": ours offered the
                    # opponent again; campaign25 52 games, e.g. 1500953, 1503243).
                    continue
                out.append(("player", i))
            if spec.what in ("player", "opponent"):
                return [r for r in out if r not in chosen_same_word]
        zone = spec.zone
        if zone == "battlefield":
            cands = list(s.battlefield)
        elif zone == "stack":
            cands = [it.obj if it.kind == "spell" else ("stack", it.id) for it in s.stack]
        elif zone in ("graveyard", "exile", "hand"):
            cands = s.players[0].zone(zone) + s.players[1].zone(zone)
        elif zone == "stack|battlefield":
            # "target spell or creature" (Fatehold Charm): spells and permanents, one target word.
            cands = [it.obj for it in s.stack if it.kind == "spell"] + list(s.battlefield)
        else:
            raise Unsupported(f"target zone {zone}")
        for oid in cands:
            if oid in chosen_same_word:
                continue
            if oid == ctx.get("source") and zone in ("stack", "stack|battlefield"):
                continue  # CR 115.5: a spell or ability on the stack can't target itself
            if oid == ctx.get("source") and ctx.get("casting"):
                # Whether a card can be cast (can_cast, castable_now): it will be on the stack as
                # its targets are chosen (601.2a, 601.2c, 115.5), so the card itself in the
                # graveyard isn't a target (Flashback given flashback by Stingcaster Mage with no
                # other instant or sorcery card in the graveyard; campaign r2b2 981218).
                continue
            if isinstance(oid, tuple):
                it = next(x for x in s.stack if x.id == oid[1])
                if spec.filt is not None and spec.filt.match_stack(self, it, ctx):
                    out.append(oid)
                continue
            if not self.can_target_obj(spec, oid, p, ctx):
                continue
            out.append(oid)
        if getattr(spec, "same_graveyard", False) and chosen_same_word:
            # "target cards from a single graveyard" (Cease): once one is chosen, only cards in
            # that graveyard (CR 601.2c).
            first = s.objs.get(chosen_same_word[0])
            if first is not None:
                out = [r for r in out if not isinstance(r, tuple) and s.objs[r].owner == first.owner]
        return out

    def can_target_obj(self, spec, oid, p, ctx):
        s = self.s
        o = s.objs.get(oid)
        if o is None or o.phased_out:
            return False
        if spec.zone == "battlefield" and o.zone != "battlefield":
            return False
        if spec.zone == "stack" and o.zone != "stack":
            return False
        if spec.zone == "stack|battlefield" and o.zone not in ("stack", "battlefield"):
            return False
        if spec.filt is not None and not spec.filt.match(self, oid, ctx.get("source"), ctx):
            return False
        if spec.what == "any":
            c = self.chars(oid)
            if not ({"Creature", "Planeswalker", "Battle"} & c.types):
                return False
        if o.zone == "battlefield":
            # Shroud, hexproof and protection protect permanents (and players) only: a spell on
            # the stack with them can still be targeted (CR 702.18a, 702.11b, 702.11d, 702.16b;
            # Emrakul, the Promised End ruling 2025-01-24), e.g. by a counterspell.
            c = self.chars(oid)
            ctrl = c.controller
            if c.has_kw("shroud"):
                return False
            # "can be the targets of spells and abilities as though they didn't have hexproof"
            # (Nowhere to Run; CR 702.11e: "hexproof from" too).
            hf = c.kw("hexproof from")
            if (hf is not None or c.has_kw("hexproof")) and ctrl != p:
                nohex = self.rule_applies(oid, "targetable_despite_hexproof")
                if c.has_kw("hexproof") and not nohex:
                    return False  # CR 702.11b
                if hf is not None and not nohex and self.source_has_quality(ctx, hf.value):
                    return False
            if self.protected_from(oid, ctx.get("source"), ctx=ctx):
                return False
        if o.zone in ("battlefield", "stack"):
            if self.rule_applies(oid, "cant_be_targeted", ctx=ctx):
                return False
        return True

    def player_hexproof(self, i, by):
        for part, src in self.rule_parts("player_hexproof"):
            # A static ability protects its controller; a resolved effect ("You ... gain hexproof
            # until end of turn", CR 702.11c, 611.2) names its player.
            who = part.data.get("player") if part is not None and part.data.get("player") is not None else self.ctrl(src)
            if who == i and by != i:
                return True
        return False

    def source_has_quality(self, ctx, quality):
        src = ctx.get("source")
        c = ctx.get("chars") or (self.chars(src) if src in self.s.objs else None)
        if c is None:
            # A source no longer where it was (a resolved spell's reflexive "When you do" trigger,
            # Faebloom Trick): its last known information (CR 113.7a), as protection does.
            c = self.s.lki.get(src)
        if c is None:
            return False
        return quality(self, c) if callable(quality) else False

    def protected_from(self, oid, source, ctx=None):
        """CR 702.16: protection from a quality."""
        c = self.chars(oid)
        for a in c.abilities:
            if a.kind == "keyword" and a.keyword == "protection":
                sc = None
                if ctx is not None and ctx.get("chars") is not None:
                    sc = ctx["chars"]
                elif source in self.s.objs:
                    sc = self.chars(source)
                elif source in self.s.lki:
                    sc = self.s.lki[source]
                if sc is not None and a.value(self, sc):
                    return True
        return False

    def choose_targets(self, p, specs, it):
        """CR 601.2c: choose targets one at a time per target word."""
        ctx = it.ctx
        groups = []
        ctx["targets_partial"] = groups  # targets chosen so far (a target that depends on an earlier one)
        for si, spec in enumerate(specs):
            chosen = []
            mn, mx = spec.n
            if callable(mn):
                mn = mn(self, ctx)  # a target word that exists only with a promised gift (702.174m)
            if mn == "X":
                mn = ctx.get("x", 0)
            if mx == "X":
                mx = ctx.get("x", 0)
            if callable(mx):
                mx = mx(self, ctx)
            while len(chosen) < mx:
                same = list(chosen)
                if spec.distinct_from is not None:
                    for gi in spec.distinct_from:
                        if gi < len(groups):
                            same += groups[gi]
                cands = self.legal_targets(spec, p, ctx, same)
                tf = getattr(self, "_target_filter", None)
                if tf is not None:
                    cands = [r for r in cands if tf(r)]
                if not cands:
                    break
                # Only offer a target if the remaining target words can still be filled.
                rest = specs[si + 1:]
                if rest:
                    cands = [r for r in cands if self._rest_targets_ok(rest, p, ctx, groups + [chosen + [r]])]
                    if not cands:
                        break
                opts = []
                if len(chosen) >= mn:
                    opts.append(Opt("done", None))
                for r in cands:
                    sig = ("target", r if isinstance(r, tuple) else self.obj_sig(r))
                    # An ability on the stack: target:stack:N (zones.ref), one option per ability.
                    opts.append(Opt(f"target:{self.ref(r)}", r, sig=sig))
                a = self.ask(Decision(p, "Target", opts, subject=self.ref(it.obj) if it.obj else it.key,
                                      prompt=f"Choose a target: {spec.text}"))
                if a is None:
                    break
                chosen.append(a)
            if len(chosen) < mn:
                return False
            groups.append(chosen)
        for si, spec in enumerate(specs):
            dv = getattr(spec, "divide", None)
            if dv is not None and si < len(groups):
                dv(self, p, si, groups[si], it)  # CR 601.2d: the division, after the targets (round2_d2)
        it.targets = groups
        ctx["targets"] = groups
        ctx.pop("targets_partial", None)
        return True

    def _rest_targets_ok(self, rest, p, ctx, groups):
        """Can the remaining target words still get their minimum number of targets?"""
        if not rest:
            return True
        spec = rest[0]
        mn = spec.n[0]
        if mn == "X":
            mn = ctx.get("x", 0)
        if not isinstance(mn, int) or mn <= 0:
            return self._rest_targets_ok(rest[1:], p, ctx, groups + [[]])
        same = []
        if spec.distinct_from is not None:
            for gi in spec.distinct_from:
                if gi < len(groups):
                    same += groups[gi]
        cands = self.legal_targets(spec, p, ctx, same)
        if len(cands) < mn:
            return False
        if mn == 1:
            return any(self._rest_targets_ok(rest[1:], p, ctx, groups + [[r]]) for r in cands[:12])
        return self._rest_targets_ok(rest[1:], p, ctx, groups + [cands[:mn]])

    def became_targets(self, it, copy=False):
        """`copy`: a copy put on the stack with its targets (CR 707.10c): its targets become
        targets, but nobody cast, activated or put a triggered ability on the stack (700.13)."""
        # One event per object or player, however many of the spell's or ability's targets it is: an
        # object "becomes the target of a spell" once (CR 702.21a's and 603.2's event is the spell
        # targeting it; rulings, Tectonic Giant 2019-07-12 "If a spell targets Tectonic Giant more
        # than once, its ability triggers only once", Cactarantula, Tenured Concocter, Pawpatch
        # Recruit). Campaign17 1000604: Vibrant Outburst with both targets on a ward creature.
        seen = []
        for g in it.targets:
            for r in g:
                if r in seen:
                    continue
                seen.append(r)
                self.emit({"type": "targeted", "target": r, "item": it.id, "controller": it.controller,
                           "kind": it.kind, "source": it.obj if it.kind == "spell" else it.source, "copy": copy})

    def target_still_legal(self, it, spec, r):
        """CR 608.2b: a target that changed zones is a new object (illegal); otherwise recheck."""
        p = it.controller
        ctx = dict(it.ctx)
        ctx["resolving"] = True
        if isinstance(r, tuple):
            if r[0] == "player":
                return r in self.legal_targets(spec, p, ctx, [])
            return any(x.id == r[1] for x in self.s.stack)
        return self.can_target_obj(spec, r, p, ctx)

    # ================================================================== costs (CR 118)
    def cost_part_payable(self, p, part, oid):
        return part.payable(self, p, oid)

    def pay_extra(self, p, extra, oid, ctx):
        for part in extra.get("parts", []):
            part.pay(self, p, oid, ctx)

    def can_pay_cost(self, p, oid, cost, ctx=None, for_mana=False):
        """Non-mana parts of an activated ability's cost."""
        s = self.s
        o = s.objs.get(oid)
        c = self.chars(oid)
        if getattr(cost, "extra_part", None) is not None and not cost.extra_part.payable(self, p, oid):
            return False
        if cost.tap or cost.untap:
            if o.zone != "battlefield":
                return False
            if cost.tap and o.tapped:
                return False
            if cost.untap and not o.tapped:
                return False
            if "Creature" in c.types and o.sick and not c.has_kw("haste"):
                return False  # CR 302.6
        if cost.life and s.players[p].life < (cost.life if isinstance(cost.life, int) else 0):
            return False  # 119.4
        if cost.sacrifice is not None:
            f, n = cost.sacrifice
            cands = [x for x in s.battlefield if self.ctrl(x) == p and f.match(self, x, oid, ctx or {})]
            if len(cands) < n:
                return False
        if cost.discard is not None:
            f, n = cost.discard
            cands = [x for x in s.players[p].hand if x != oid and f.match(self, x, oid, ctx or {})]
            if len(cands) < n:
                return False
        if cost.exile_from_gy is not None:
            f, n = cost.exile_from_gy
            # Cards only: a token in the graveyard (sacrificed for this cost's mana, until
            # state-based actions) isn't a card and can't be exiled (CR 111.6, 111.8).
            cands = [x for x in s.players[p].graveyard if x != oid and not s.objs[x].token and f.match(self, x, oid, ctx or {})]
            if len(cands) < n:
                return False
        if cost.remove_counters is not None:
            kind, n = cost.remove_counters
            have = sum(v for v in o.counters.values() if v > 0) if kind is None else o.counters.get(kind, 0)
            if have < n:
                return False
        if cost.loyalty is not None and cost.loyalty < 0:
            if o.counters.get("loyalty", 0) < -cost.loyalty:
                return False
        if cost.tap_others is not None:
            f, n = cost.tap_others
            # CR 302.6 restricts only a creature's own {T}/{Q} abilities: a summoning-sick
            # creature can be tapped to pay another permanent's cost.
            cands = [x for x in s.battlefield if x != oid and self.ctrl(x) == p and not s.objs[x].tapped
                     and x not in self._keep_untapped and f.match(self, x, oid, ctx or {})]
            if len(cands) < n:
                return False
        if cost.discard_self and (o.zone != "hand"):
            return False
        if cost.return_to_hand is not None:
            # "Return an unblocked attacking creature you control to its owner's hand" (ninjutsu,
            # CR 702.49a).
            f, n = cost.return_to_hand
            if len([x for x in s.battlefield if x != oid and f.match(self, x, oid, ctx or {})]) < n:
                return False
        if cost.blight is not None:
            raise Unsupported("blight cost")
        if getattr(cost, "crew", None) is not None and not self.crew_payable(p, oid, cost.crew):
            return False  # CR 702.122a (ref/cards/batch8.py: CrewMixin)
        return True

    def _crew_cands(self, p, oid):
        s = self.s
        return [x for x in s.battlefield if x != oid and self.ctrl(x) == p and not s.objs[x].tapped
                and not s.objs[x].phased_out and "Creature" in self.chars(x).types
                and x not in self._keep_untapped and x not in self._excluded]

    def crew_payable(self, p, oid, n):
        """CR 702.122a: "Tap any number of other untapped creatures you control with total power N
        or greater" (summoning sickness doesn't matter: 302.6 is about a creature's own {T})."""
        return sum(max(0, self.crew_power(x)) for x in self._crew_cands(p, oid)) >= n

    @staticmethod
    def _same_plain_mana(a, b):
        """Two mana abilities that differ in nothing a payment can tell apart: {T}-only costs, no
        spend restriction, no rider."""
        from ..cards.batch8 import _tap_only
        return (a is not None and b is not None and _tap_only(a.cost) and _tap_only(b.cost)
                and a.mana.restrict is None and b.mana.restrict is None
                and a.mana.rider is None and b.mana.rider is None)

    def _bigger_same_mana(self, srcs_all, oid, ab, pr, spend):
        """Another {T}-only mana ability of the same source making more of the same one color,
        usable for this payment (its spend restriction allows it, CR 106.6): (ability, product),
        the largest; None if there's none (Tablet of Discovery's {R}{R} for an instant)."""
        if len(set(pr)) != 1:
            return None
        best = None
        for t in srcs_all:
            if t[0] != oid or t[1] is ab or not self._same_tap_mana(ab, t[1]):
                continue
            ab2 = t[1]
            if ab2.mana.restrict is not None and not ab2.mana.restrict(self, dict(spend, mana_source=oid)):
                continue
            for pr2 in t[3]:
                if len(set(pr2)) == 1 and pr2[0] == pr[0] and len(pr2) > len(pr) and (best is None or len(pr2) > len(best[1])):
                    best = (ab2, pr2)
        return best

    @staticmethod
    def _same_tap_mana(a, b):
        """Two {T}-only mana abilities without riders (they may differ in amount and spend
        restriction)."""
        from ..cards.batch8 import _tap_only
        return (a is not None and b is not None and a is not b and _tap_only(a.cost) and _tap_only(b.cost)
                and a.mana.rider is None and b.mana.rider is None)

    def crew_power(self, x):
        """A creature's power for crew (CR 702.122a), plus "crews Vehicles as though its power
        were N greater" (Defend the Rider's Pilot token; rule crew_power_bonus, round2_c2.py)."""
        pw = self.chars(x).power or 0
        for part, src in self.rule_parts("crew_power_bonus"):
            if self._affects(part, src, x):
                pw += part.data["n"]
        return pw

    def pay_crew(self, p, oid, n):
        """Crew's cost, one creature at a time (PayCost choose:<ref>; done once the total power
        is N or greater). A creature is offered only if the total can still reach N."""
        chosen, total = [], 0
        while True:
            cands = [x for x in self._crew_cands(p, oid) if x not in chosen]
            rest = {x: max(0, self.crew_power(x)) for x in cands}
            avail = sum(rest.values())
            opts = [Opt("done", None)] if total >= n else []
            for x in cands:
                pw = self.crew_power(x)
                if total + pw + avail - rest[x] >= n:
                    opts.append(Opt(f"choose:{self.ref(x)}", x, sig=("crew", self.obj_sig(x))))
            if not opts or (len(opts) == 1 and opts[0].data is None):
                break
            x = self.ask(Decision(p, "PayCost", opts, subject=self.ref(oid),
                                  prompt=f"Crew {n}: choose a creature to tap (total power {total})."))
            if x is None:
                break
            chosen.append(x)
            total += self.crew_power(x)
        for x in chosen:
            self.tap(x)
        self.s.turn_flags["helper_taps"] = tuple(self.s.turn_flags.get("helper_taps", ())) + tuple(chosen)
        return [self.chars(x) for x in chosen]

    def pay_cost(self, p, oid, cost, ctx, item=None):
        """Pay non-mana parts (CR 601.2h/602.2b): choices are PayCost decisions."""
        s = self.s
        o = s.objs[oid]
        paid = {}
        if cost.tap:
            self.tap(oid)
        if cost.untap:
            self.untap(oid)
        if cost.loyalty is not None:
            if cost.loyalty > 0:
                self.add_counters(oid, "loyalty", cost.loyalty)
            elif cost.loyalty < 0:
                self.remove_counters(oid, "loyalty", -cost.loyalty)
        if getattr(cost, "loyalty_x", False):
            self.remove_counters(oid, "loyalty", (ctx or {}).get("x", 0))  # [-X] (CR 606.4)
        if cost.remove_counters is not None:
            kind, n = cost.remove_counters
            if kind is not None:
                self.remove_counters(oid, kind, n)
            else:
                for _ in range(n):  # CR 118.3: counters of any kind; the player chooses which
                    kinds = sorted(k for k, v in o.counters.items() if v > 0)
                    k = kinds[0] if len(kinds) == 1 else self.ask(Decision(
                        p, "RemoveCounters", [Opt(f"counter:{k}", k) for k in kinds], subject=self.ref(oid),
                        prompt="Choose a counter to remove."))
                    self.remove_counters(oid, k, 1)
        if cost.put_counters is not None:
            kind, n = cost.put_counters
            self.add_counters(oid, kind, n, source=oid)
        if cost.life:
            self.pay_life(p, cost.life if isinstance(cost.life, int) else cost.life(self, ctx))
        if cost.tap_others is not None:
            f, n = cost.tap_others
            for k in range(n):
                # Not the permanent whose own {T} the activation being paid for still needs
                # (601.2g before 601.2h, 602.2b; 118.3), nor one reserved for the cost being paid
                # (tapping it here would leave that cost unpayable, CR 601.2h).
                cands = [x for x in s.battlefield if x != oid and self.ctrl(x) == p and not s.objs[x].tapped
                         and x not in self._keep_untapped and x not in self._excluded
                         and f.match(self, x, oid, ctx)]
                paying = getattr(self, "_paying", None)
                if paying is not None and len(cands) > 1:
                    slots, generic, spend, prod, later = paying
                    ok = []
                    for x in cands:
                        srcs = [t for t in later if t[0] not in (oid, x)]
                        saved_ex = self._excluded
                        self._excluded = set(saved_ex) | {oid, x}
                        try:
                            pool = self._pool_units(p, spend)
                            unit_vars = [pool + vu for vu in self._unit_variants(srcs, spend)]
                        finally:
                            self._excluded = saved_ex
                        states = [(slots, generic)]
                        for col in prod:
                            states = [st2 for st in states for st2 in unit_assignments(st[0], st[1], col)] + states
                        if any(payable(sl, ge, units) for units in unit_vars for sl, ge in states):
                            ok.append(x)
                    cands = ok or cands
                x = self.ask(Decision(p, "PayCost", [Opt(f"choose:{self.ref(x)}", x, sig=("pc", self.obj_sig(x))) for x in cands],
                                      subject=self.ref(oid), prompt=f"Choose a permanent to tap for the cost ({n - k} left)."))
                self.tap(x)
                paid.setdefault("tapped", []).append(x)  # station reads the tapped creature (702.184a)
                # Bookkeeping for the differential harness (which permanents were tapped for a
                # cost rather than for mana; forced picks aren't visible to it). Immutable: the
                # state clone copies turn_flags shallowly.
                s.turn_flags["helper_taps"] = tuple(s.turn_flags.get("helper_taps", ())) + (x,)
        if cost.return_to_hand is not None:
            f, n = cost.return_to_hand
            for _ in range(n):
                cands = [x for x in s.battlefield if x != oid and f.match(self, x, oid, ctx)]
                x = self.ask(Decision(p, "PayCost", [Opt(f"choose:{self.ref(x)}", x, sig=("pc", self.obj_sig(x))) for x in cands],
                                      subject=self.ref(oid), prompt="Choose a permanent to return to its owner's hand for the cost."))
                # What it was attacking (ninjutsu puts the card onto the battlefield attacking the
                # same player, planeswalker or battle, 702.49c).
                dfd = s.combat.attackers.get(x) if s.combat is not None else None
                paid.setdefault("returned", []).append((x, dfd))
                self.move(x, "hand", cause="cost")
        if cost.discard is not None:
            f, n = cost.discard
            for _ in range(n):
                cands = [x for x in s.players[p].hand if x != oid and f.match(self, x, oid, ctx)]
                x = self.ask(Decision(p, "PayCost", [Opt(f"choose:{self.ref(x)}", x, sig=("pc", self.obj_sig(x))) for x in cands],
                                      subject=self.ref(oid), prompt="Choose a card to discard for the cost."))
                paid.setdefault("discarded", []).append(self.discard(p, x, cause="cost"))
        if cost.discard_self:
            self.discard(p, oid, cause="cost")
        if cost.exile_from_gy is not None:
            f, n = cost.exile_from_gy
            for _ in range(n):
                cands = [x for x in s.players[p].graveyard if x != oid and not s.objs[x].token and f.match(self, x, oid, ctx)]
                x = self.ask(Decision(p, "PayCost", [Opt(f"choose:{self.ref(x)}", x, sig=("pc", self.obj_sig(x))) for x in cands],
                                      subject=self.ref(oid), prompt="Choose a card to exile for the cost."))
                paid.setdefault("exiled", []).append(self.move(x, "exile", cause="cost"))
        if cost.sacrifice is not None:
            f, n = cost.sacrifice
            chosen = []
            for _ in range(n):
                cands = [x for x in s.battlefield if self.ctrl(x) == p and x not in chosen and f.match(self, x, oid, ctx)]
                x = self.ask(Decision(p, "PayCost", [Opt(f"choose:{self.ref(x)}", x, sig=("pc", self.obj_sig(x))) for x in cands],
                                      subject=self.ref(oid), prompt="Choose a permanent to sacrifice for the cost."))
                chosen.append(x)
                paid.setdefault("sacrificed", []).append(self.chars(x))
            # One cost's permanents are sacrificed together (CR 701.21a; 603.10a: a "whenever you
            # sacrifice" ability of one of them sees the others).
            self.sacrifice_batch(chosen)
        if getattr(cost, "crew", None) is not None:
            paid["crewed_by"] = self.pay_crew(p, oid, cost.crew)  # CR 702.122a-c
        if getattr(cost, "extra_part", None) is not None:
            # A card-specific cost part with its own choices (craft's materials, CR 702.167a;
            # ref/cards/round2_c2.py): paid before "exile this permanent".
            hk = getattr(self, "hooks", None)
            if cost.exile_self and getattr(cost.extra_part, "name", None) == "craft" \
                    and getattr(hk, "o16_craft_self_first", False):
                # Opt-in alignment for lockstep runs (Hooks.o16_craft_self_first; off by default, never
                # used by ref.corpus): exile the crafting permanent before the materials (the printed
                # order; CR 601.2h lets the player choose). The materials are still chosen first; only
                # the moves change.
                def _self_first():
                    paid["exiled_self"] = self.move(oid, "exile", cause="cost")
                ctx["_o16_exile_self_first"] = _self_first
            paid.update(cost.extra_part.pay(self, p, oid, ctx) or {})
            ctx.pop("_o16_exile_self_first", None)
        if cost.exile_self and "exiled_self" not in paid:
            paid["exiled_self"] = self.move(oid, "exile", cause="cost")  # "this card" for craft (702.167a)
        if cost.sac_self:
            paid["sacrificed_self"] = self.chars(oid)
            # What was attached to it as it left ("an Equipment that was attached to Zack Fair").
            paid["attached_to_self"] = [x for x in s.battlefield if s.objs[x].attached_to == oid]
            self.sacrifice(oid)
        ctx["paid"] = paid
        return paid

    # ================================================================== mana (CR 106, 605)
    def mana_sources(self, p, exclude=()):
        """[(oid, ability, ability_index, [production tuples])] for p's usable mana abilities."""
        s = self.s
        out = []
        exclude = set(exclude) | self._excluded
        blocked = self.rule_parts("cant_activate")  # e.g. Clarion Conqueror (mana abilities too)
        for oid in s.battlefield:
            if oid in exclude:
                continue
            o = s.objs[oid]
            if o.phased_out:
                continue
            c = self.chars(oid)
            if c.controller != p:
                continue
            if blocked and self.rule_applies(oid, "cant_activate", mana=True):
                continue
            n = 0
            for ab in c.abilities:
                if ab.kind != "activated":
                    continue
                if not ab.is_mana:
                    continue
                n += 1
                if ab.cost.mana:
                    continue  # filter abilities ({1}: add ...) are not used as sources here
                if ab.condition is not None and not ab.condition(self, oid, p):
                    continue
                if not self.can_pay_cost(p, oid, ab.cost, for_mana=True):
                    continue
                out.append((oid, ab, n, self.productions(ab, oid)))
        return out

    def productions(self, ab, oid):
        m = ab.mana
        if m.any_color:
            k = m.amount or 1
            return [tuple([c] * k) for c in ALL_COLORS]
        if callable(m.choices):
            return m.choices(self, oid)
        return list(m.choices)

    def _unit_variants(self, srcs, spend):
        """Mana units the sources can still produce, as a list of alternative unit lists (the
        cost is payable if it's payable from any of them). All {T} mana abilities of one
        permanent share a single activation (tapping it once), so they count as one source
        whose units may be any of the abilities' productions. A source whose cost taps another
        untapped permanent ("{T}, Tap an untapped permanent you control: Add one mana of any
        color") needs a distinct helper (CR 118.3, 605.1a): a permanent that isn't otherwise
        producing mana adds a unit; tapping a mana source instead only converts that source's
        mana (no net gain); tapping another such source gives one unit for the two."""
        units = []
        by_obj = {}
        helpers_needed = []
        s = self.s
        for oid, ab, n, prods in srcs:
            if ab.mana.restrict is not None and not ab.mana.restrict(self, dict(spend, mana_source=oid)):
                continue
            if not prods:
                continue
            if ab.cost.tap_others is not None:
                helpers_needed.append((oid, ab, prods))
                continue
            key = oid if ab.cost.tap else (oid, n)
            by_obj.setdefault(key, []).extend(prods)
        unit_of = {}  # permanent -> index of its single unit (all {T} abilities share one tap)
        owner = []    # units[i] comes from permanent owner[i] (None: not tied to one tap)
        for key, prods in by_obj.items():
            k = max(len(pr) for pr in prods)
            if k == 1:
                if isinstance(key, int):
                    unit_of[key] = len(units)
                owner.append(key if isinstance(key, int) else None)
                units.append(frozenset(pr[0] for pr in prods))
                continue
            best = max(prods, key=len)
            for col in best:
                owner.append(None)
                units.append(frozenset([col]))
        if not helpers_needed:
            return [units]
        src_objs = {k if isinstance(k, int) else k[0] for k in by_obj}
        tapx_objs = {h[0] for h in helpers_needed}
        p = self.ctrl(helpers_needed[0][0])
        free = [x for x in s.battlefield if self.ctrl(x) == p and not s.objs[x].tapped
                and x not in src_objs and x not in tapx_objs and x not in self._excluded]
        seen_tapx = set()
        pending = []  # (oid, filter, colors): sources that add a unit given a distinct helper
        widen = []    # (oid, filter, colors): a second {T} ability of a permanent with a unit
        for oid, ab, prods in helpers_needed:
            f, need = ab.cost.tap_others
            cols = frozenset(c for pr in prods for c in pr)
            if ab.cost.tap and (oid in unit_of or oid in seen_tapx):
                # The permanent's other {T} mana ability already counts it once: with a free
                # helper this ability only widens that unit's colors (one tap).
                if oid in unit_of:
                    widen.append((oid, f, cols))
                continue
            if ab.cost.tap:
                seen_tapx.add(oid)
            pending.append((oid, f, cols))
        # Free helpers by maximum matching (each permanent taps once, CR 118.3): a greedy first
        # pick could take the only creature Springleaf Drum accepts for a source that takes any
        # permanent.
        cand = [[x for x in free if f.match(self, x, oid, {})] for oid, f, _ in pending]
        match = {}  # helper -> index into pending

        def augment(i, seen):
            for x in cand[i]:
                if x in seen:
                    continue
                seen.add(x)
                if x not in match or augment(match[x], seen):
                    match[x] = i
                    return True
            return False

        # Sources that can't fall back on converting a mana source (Springleaf Drum needs a
        # creature; the lands can't serve it) get a free helper first; Kuhn's algorithm keeps
        # every source it matched, so the order decides which ones are left to convert (Gene
        # Pollinator taps a Forest, the Drum the only non-mana creature: White Auracite).
        def can_convert(i):
            oid, f, cols = pending[i]
            return any(owner[j] is not None and owner[j] != oid and not s.objs[owner[j]].tapped
                       and f.match(self, owner[j], oid, {}) for j in range(len(units)))
        order0 = sorted(range(len(pending)), key=lambda i: (can_convert(i), len(cand[i])))
        for i in order0:
            augment(i, set())
        # The order also decides which sources are left to serve
        # each other (one taps the other for its cost, one unit for the two, CR 118.3). With a Forest
        # used first, two Drums, a Gene Pollinator and a summoning-sick Pollinator (the only free
        # helper, 302.6): the free one went to the Pollinator and the Drums (which need a creature)
        # couldn't pair, one unit; given to a Drum, the other Drum taps the Pollinator, two units.
        # Gray Ogre was offered and its payment then stuck (mtgref-mutate4 104500-259). For a few
        # unmatched sources every order is tried and the one with the most units (matched + pairs) kept;
        # the order above wins ties.
        # (o14-helper-matching-with-pairs, rt6-f 1231869, is the same fix found independently; merged here)
        if (free or any(cand)) and 1 < len(pending) <= 5 and len(set(match.values())) < len(pending):
            import itertools

            def units_of(m):
                got = set(m.values())
                rest_ = [t for i, t in enumerate(pending) if i not in got]
                return len(got) + len(self._max_tapx_pairs(rest_, src_objs))
            best_m, best_n = dict(match), units_of(match)
            for perm in itertools.permutations(range(len(pending))):
                match.clear()
                for i in perm:
                    augment(i, set())
                k = units_of(match)
                if k > best_n:
                    best_m, best_n = dict(match), k
            match.clear()
            match.update(best_m)
        matched = set(match.values())
        free = [x for x in free if x not in match]
        for i, (oid, f, cols) in enumerate(pending):
            if i in matched:
                units.append(cols)
                owner.append(None)
        for oid, f, cols in widen:
            ok = [x for x in free if f.match(self, x, oid, {})]
            if ok:
                free.remove(ok[0])
                units[unit_of[oid]] = units[unit_of[oid]] | cols
        # Two unmatched sources can serve each other: one is tapped as the other's helper (Gene
        # Pollinator tapping a second Gene Pollinator, Springleaf Drum tapping a Gene
        # Pollinator: CR 118.3, 605.1a), one unit for both. A source that is also an ordinary
        # mana source isn't used up this way (its own unit would be lost).
        rest = [t for i, t in enumerate(pending) if i not in matched]
        used = set()
        pairs = []  # (source, partner tapped for its cost, colors)
        for oid, f, cols in rest:
            if oid in used:
                continue
            partner = next((o2 for o2, _, _ in rest if o2 != oid and o2 not in used and o2 not in src_objs
                            and not s.objs[o2].tapped and f.match(self, o2, oid, {})), None)
            if partner is not None:
                used |= {oid, partner}
                pairs.append((oid, partner, cols))
        # The first-come pairing above depends on the sources' order: two Gene Pollinators
        # paired with each other leave two Springleaf Drums (which need a creature) without a
        # partner, while each Drum tapping a Pollinator makes two mana (CR 118.3; campaign10
        # 765293, castable in battlefield order, stuck in pay_mana's canonical order). Use a
        # maximum pairing when it has more pairs.
        if len(rest) <= 8 and len(pairs) < len(rest) // 2:
            best = self._max_tapx_pairs(rest, src_objs)
            if len(best) > len(pairs):
                pairs = best
        # Pairing adds a wild unit but uses up two sources; the same sources converting mana
        # sources instead add none but fix colors (two Gene Pollinators tapping two Forests for
        # {W}{W}): each number of pairs is its own scenario, all pairs first.
        scenarios = []
        for m in range(len(pairs), -1, -1):
            in_pair = {x for a, b, _ in pairs[:m] for x in (a, b)}
            scenarios.append((units + [c for _, _, c in pairs[:m]], owner + [None] * m,
                              [(cols, f, oid) for oid, f, cols in rest if oid not in in_pair]))
        # Tap a mana-producing permanent as the helper (it must match the cost's filter, e.g.
        # "an untapped creature" for Springleaf Drum, CR 118.3): its single unit may then be
        # `cols` instead (no net gain). Which one is converted depends on the cost (Forest +
        # Plains + Gene Pollinator pay {G}{G} only by tapping the Plains for the cost), so every
        # distinct choice is kept as an alternative unit list (capped).
        variants, seen_all = [], set()
        for us0, own, convert in scenarios:
            alts = [(tuple(us0), frozenset())]
            for cols, f, tapx in convert:
                targets = [i for i, u in enumerate(us0) if own[i] is not None and own[i] != tapx
                           and not cols <= u and not s.objs[own[i]].tapped and f.match(self, own[i], tapx, {})]
                if not targets:
                    continue
                nxt, seen = [], set()
                for us, conv in alts:
                    kept = False
                    for i in targets:
                        if i in conv:
                            continue
                        u2 = list(us)
                        u2[i] = us[i] | cols
                        k = tuple(sorted(tuple(sorted(u)) for u in u2))
                        kept = True
                        if k in seen:
                            continue  # converting a Forest or another Forest: the same units
                        seen.add(k)
                        nxt.append((tuple(u2), conv | {i}))
                    if not kept:
                        nxt.append((us, conv))  # every target already converted: unchanged
                alts = (nxt or alts)[:64]
            for us, _ in alts:
                k = (len(us), tuple(sorted(tuple(sorted(u)) for u in us)))
                if k not in seen_all:
                    seen_all.add(k)
                    variants.append(list(us))
        return variants[:64] or [units]

    def _max_tapx_pairs(self, rest, src_objs):
        """Most pairs of "tap an untapped permanent/creature" sources (oid, filter, colors) in
        which one source taps the other for its cost (each permanent taps once, CR 118.3), as
        (source, partner, colors) in rest's order of the source."""
        s = self.s
        n = len(rest)

        def can(i, j):  # rest[i]'s cost taps rest[j]
            o1, f, _ = rest[i]
            o2 = rest[j][0]
            return o2 != o1 and o2 not in src_objs and not s.objs[o2].tapped and f.match(self, o2, o1, {})
        best = []

        def rec(i, used, cur):
            nonlocal best
            while i < n and i in used:
                i += 1
            free = sum(1 for k in range(i, n) if k not in used)
            if len(cur) + free // 2 <= len(best):
                return
            if i >= n:
                best = list(cur)
                return
            used.add(i)
            for j in range(i + 1, n):
                if j in used:
                    continue
                for a, b in ((i, j), (j, i)):
                    if can(a, b):
                        used.add(j)
                        cur.append((rest[a][0], rest[b][0], rest[a][2]))
                        rec(i + 1, used, cur)
                        cur.pop()
                        used.discard(j)
                        break
            used.discard(i)
            rec(i + 1, used | {i}, cur)  # i stays unpaired
        rec(0, set(), [])
        return best

    def _units_from_sources(self, srcs, spend):
        """The first variant of _unit_variants (no tapX source paired with another)."""
        return self._unit_variants(srcs, spend)[0]

    def _payable_any(self, prefix, srcs, spend, states):
        """Some (slots, generic) state in `states` is payable from `prefix` units plus one of
        the sources' unit variants."""
        for vu in self._unit_variants(srcs, spend):
            units = list(prefix) + vu
            if self._spend_any_type(spend):
                units = [ANY_TYPE] * len(units)  # CR 609.4b
            if any(payable(sl, ge, units) for sl, ge in states):
                return True
        return False

    def _unit_assignments(self, slots, generic, color, spend):
        """Ways to pay one unit of `color` into the cost; with CR 609.4b ("as though it were
        mana of any type") any slot, {C} included."""
        if self._spend_any_type(spend):
            out, seen = [], set()
            for i, sl in enumerate(slots):
                if sl not in seen:
                    seen.add(sl)
                    out.append((slots[:i] + slots[i + 1:], generic))
            if generic > 0:
                out.append((slots, generic - 1))
            return out
        return unit_assignments(slots, generic, color)

    def _pool_units(self, p, spend):
        out = []
        for u in self.s.players[p].pool:
            if u.restrict is not None and not u.restrict(self, dict(spend, mana_source=u.source)):
                continue
            out.append(frozenset(u.color))
        return out

    def mana_payable(self, p, cost, x, spend, life=0, exclude=()):
        s = self.s
        saved = self._excluded
        self._excluded = set(saved) | set(exclude)
        try:
            pool = self._pool_units(p, spend)
            unit_vars = [pool + vu for vu in self._unit_variants(self.mana_sources(p), spend)]
        finally:
            self._excluded = saved
        if self._spend_any_type(spend):
            unit_vars = [[ANY_TYPE] * len(u) for u in unit_vars]  # CR 609.4b
        for variant, life_extra in expand_choices(cost, s.players[p].life - life):
            slots, generic = normalize(variant, x)
            if any(payable(slots, generic, units) for units in unit_vars):
                return True
        return False

    def can_activate_mana(self, p, oid, ab):
        o = self.s.objs[oid]
        if ab.cost.mana:
            return False
        if self.rule_applies(oid, "cant_activate", mana=True):
            return False
        if ab.condition is not None and not ab.condition(self, oid, p):
            return False
        return self.can_pay_cost(p, oid, ab.cost, for_mana=True)

    def activate_mana(self, p, oid, ab, prod, spend=None):
        """CR 605.3: mana abilities don't use the stack. Returns produced Units."""
        if prod is None:
            prods = self.productions(ab, oid)
            if len(prods) == 1:
                prod = prods[0]
            else:
                opts = [Opt(f"color:{''.join(pr)}", pr) for pr in prods]
                prod = self.ask(Decision(p, "ChooseColor", opts, subject=self.ref(oid)))
        ctx = {"source": oid, "controller": p}
        self.pay_cost(p, oid, ab.cost, ctx)
        units = [Unit(c, source=oid, restrict=ab.mana.restrict, rider=ab.mana.rider) for c in prod]
        self.s.players[p].pool.extend(units)
        self.emit({"type": "mana", "player": p, "source": oid, "units": list(prod)})
        if getattr(ab, "effect", None) is not None and not isinstance(ab.effect, type(None)):
            from .ir import Noop
            if not isinstance(ab.effect, Noop):
                ab.effect.run(self, ctx)
        return units

    def pay_mana(self, p, cost, x, spend, subject):
        """Interactive mana payment (601.2g-h): pick pool mana or a mana source one at a time.
        Returns {color: n} mana spent."""
        s = self.s
        pl = s.players[p]
        variants = list(expand_choices(cost, pl.life))
        feasible = []
        pool_u = self._pool_units(p, spend)
        all_srcs = self.mana_sources(p)
        for v, life in variants:
            sl, ge = normalize(v, x)
            if self._payable_any(self._pool_units(p, spend), self.mana_sources(p), spend, [(sl, ge)]):
                feasible.append((v, life))
        if not feasible:
            raise IllegalAction(f"can't pay {cost}")
        if len(feasible) > 1:
            opts = [Opt(f"variant:{i + 1}", fv, sig=repr(fv)) for i, fv in enumerate(feasible)]
            v, life = self.ask(Decision(p, "PayVariant", opts, subject=self.ref(subject),
                                        prompt="Choose how to pay hybrid/Phyrexian symbols."))
        else:
            v, life = feasible[0]
        if life:
            self.pay_life(p, life)
        slots, generic = normalize(v, x)
        spent = {}
        order = {}
        last = -1
        paid_srcs = []
        first_asked = None
        while slots or generic:
            # Canonical order (format §3.1): after using a source, only sources later in a fixed
            # order are offered, so each set of sources is reachable by one sequence of picks and
            # a payment with a single possible set of sources is forced. A source that adds more
            # than one mana sorts last: a payment may use it for less than it makes (the rest
            # stays in the pool, CR 106.4, 601.2h), and such a set is reachable only if no source
            # of the set comes after it (campaign11 795680: a Treasure for {1}, then Hedron
            # Archive's {C}{C} for the other {1}).
            srcs_all = sorted(self.mana_sources(p), key=self._pay_order_key)
            for idx, t in enumerate(srcs_all):
                order.setdefault((t[0], t[2]), self._pay_order_key(t))
            opts = []
            pool_units = [u for u in pl.pool if u.restrict is None or u.restrict(self, dict(spend, mana_source=u.source))]
            seen = set()
            for u in pool_units:
                if u.color in seen:
                    continue
                for sl2, ge2 in self._unit_assignments(slots, generic, u.color, spend):
                    rest = list(pool_units)
                    rest.remove(u)
                    later = [t for t in srcs_all if last == -1 or order[(t[0], t[2])] > last]
                    if self._payable_any([frozenset(r.color) for r in rest], later, spend, [(sl2, ge2)]):
                        seen.add(u.color)
                        opts.append(Opt(f"pool:{u.color}", ("pool", u.color), sig=("pool", u.color)))
                        break
            for oid, ab, n, prods in srcs_all:
                if ab.mana.restrict is not None and not ab.mana.restrict(self, dict(spend, mana_source=oid)):
                    continue
                if last != -1 and order[(oid, n)] <= last:
                    continue
                if ab.cost.sac_self and not self._extra_ok_without(p, oid, subject):
                    continue  # sacrificing it for mana would leave a sacrifice cost unpayable
                mine = order[(oid, n)]
                others = [t for t in srcs_all if t[0] != oid and order[(t[0], t[2])] > mine]
                others = self._cap_sac_sources(p, oid, ab, others, subject)
                # A source whose cost taps another permanent reserves that helper: check each
                # possible helper (it can't produce mana or serve another source afterwards).
                helper_choices = [None]
                if ab.cost.tap_others is not None:
                    f, _ = ab.cost.tap_others
                    helper_choices = [x for x in s.battlefield if x != oid and self.ctrl(x) == p and not s.objs[x].tapped
                                      and x not in self._keep_untapped and f.match(self, x, oid, {})]
                    # Only a sample is tried: helpers that tap no mana source this payment can
                    # still reach come first, and identical permanents are one helper (the first
                    # eight untapped lands alone would hide Spectral Sailor, Seam Rip: a second
                    # Gene Pollinator then only converts a land's mana, campaign11 799585;
                    # CR 118.3, 605.1a).
                    reach = {t[0] for t in others}
                    seen_h, hc = set(), []
                    for x in sorted(helper_choices, key=lambda x: x in reach):
                        hs = repr(self.obj_sig(x))
                        if hs not in seen_h:
                            seen_h.add(hs)
                            hc.append(x)
                    helper_choices = hc[:8]
                saved_ex = self._excluded
                useful = []
                try:
                    for pr in prods:
                        for h in helper_choices:
                            self._excluded = set(saved_ex) | {oid} | ({h} if h is not None else set())
                            oth = [t for t in others if t[0] != h]
                            if self._source_useful(slots, generic, pr, pool_units, oth, spend):
                                useful.append(pr)
                                break
                finally:
                    self._excluded = saved_ex
                for pr in useful:
                    if True:
                        col = pr[0] if len(set(pr)) == 1 else "".join(pr)  # {C}{C} -> "C" (format §3.1)
                        canon = f"mana:{self.ref(oid)}:{col}"
                        prev = next((o for o in opts if o.canon == canon), None)
                        if prev is not None and self._same_plain_mana(prev.data[2], ab):
                            # Two {T}-only mana abilities of one source making the same mana with
                            # no restriction or rider (an Island and Great Divide Guide's granted
                            # "{T}: Add one mana of any color"): one option (round2_c2).
                            continue
                        if prev is not None and prev.data[1] == oid and self._same_tap_mana(prev.data[2], ab):
                            # Two {T}-only mana abilities of one source making one color in
                            # different amounts (Tablet of Discovery's {R} and its instant/sorcery
                            # {R}{R}): one option (format §3.1), at the first one's place in the
                            # canonical order; the source then makes the largest amount this
                            # payment may use (_bigger_same_mana; the rest stays in the pool, CR
                            # 106.4, 601.2h, so the smaller one is never better).
                            continue
                        opts.append(Opt(canon, ("src", oid, ab, pr),
                                        sig=("mana", self.obj_sig(oid), n, pr)))
            if not opts:
                raise IllegalAction("mana payment stuck")
            dm = Decision(p, "PayMana", opts, subject=self.ref(subject),
                          prompt=f"Pay mana ({self._fmt_remaining(slots, generic)} left).")
            dm.units_left = len(slots) + generic  # metadata for the differential harness
            dm.cost_left = (tuple(slots), generic)  # (same) what is left to pay
            dm.pool_units = [(u.color, u.restrict is not None) for u in pool_units]  # (same) spendable pool
            dm.paid_srcs = list(paid_srcs)        # sources used so far in this payment (same)
            if first_asked is None and len(dm.options) > 1:
                first_asked = len(paid_srcs)
            dm.paid_first = first_asked           # paid_srcs entries before our first real choice
            a = self.ask(dm)
            if a[0] == "pool":
                # Mana that lasts until end of combat (firebending, CR 702.189a) is spent after
                # other mana of the same type: which unit pays is the payer's choice (601.2h),
                # and this one is never worse (ref/cards/batch9a.py FireUnit).
                # Likewise mana with a spending
                # restriction that allows this payment ("Spend this mana only to cast a creature
                # spell", Rockface Village) is spent before unrestricted mana of that type: the one
                # `pool:<color>` option stands for both, and this choice is never worse (the
                # unrestricted unit left can pay for anything the restricted one could; the EUT
                # spends it first: rt6-t 1312270, Hired Claw's {1}{R} after Viashino Pyromancer).
                u = next(u for u in sorted(pl.pool, key=lambda x: (getattr(x, "restrict", None) is None,
                                                                   bool(getattr(x, "until_eoc", False))))
                         if u.color == a[1] and (u.restrict is None or u.restrict(self, dict(spend, mana_source=u.source))))
                later = [t for t in srcs_all if last == -1 or order[(t[0], t[2])] > last]
                slots, generic = self._spend_unit(p, u, slots, generic, spend, srcs=later)
                self._apply_rider(u, spend)
                spent[u.color] = spent.get(u.color, 0) + 1
            else:
                _, oid, ab, pr = a
                paid_srcs.append(oid)
                last = order[(oid, next(t[2] for t in srcs_all if t[0] == oid and t[1] is ab))]
                big = self._bigger_same_mana(srcs_all, oid, ab, pr, spend)
                if big is not None:
                    ab, pr = big  # format §3.1: the largest amount this payment may spend
                later_srcs = [t for t in srcs_all if order[(t[0], t[2])] > last]
                self._paying = (slots, generic, spend, pr, later_srcs)
                try:
                    units = self.activate_mana(p, oid, ab, pr, spend)
                finally:
                    self._paying = None
                later = [t for t in self.mana_sources(p) if (t[0], t[2]) in order and order[(t[0], t[2])] > last]
                for u in units:
                    if not (slots or generic):
                        break
                    if u.restrict is not None and not u.restrict(self, dict(spend, mana_source=u.source)):
                        continue
                    r = self._spend_unit(p, u, slots, generic, spend, required=False, srcs=later)
                    if r is not None:
                        slots, generic = r
                        self._apply_rider(u, spend)
                        spent[u.color] = spent.get(u.color, 0) + 1
        return spent

    def cost_parts_feasible(self, p, oid, cost, parts, later_alts, spend, reserved, life=0, cost_with=None):
        """Can the total cost be paid with these non-mana parts plus one alternative of each
        later "A or B" choice (None = an optional cost not paid)? Permanents a sacrifice part
        needs and `reserved` ones are kept out of the mana (CR 601.2g-h, 118.3). `cost_with(add)`:
        the total with additional mana `add`, cost reductions applied to all of it (601.2f)."""
        import itertools
        from .effects import ManaCostPart, HarmonizeTap
        for combo in itertools.product(*later_alts):
            ps = list(parts) + [a for a in combo if a is not None]
            if any(isinstance(pt, HarmonizeTap) for pt in ps):
                return True  # its own castable() check covers the reduced mana (601.2f)
            add = []
            ok = True
            for pt in ps:
                if isinstance(pt, ManaCostPart):
                    add += list(pt.cost)
                elif not pt.payable(self, p, oid):
                    ok = False
                    break
            total = (cost_with(add) if cost_with is not None else cost + add) if add else cost
            if ok and self.mana_payable(p, total, 0, spend, life=life,
                                        exclude=tuple(set(self._sac_reserved(p, {"parts": ps}, oid)) | set(reserved))):
                return True
        return False

    def _own_cost_exclusion(self, oid, ab):
        """Objects an activated ability's own cost keeps out of its mana payment. Mana abilities
        are activated before the costs are paid (CR 601.2g, then 601.2h; 602.2b):
        - "{T}" in the cost: the source can't be tapped for mana or as another mana ability's
          tap helper first, or the {T} couldn't be paid (118.3);
        - "Sacrifice ~": tapping the source for mana first is fine (a tapped permanent can still
          be sacrificed), e.g. Spectacular Spider-Man tapped for Springleaf Drum, then sacrificed;
          only a mana ability of the source that sacrifices it can't be used (it can't be
          sacrificed twice; the "can't sacrifice a ... to pay multiple costs" rulings)."""
        if ab.cost.tap:
            return (oid,)
        if ab.cost.sac_self:
            if any(a.kind == "activated" and a.is_mana and a.cost.sac_self for a in self.chars(oid).abilities):
                return (oid,)
        return ()

    def _sac_reserved(self, p, extra, subject):
        """Mana sources that sacrifice themselves and that a sacrifice cost (601.2h) needs:
        beyond the cost's slack they can't be spent on mana (castability, cf. _cap_sac_sources)."""
        from .effects import SacCost
        out = set()
        for part in extra.get("parts", []):
            if not isinstance(part, SacCost):
                continue
            cands = {x for x in self.s.battlefield if self.ctrl(x) == p
                     and part.filt.match(self, x, subject, {"controller": p})}
            slack = max(0, len(cands) - part.n)
            sac_srcs = sorted({t[0] for t in self.mana_sources(p) if t[1].cost.sac_self and t[0] in cands})
            out.update(sac_srcs[slack:])
        return tuple(out)

    def _cap_sac_sources(self, p, oid, ab, others, subject):
        """A pending sacrifice cost (601.2h, paid after mana) needs enough of its candidates
        left: later sources that sacrifice themselves for mana and are candidates may be used
        only within the slack (e.g. two Treasures and "sacrifice an artifact": one of them)."""
        from .effects import SacCost
        parts = [pt for pt in (getattr(self, "_pending_extra", []) or []) if isinstance(pt, SacCost)]
        if not parts:
            return others
        out = list(others)
        for part in parts:
            cands = {x for x in self.s.battlefield if self.ctrl(x) == p
                     and part.filt.match(self, x, subject, {"controller": p})}
            slack = len(cands) - part.n
            if ab.cost.sac_self and oid in cands:
                slack -= 1
            kept = []
            for t in out:
                if t[1].cost.sac_self and t[0] in cands:
                    if slack <= 0:
                        continue
                    slack -= 1
                kept.append(t)
            out = kept
        return out

    def _extra_ok_without(self, p, gone, subject):
        """Can the pending non-mana costs (601.2h) still be paid if permanent `gone` is
        sacrificed for mana first?"""
        from .effects import SacCost
        for part in getattr(self, "_pending_extra", []) or []:
            if isinstance(part, SacCost):
                cands = [x for x in self.s.battlefield if x != gone and self.ctrl(x) == p
                         and part.filt.match(self, x, subject, {"controller": p})]
                if len(cands) < part.n:
                    return False
        return True

    def _apply_rider(self, u, spend):
        if u.rider is not None:
            u.rider(self, spend)

    def _fmt_remaining(self, slots, generic):
        out = "".join("{%s}" % "/".join(sorted(s)) for s in slots)
        return (f"{{{generic}}}" if generic else "") + out or "{0}"

    def _spend_unit(self, p, u, slots, generic, spend, required=True, srcs=None):
        pl = self.s.players[p]
        pool_rest = [x for x in pl.pool if x is not u and (x.restrict is None or x.restrict(self, dict(spend, mana_source=x.source)))]
        best = None
        if srcs is None:
            srcs = self.mana_sources(p)
        for sl2, ge2 in self._unit_assignments(slots, generic, u.color, spend):
            if self._payable_any([frozenset(r.color) for r in pool_rest], srcs, spend, [(sl2, ge2)]):
                best = (sl2, ge2)
                break
        if best is None:
            if required:
                raise IllegalAction("unit can't be spent")
            return None
        pl.pool.remove(u)
        return best

    def _pay_order_key(self, t):
        """pay_mana's canonical order of a mana source entry (oid, ability, n, productions):
        sources adding more than one mana last, then by interchangeability signature."""
        return (min((len(pr) for pr in t[3]), default=1) > 1, repr(self.obj_sig(t[0])), t[0], t[2])

    def _source_useful(self, slots, generic, prod, pool_units, other_sources, spend):
        """Would tapping this source for `prod` keep the remaining cost payable, and pay part
        of it?"""
        # The produced units must contribute (at least one assignable), and the rest stays payable.
        states = [(slots, generic)]
        for color in prod:
            nxt = []
            for sl, ge in states:
                for sl2, ge2 in self._unit_assignments(sl, ge, color, spend):
                    nxt.append((sl2, ge2))
                nxt.append((sl, ge))  # unit floats unused
            states = nxt
        states = [st for st in states if st != (slots, generic)]
        return self._payable_any([frozenset(u.color) for u in pool_units], other_sources, spend, states)

    # ================================================================== activated abilities
    def activatable(self, p):
        """(oid, n, ability, zone): n = 1-based index among non-mana activated abilities (or
        among mana abilities for mana:)."""
        s = self.s
        out = []
        objs = [oid for oid in s.battlefield if self.ctrl(oid) == p]
        for zname in ("hand", "graveyard", "exile"):
            objs += list(s.players[p].zone(zname))
        for oid in objs:
            o = s.objs[oid]
            c = self.chars(oid)
            na = nm = 0
            for ab in c.abilities:
                if ab.kind != "activated":
                    continue
                if ab.is_mana:
                    nm += 1
                    if o.zone == "battlefield":
                        out.append((oid, nm, ab, o.zone))
                else:
                    na += 1
                    if o.zone in ab.zones:
                        out.append((oid, na, ab, o.zone))
        return out

    def ability_cost_mana(self, p, oid, ab, targets=None):
        """CR 602.2b, 601.2f: the total cost is determined after targets are chosen, so
        modifiers may depend on them (`filter_t`, given the chosen target groups)."""
        cost = list(ab.cost.mana)
        if getattr(ab, "power_up", False):
            cost = self.power_up_cost(oid, cost)  # CR 702.193a-b (altcast.py)
        for part, src in self.rule_parts("ability_cost_mod"):
            ft = part.data.get("filter_t")
            hit = ft(self, oid, ab, src, targets or []) if ft is not None else part.data["filter"](self, oid, ab, src)
            if hit:
                n = part.data["n"]
                if callable(n):
                    # An amount that depends on the game or the targets ("{1} less for each
                    # Equipment you control", round2_d2); 0 changes nothing.
                    n = n(self, oid, ab, src, targets or [])
                    if n == 0:
                        continue
                cost = self._reduce_generic(cost, -n) if n < 0 else cost + [n]
        return cost

    def _target_cost_mods(self, oid, ab):
        """Are there cost modifiers of this ability that depend on its targets?"""
        return any(part.data.get("filter_t") is not None and part.data.get("may_apply", lambda g, o, a, sr: True)(self, oid, ab, src)
                   for part, src in self.rule_parts("ability_cost_mod"))

    def reduce_total(self, cost, red):
        """Reduce a total cost by `red` generic mana (CR 601.2f; X's generic mana counts)."""
        if red <= 0:
            return list(cost)
        out = []
        for sym in cost:
            if isinstance(sym, int) and red > 0:
                k = min(sym, red)
                red -= k
                if sym - k:
                    out.append(sym - k)
            else:
                out.append(sym)
        if red and "X" in out:
            out.append(("XR", red))
        return out

    def _reduce_generic(self, cost, red):
        out = []
        for sym in cost:
            if isinstance(sym, int) and red > 0:
                k = min(sym, red)
                red -= k
                if sym - k:
                    out.append(sym - k)
            else:
                out.append(sym)
        return out

    def can_activate(self, p, oid, ab):
        s = self.s
        o = s.objs[oid]
        c = self.chars(oid)
        if ab.timing == "sorcery" or ab.loyalty is not None:
            if not (s.active == p and s.step in ("Main1", "Main2") and not s.stack) \
                    and not (ab.loyalty is not None and self.loyalty_any_time(p, c)):
                return False
        if ab.loyalty is not None:
            if s.turn_flags.get(("loyalty", oid)):
                return False  # CR 606.3
        if ab.limit is not None and s.turn_flags.get(("limit", oid, id(ab)), 0) >= ab.limit:
            return False
        if getattr(ab, "once", False) and o.flags.get(("once", ab.text)):
            return False  # "Activate only once": exhaust 702.177a, power-up 702.193a (this object, 400.7)
        if ab.condition is not None and not ab.condition(self, oid, p):
            return False
        if self.rule_applies(oid, "cant_activate", mana=bool(getattr(ab, "is_mana", False))):
            return False
        ctx = {"source": oid, "controller": p, "x": 0, "chars": c}
        if not self.can_pay_cost(p, oid, ab.cost, ctx):
            return False
        if ab.modes is not None:
            if not any(all(self.legal_targets(t, p, ctx, []) or t.n[0] == 0 for t in m.targets) for m in ab.modes.modes):
                return False
        else:
            for t in ab.targets:
                if t.n[0] > 0 and len(self.legal_targets(t, p, ctx, [])) < t.n[0]:
                    return False
        if self.free_alt_cost(p, oid, ab):
            return True  # "you may pay {0} rather than pay the equip cost" (ref/cards/round2_c2.py)
        excl = self._own_cost_exclusion(oid, ab)
        if ab.cost.sacrifice is not None:
            # Sources that sacrifice themselves for mana and that the sacrifice cost needs can't
            # pay mana too (601.2h, 118.3; "can't sacrifice a ... to pay multiple costs"
            # rulings): Legion Extruder with a Treasure as its only other artifact.
            from .effects import SacCost
            excl = tuple(excl) + self._sac_reserved(p, {"parts": [SacCost(*ab.cost.sacrifice)]}, oid)
        spend = {"kind": "ability", "obj": oid, "ability": ab}
        life = ab.cost.life if isinstance(ab.cost.life, int) else 0
        if ab.modes is None and len(ab.targets) == 1 and ab.targets[0].n == (1, 1) and self._target_cost_mods(oid, ab):
            # The cost depends on the target: some legal target must make it payable.
            return any(self.mana_payable(p, self.ability_cost_mana(p, oid, ab, [[r]]), 0, spend, life=life, exclude=excl)
                       for r in self.legal_targets(ab.targets[0], p, ctx, []))
        cost = self.ability_cost_mana(p, oid, ab)
        return self.mana_payable(p, cost, 0, spend, life=life, exclude=excl)

    def activate(self, p, oid, ab):
        """CR 602.2."""
        s = self.s
        o = s.objs[oid]
        it = StackItem("activated", p, s.new_id())
        it.source = oid
        it.source_lki = self.chars(oid)
        it.ability = ab
        it.key = f"ability:{o.key}"
        ctx = {"source": oid, "controller": p, "x": 0, "chars": it.source_lki, "item": it.id,
               "source_chars": it.source_lki}
        it.ctx = ctx
        s.stack.append(it)
        if ab.modes is not None:
            modes = self.choose_modes(p, ab.modes, it, "ability")
            ctx["modes"] = modes
        # An alternative cost for the ability, announced before targets (CR 602.2b, 601.2b):
        # Kíli the Resourceful's {0} for the first equip ability each turn (round2_c2.py).
        free = self.choose_free_alt_cost(p, oid, ab)
        cost = [] if free else self.ability_cost_mana(p, oid, ab)
        excl = self._own_cost_exclusion(oid, ab)
        x = 0
        if "X" in cost:
            x = self.choose_x(p, cost, {"kind": "ability", "obj": oid, "ability": ab}, oid, {})
        elif getattr(ab.cost, "loyalty_x", False):
            # A [-X] loyalty cost (ref/cards/round2_a2.py): X is announced as the ability is
            # activated (CR 107.3a, 601.2b via 602.2b), at most the loyalty counters on the
            # permanent (606.6); pay_cost removes X of them.
            n = o.counters.get("loyalty", 0)
            x = self.ask(Decision(p, "ChooseX", [Opt(f"x:{i}", i) for i in range(n + 1)], subject=self.ref(oid),
                                  prompt="Choose X."))
        ctx["x"] = x
        specs = self.target_specs(ab, ctx)
        tdep = specs and self._target_cost_mods(oid, ab) and not free
        spend = {"kind": "ability", "obj": oid, "ability": ab}
        if tdep and len(specs) == 1 and specs[0].n == (1, 1):
            # Only targets with which the total cost can be paid (601.2c, 601.2f-h).
            self._target_filter = lambda r: self.mana_payable(p, self.ability_cost_mana(p, oid, ab, [[r]]), x, spend, exclude=excl)
        elif tdep:
            raise Unsupported("target-dependent ability cost with several targets")
        try:
            if specs and not self.choose_targets(p, specs, it):
                raise IllegalAction("no legal targets")
        finally:
            self._target_filter = None
        if tdep:
            cost = self.ability_cost_mana(p, oid, ab, it.targets)
        if ab.loyalty is not None:
            s.turn_flags[("loyalty", oid)] = True
        if ab.limit is not None:
            k = ("limit", oid, id(ab))
            s.turn_flags[k] = s.turn_flags.get(k, 0) + 1
        if getattr(ab, "once", False):
            o.flags[("once", ab.text)] = True  # CR 702.177a, 702.193a
        # Mana first while the source is still untapped (sources excluded if the cost taps it).
        from .effects import SacCost
        self._pending_extra = [SacCost(*ab.cost.sacrifice)] if ab.cost.sacrifice is not None else []
        # A source whose cost includes {T} can't be tapped as a mana ability's helper either
        # (Ba Sing Se, an earthbent land creature, for Springleaf Drum's cost while paying its own
        # "{2}{G}, {T}: Earthbend 2"): its {T} couldn't be paid afterwards (601.2g-h, 118.3).
        saved_keep = self._keep_untapped
        self._keep_untapped = set(saved_keep) | ({oid} if ab.cost.tap else set())
        try:
            self.pay_mana_excluding(p, cost, x, {"kind": "ability", "obj": oid, "ability": ab}, oid, excl)
        finally:
            self._pending_extra = []
            self._keep_untapped = saved_keep
        self.pay_cost(p, oid, ab.cost, ctx, it)
        s.touch()
        self.emit({"type": "activated", "player": p, "source": oid, "item": it.id, "ability": ab})
        if it.targets:
            self.became_targets(it)

    def pay_mana_excluding(self, p, cost, x, spend, subject, excl):
        if not cost:
            return {}
        if not excl:
            return self.pay_mana(p, cost, x, spend, subject)
        # Temporarily hide excluded sources from mana_sources.
        saved = self._excluded
        self._excluded = set(excl) | saved
        try:
            return self.pay_mana(p, cost, x, spend, subject)
        finally:
            self._excluded = saved

    # ================================================================== resolution (608)
    def resolve_top(self):
        s = self.s
        it = s.stack[-1]
        self.emit({"type": "resolving", "item": it.id})
        # Bookkeeping for the views (harness/viewcheck.py): a decision raised during the resolution
        # pauses here with it set (Game.view); cleared once the item has resolved.
        s.resolving = it.id
        if it.kind == "spell":
            self.resolve_spell(it)
        else:
            self.resolve_ability(it)
        s.resolving = None
        s.touch()

    def _targets_check(self, it, specs):
        """Returns (any_targets, all_illegal, legal_groups)."""
        if not it.targets:
            return False, False, []
        legal = []
        any_t = False
        all_bad = True
        for spec, grp in zip(specs, it.targets):
            ok = []
            for r in grp:
                any_t = True
                if self.target_still_legal(it, spec, r):
                    ok.append(r)
                    all_bad = False
            legal.append(ok)
        return any_t, (any_t and all_bad), legal

    def resolve_spell(self, it):
        s = self.s
        oid = it.obj
        o = s.objs[oid]
        c = self.chars(oid)
        f = self.spell_face(oid, o.face)
        sa = self.spell_ability(f)
        permanent = bool({"Creature", "Artifact", "Enchantment", "Planeswalker", "Land", "Battle"} & c.types)
        if "Aura" in c.subtypes and permanent:
            spec = self.enchant_spec(f)
            any_t, bad, legal = self._targets_check(it, [spec])
            if bad or not legal or not legal[0]:
                self.move(oid, self._leave_stack_zone(it), cause="fizzle")  # 608.3b
                return
            self.move(oid, "battlefield", controller=it.controller, attach_to=legal[0][0], cause="resolve")
            return
        if permanent:
            nid = self.move(oid, "battlefield", controller=it.controller, cause="resolve")  # 608.3
            self.permanent_resolved(nid, it)
            return
        specs = self.target_specs(sa, it.ctx)
        any_t, bad, legal = self._targets_check(it, specs)
        if bad:
            self.move(oid, self._leave_stack_zone(it), cause="fizzle")  # 608.2b
            return
        ctx = it.ctx
        ctx["legal"] = legal
        ctx["resolving_item"] = it.id
        if sa is not None:
            if sa.modes is not None:
                gi = 0
                for mi in ctx.get("modes", []):
                    m = sa.modes.modes[mi]
                    k = len(m.targets)
                    ctx["mode_targets"] = legal[gi:gi + k] if legal else [[] for _ in range(k)]
                    ctx["mode_target_specs"] = m.targets
                    gi += k
                    if k and legal and all(not g for g in ctx["mode_targets"]) and any(it.targets[gi - k:gi]):
                        continue  # this mode's targets are all illegal (608.2b applies per mode)
                    m.effect.run(self, ctx)
            else:
                sa.effect.run(self, ctx)
        # 608.2n: put into the graveyard (or wherever an effect says).
        if oid in s.objs and s.objs[oid].zone == "stack":
            dest = ctx.get("spell_dest") or ("exile" if (ctx.get("how") or "").split(":")[0] in ("flashback", "harmonize") else "graveyard")
            card = DB.get(o.card)
            cast_face = card.faces[o.face] if o.face < len(card.faces) else card.faces[0]
            omen = o.face == 1 and "Omen" in cast_face.subtypes
            adventure = card.layout == "adventure" and o.face == 1 and not omen
            if omen and dest == "graveyard":
                # CR 720.3d: shuffled into its owner's library instead of the graveyard.
                nid = self.move(oid, "library", cause="resolved")
                self.shuffle(o.owner)
                return
            if adventure and dest == "graveyard":
                dest = "exile"  # CR 715.3d: its controller exiles it and may play the card later
            nid = self.move(oid, dest, cause="resolved")
            if adventure and nid is not None and dest == "exile":
                s.objs[nid].flags["adventure_exile"] = {"player": it.controller}  # 715.3d: "that player"

    def resolve_ability(self, it):
        s = self.s
        ab = it.ability
        ctx = it.ctx
        specs = self.target_specs(ab, ctx)
        any_t, bad, legal = self._targets_check(it, specs)
        if it in s.stack and bad:
            s.stack.remove(it)  # 608.2b
            return
        if it.kind == "triggered" and ab.condition is not None and not ab.condition(self, ctx):
            s.stack.remove(it)  # 608.2a
            return
        ctx["legal"] = legal
        paused = False
        try:
            if ab.modes is not None and ctx.get("modes") is not None:
                gi = 0
                for mi in ctx["modes"]:
                    m = ab.modes.modes[mi]
                    k = len(m.targets)
                    ctx["mode_targets"] = legal[gi:gi + k] if legal else [[] for _ in range(k)]
                    gi += k
                    m.effect.run(self, ctx)
            else:
                ab.effect.run(self, ctx)
        except NeedDecision:
            # A choice during the resolution: the ability is still on the stack until the final
            # part of its resolution (CR 608.2n). The scratch state paused here is what the players
            # see (Game.view); the next submit re-runs the unit from the committed state.
            paused = True
            raise
        finally:
            if it in s.stack and not paused:
                s.stack.remove(it)

    def counter(self, target, to="graveyard"):
        """CR 701.6: counter a spell (to graveyard) or ability (removed)."""
        s = self.s
        if isinstance(target, tuple) and target[0] == "stack":
            it = next((x for x in s.stack if x.id == target[1]), None)
            if it is not None:
                s.stack.remove(it)
            return True
        o = s.objs.get(target)
        if o is None or o.zone != "stack":
            return False
        it = next((x for x in s.stack if x.kind == "spell" and x.obj == target), None)
        if it is not None and (it.uncounterable or self.rule_applies(target, "cant_be_countered")):
            return False
        if it is not None and to == "graveyard":
            to = self._leave_stack_zone(it)
        self.move(target, to, cause="countered")
        return True

    def _leave_stack_zone(self, it):
        """CR 702.34a (flashback) / harmonize: a spell cast that way is exiled instead of going
        anywhere else when it leaves the stack."""
        how = ((it.ctx or {}).get("how") or "").split(":")[0]  # `flashback:2` (alt_methods)
        return "exile" if how in ("flashback", "harmonize") else "graveyard"
