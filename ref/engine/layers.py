"""Characteristics and the layer system (CR 613).

`chars(oid)` returns an object's characteristics after all continuous effects. Battlefield
permanents and spells are computed together, layer by layer (CR 613.1), with characteristic-
defining abilities first (613.3), dependency order (613.8) and otherwise timestamp order
(613.7). The set of objects a static ability's effect applies to is fixed in the first layer it
applies in (613.6); an effect from a resolved spell or ability affects a set fixed when it began
(611.2c).

Fidelity limits (documented, fail-safe): dependency is detected through changes of an effect's
existence or of the set of objects it applies to (613.8a), not through changes of "what it
does".
"""
from .objects import Chars
from ..cards.model import DB, BASIC_LAND_TYPES
from .. import Unsupported


def printed_pt(v):
    """Printed power/toughness as a number: '*' counts as 0 and the rest of the expression stays
    ('1+*' -> 1, '*+1' -> 1, '*' -> 0). The characteristic-defining ability, while the object has
    it, overrides this in layer 7a (CR 208.2a, 613.4a); without it these are the values
    (Merfolk Trickster ruling 2018-04-27: "*/*+1 ... it's 0/1")."""
    if v is None or isinstance(v, int):
        return v
    s = str(v).replace(" ", "").replace("*", "0")
    total, sign, num = 0, 1, ""
    for ch in s + "+":
        if ch in "0123456789":
            num += ch
        elif ch in "+-":
            total += sign * int(num or 0)
            sign, num = (1 if ch == "+" else -1), ""
        else:
            return 0  # an unusual printed value (e.g. '*²'): no number to start from
    return total

LAYERS = [1, 2, 3, 4, 5, 6, "7a", "7b", "7c", "7d"]


_KW_COUNTERS = {"flying", "first strike", "double strike", "deathtouch", "haste", "hexproof",
                "indestructible", "lifelink", "menace", "reach", "trample", "vigilance"}



# hook(g, obj, chars): abilities granted to objects off the battlefield (layers._chars_off_battlefield).
OFF_BATTLEFIELD_HOOKS = []

class _KwCounter:
    """Layer-6 effect of a keyword counter (called like a continuous effect's layer function)."""
    __slots__ = ("kw",)

    def __init__(self, kw):
        self.kw = kw

    def __call__(self, g, oid, c, ce):
        from .ir import Keyword
        c.abilities.append(Keyword(self.kw, text=f"{self.kw} counter"))


class _KwCounterTarget:
    """Stands in for a continuous effect with a fixed object list (_affected, _fixed_sig)."""
    __slots__ = ("affected",)

    def __init__(self, oid):
        self.affected = [oid]


class _Eff:
    __slots__ = ("key", "src", "ability", "part", "ts", "cda", "ce", "parts")

    def __init__(self, key, src, ability, part, ts, cda=False, ce=None):
        self.key = key
        self.src = src
        self.ability = ability
        self.part = part
        self.ts = ts
        self.cda = cda
        self.ce = ce


def _room_name(card, halves):
    """A Room's name with those halves unlocked (CR 709.5: none, one door's, or both: the card's)."""
    halves = tuple(halves)
    return card.name if len(halves) == 2 else (card.faces[halves[0]].name if halves else None)


class LayersMixin:
    _partial = None

    # ------------------------------------------------------------------ base characteristics
    def face_of(self, o):
        card = DB.get(o.card)
        idx = o.face if o.face < len(card.faces) else 0
        return card, card.faces[idx]

    def base_chars(self, o):
        """Copiable values (CR 707.2) before layer 1 effects other than the object's own."""
        c = Chars()
        c.controller = o.controller
        if o.face_down and o.zone == "exile":
            return c  # CR 406.3a: a card exiled face down has no characteristics
        if o.face_down:
            # CR 708.2a: a face-down permanent/spell is a 2/2 creature with no text, no name,
            # no subtypes and no mana cost (manifest dread, cloak, disguise give extras).
            c.name = None
            c.types = {"Creature"}
            c.power, c.toughness = 2, 2
            kind = o.face_down_kind
            if kind == "cloak" or kind == "disguise":
                from ..cards.compiler import ward_ability
                c.abilities = [ward_ability([2], 0)]  # CR 701.58a / 702.168a
            return c
        copiable = o.flags.get("copiable")
        if copiable is not None:
            if "Room" in copiable.subtypes and copiable.room is None:
                # A copy of a Room spell on the stack (only the cast half's characteristics are
                # recorded there, 709.3b; 709.5b makes both halves copiable).
                raise Unsupported("a copy of a Room spell (CR 709.5b)")
            c = copiable.copy()
            c.controller = o.controller
            if c.room is not None:
                # A copy of a Room permanent (CR 709.5, 709.5b): the two
                # halves and the shared type line's two static abilities are copiable values, the
                # unlocked designations are not (709.5c, 707.2): the copy has the name, mana cost
                # and rules text of ITS OWN unlocked halves (none as a token copy enters, 709.5d)
                # and may be unlocked (709.5e). Exceptions of the copy effect (Adagia's "except
                # it's legendary") stay: only the halves' contributions are swapped.
                rname, had = c.room
                want = tuple(i for i in (0, 1) if i in (o.flags.get("unlocked") or ())) \
                    if o.zone == "battlefield" else (0, 1)  # off the battlefield: both (709.4)
                if want != tuple(had):
                    rc = DB.get(rname)
                    old = [a for i in had for a in rc.faces[i].abilities()]
                    old_name = _room_name(rc, had)
                    old_cost = [sym for i in had for sym in rc.faces[i].mana_cost]
                    from .mana import cost_colors
                    if c.name == old_name:
                        c.name = _room_name(rc, want)
                    if c.mana_cost == old_cost:
                        c.mana_cost = [sym for i in want for sym in rc.faces[i].mana_cost]
                        if c.colors == set(cost_colors(old_cost)):
                            c.colors = set(cost_colors(c.mana_cost))
                    c.abilities = [a for a in c.abilities if not any(a is b for b in old)] + \
                        [a for i in want for a in rc.faces[i].abilities()]
                    c.room = (rname, want)
            if c.mv_front:
                # CR 202.3b, 712.8e: a copy of the back face of a nonmodal double-faced object has
                # the back face's mana cost (none): mana value 0. The front face's cost was only the
                # transformed original's mana value, not a copiable value (707.2).
                c.mana_cost = []
                c.mv_front = False
            return c
        card, face = self.face_of(o)
        if card.layout == "split" and o.zone == "battlefield" and "Room" in card.faces[0].subtypes:
            # CR 709.5, 709.5a, 709.5c: a Room permanent has its shared type line, and the name,
            # mana cost (so its colors, 202.2) and rules text of its unlocked halves only
            # (flags['unlocked']: ref/cards/round2_a2.py, Rooms).
            from .mana import cost_colors
            un = [i for i in (0, 1) if i in (o.flags.get("unlocked") or ())]
            f0 = card.faces[0]
            c.name = card.name if len(un) == 2 else (card.faces[un[0]].name if un else None)
            c.mana_cost = [sym for i in un for sym in card.faces[i].mana_cost]
            c.colors = set(cost_colors(c.mana_cost))
            c.types = set(f0.types)
            c.subtypes = set(f0.subtypes)
            c.supertypes = set(f0.supertypes)
            c.abilities = [a for i in un for a in card.faces[i].abilities()]
            c.face = 0
            c.room = (card.name, tuple(un))  # Copiable halves (709.5b)
            return c
        if card.layout == "split" and o.zone != "stack" and not o.flags.get("split_half"):
            # CR 709.4, 709.4a-c: off the stack, both halves combined: both names, the combined
            # mana cost (its colors and mana value come from it), each type and ability of each
            # half. ("split_half": casting.face_chars evaluating one half, 709.3a.)
            from .mana import cost_colors
            f0, f1 = card.faces[0], card.faces[1]
            c.name = card.name
            c.mana_cost = list(f0.mana_cost) + list(f1.mana_cost)
            c.colors = set(cost_colors(c.mana_cost))
            c.types = set(f0.types) | set(f1.types)
            c.subtypes = set(f0.subtypes) | set(f1.subtypes)
            c.supertypes = set(f0.supertypes) | set(f1.supertypes)
            c.abilities = list(f0.abilities()) + list(f1.abilities())
            c.face = 0
            if "Room" in f0.subtypes:
                c.room = (card.name, (0, 1))  # Copiable halves (709.5b)
            return c
        c.name = face.name
        c.mana_cost = list(face.mana_cost)
        if card.layout in ("transform",) and o.face == 1:
            # CR 712.8e: a transformed permanent's mana value is the front face's.
            c.mana_cost = list(card.faces[0].mana_cost)
            c.mv_front = True
        c.colors = set(face.colors)
        c.types = set(face.types)
        c.subtypes = set(face.subtypes)
        c.supertypes = set(face.supertypes)
        c.power = printed_pt(face.power)
        c.toughness = printed_pt(face.toughness)
        c.loyalty = face.loyalty if isinstance(face.loyalty, int) else None
        c.defense = face.defense if isinstance(face.defense, int) else None
        c.abilities = list(face.abilities())
        c.face = o.face
        if card.layout == "prepare" and not o.face:
            c.prepare = o.card  # CR 722.2b: the prepare spell is part of the copiable values
        # CR 305.6: basic land types grant their mana ability (intrinsic).
        if "Land" in c.types:
            from .effects import intrinsic_mana_ability
            for st in sorted(c.subtypes):
                if st in BASIC_LAND_TYPES:
                    c.abilities.append(intrinsic_mana_ability(BASIC_LAND_TYPES[st]))
        return c

    # ------------------------------------------------------------------ access
    def chars(self, oid):
        s = self.s
        if self._partial is not None:
            c = self._partial.get(oid)
            if c is not None:
                return c
        if s.chars_cache is None:
            self._compute_all()
        c = s.chars_cache.get(oid)
        if c is None:
            o = s.objs.get(oid)
            if o is None:
                if oid in s.lki:
                    return s.lki[oid]
                raise KeyError(f"no object {oid}")
            c = self._chars_off_battlefield(o)
            s.chars_cache[oid] = c
        return c

    def _chars_off_battlefield(self, o):
        c = self.base_chars(o)
        # CDAs function in all zones (CR 604.3).
        for ab in list(c.abilities):
            if ab.kind == "static" and "all" in ab.zones:
                for part in ab.parts:
                    if part.kind == "layer" and part.affected == "self":
                        part.fn(self, o.id, c, o.id)
        # Abilities an effect grants a card in a hidden or graveyard zone (613.1f: a card in the
        # graveyard that "gains flashback until end of turn"; ref/cards/round2_b2.py).
        for hook in OFF_BATTLEFIELD_HOOKS:
            hook(self, o, c)
        return c

    # ------------------------------------------------------------------ full computation
    def _compute_all(self):
        s = self.s
        ids = list(s.battlefield) + [it.obj for it in s.stack if it.kind == "spell" and it.obj in s.objs]
        chars = {}
        for oid in ids:
            chars[oid] = self.base_chars(s.objs[oid])
        prev = self._partial
        self._partial = chars
        try:
            started = {}
            for layer in LAYERS:
                effs = self._layer_effects(layer, chars, started)
                if len(effs) > 1:
                    effs = self._order(effs, layer, chars, started)
                for e in effs:
                    self._apply(e, layer, chars, started)
                if layer == "7c":
                    self._pt_counters(chars)
            for oid, c in chars.items():
                o = s.objs[oid]
                if "Planeswalker" in c.types and o.zone == "battlefield":
                    c.loyalty = o.counters.get("loyalty", 0)  # CR 306.5c
                if "Battle" in c.types and o.zone == "battlefield":
                    c.defense = o.counters.get("defense", 0)  # CR 310.4c
                if o.zone == "battlefield" and "Creature" not in c.types:
                    # CR 208.3: a noncreature permanent has no power or toughness, even with a
                    # printed P/T (an uncrewed Vehicle, an Enduring enchantment); 208.3a.
                    c.power = c.toughness = None
        finally:
            self._partial = prev
        s.chars_cache = chars

    def _static_ts(self, oid):
        return self.s.objs[oid].timestamp

    def _layer_effects(self, layer, chars, started):
        s = self.s
        out = []
        seen = set()
        for oid, c in chars.items():
            o = s.objs[oid]
            for ab in c.abilities:
                if ab.kind != "static":
                    continue
                # A spell on the stack: only its characteristic-defining abilities, which
                # function in all zones (CR 604.3, 702.73a: a changeling spell is every creature
                # type, so Giada's "Spend this mana only to cast an Angel spell" mana can pay
                # for Nameless Inversion).
                if o.zone != "battlefield" and "all" not in ab.zones:
                    continue
                parts = [p for p in ab.parts if p.kind == "layer" and p.layer == layer
                         and (o.zone == "battlefield" or p.affected == "self")]
                if not parts:
                    continue
                key = (oid, id(ab))
                if key not in started and ab.condition is not None and not ab.condition(self, oid):
                    continue
                cda = getattr(ab, "cda", False)
                for p in parts:
                    out.append(_Eff(key, oid, ab, p, self._static_ts(oid), cda=cda))
                seen.add(key)
        # CR 613.6: an effect that began applying keeps applying even if its ability is removed.
        for key, rec in started.items():
            if key in seen or rec[1] is None:
                continue
            src, ab = rec[1], rec[2]
            for p in ab.parts:
                if p.kind == "layer" and p.layer == layer:
                    out.append(_Eff(key, src, ab, p, self._static_ts(src) if src in s.objs else 0))
        for ce in s.effects:
            fn = ce.parts.get(layer)
            if fn is None:
                continue
            if ce.kind == "layer_static" and ce.data.get("condition") and not ce.data["condition"](self, ce):
                continue
            out.append(_Eff(("ce", ce.id), ce.source, None, fn, ce.timestamp, ce=ce))
        if layer == 6:
            out += self._keyword_counter_effects(chars)
        out.sort(key=lambda e: (not e.cda, e.ts, str(e.key)))
        return out

    def _keyword_counter_effects(self, chars):
        """CR 122.1b, 613.1f: a keyword counter makes its object have that keyword, applied in
        layer 6 in timestamp order (613.7c: a counter's timestamp is when it was put on; one an
        object entered with has the object's), so an earlier "loses all abilities" doesn't stop
        it and a later one removes it."""
        out = []
        for oid in chars:
            o = self.s.objs[oid]
            if o.zone != "battlefield":
                continue
            for k, n in o.counters.items():
                if k in _KW_COUNTERS and n > 0:
                    out.append(_Eff(("kwc", oid, k), oid, None, _KwCounter(k), o.counter_ts.get(k, o.timestamp),
                                    ce=_KwCounterTarget(oid)))
        return out

    def _affected(self, e, chars):
        s = self.s
        if e.ce is not None:
            if e.ce.affected is not None:
                return [a for a in e.ce.affected if a in chars]
            f = e.ce.filter
            return [oid for oid in chars if s.objs[oid].zone == "battlefield" and f(self, oid, chars[oid])]
        aff = e.part.affected
        if aff == "self":
            return [e.src] if e.src in chars else []
        if aff == "attached":
            o = s.objs.get(e.src)
            t = o.attached_to if o else None
            return [t] if isinstance(t, int) and t in chars else []
        if callable(aff):
            return [oid for oid in chars if s.objs[oid].zone == "battlefield" and aff(self, oid, chars[oid], e.src)]
        if hasattr(aff, "match"):
            return [oid for oid in chars if s.objs[oid].zone == "battlefield" and aff.match(self, oid, e.src)]
        raise Unsupported(f"affected spec {aff!r}")

    def _apply(self, e, layer, chars, started):
        if e.key in started and started[e.key][0] is not None:
            aff = [a for a in started[e.key][0] if a in chars]
        else:
            if e.ce is None:
                # CR 613.6/613.8a: an effect that hasn't begun applying doesn't exist any more if
                # an earlier effect in this layer removed its ability from its source.
                src = chars.get(e.src)
                if src is None or not any(a is e.ability for a in src.abilities):
                    return
            aff = self._affected(e, chars)
            started[e.key] = (aff, e.src if e.ce is None else None, e.ability)
        for oid in aff:
            if e.ce is not None:
                e.part(self, oid, chars[oid], e.ce)
            else:
                e.part.fn(self, oid, chars[oid], e.src)

    def _order(self, effs, layer, chars, started):
        """CDAs first (613.3), then dependency order (613.8), then timestamps (613.7)."""
        cdas = [e for e in effs if e.cda]
        rest = [e for e in effs if not e.cda]
        if len(rest) <= 1:
            return cdas + rest
        n = len(rest)
        deps = {i: set() for i in range(n)}
        # Only an effect whose (exists, affected set) can change can depend on another (613.8a as
        # _sig models it). Effects with a fixed object list (e.g. "it gets +2/+0 until end of turn"
        # for each of hundreds of tokens) can't: with none of the others variable, the order is
        # the timestamp order `rest` already has, and no trial is needed.
        var = [i for i in range(n) if not self._fixed_sig(rest[i], started)]
        if not var:
            return cdas + rest
        base_sig = {i: self._sig(rest[i], chars, started) for i in var}
        for j in range(n):
            # Apply effect j alone to a copy and see which others change existence/affected set.
            # Copy-on-write: only the characteristics an applied effect modifies are copied.
            trial = dict(chars)
            prev = self._partial
            self._partial = trial
            try:
                tmp_started = dict(started)
                copied = set()
                for e in cdas + [rest[j]]:
                    for oid in self._apply_targets(e, trial, tmp_started):
                        if oid not in copied:
                            trial[oid] = trial[oid].copy()
                            copied.add(oid)
                    self._apply(e, layer, trial, tmp_started)
                for i in var:
                    if i == j:
                        continue
                    if self._sig(rest[i], trial, started) != base_sig[i]:
                        deps[i].add(j)
            finally:
                self._partial = prev
        order, done = [], set()
        remaining = list(range(n))
        while remaining:
            ready = [i for i in remaining if deps[i] <= done]
            if not ready:
                ready = remaining  # dependency loop: timestamp order (613.8b)
            i = min(ready, key=lambda k: (rest[k].ts, str(rest[k].key)))
            order.append(rest[i])
            done.add(i)
            remaining.remove(i)
        return cdas + order

    def _fixed_sig(self, e, started):
        """True when _sig(e) is the same on every trial: an explicit object list, or a static
        ability that already began applying to itself or the object it's attached to."""
        if e.ce is not None:
            return e.ce.affected is not None
        if e.key in started and started[e.key][0] is not None:
            return e.part.affected in ("self", "attached")
        return False

    def _apply_targets(self, e, chars, started):
        """The objects _apply(e, ..., chars, started) would modify (same rules, no side effects)."""
        if e.key in started and started[e.key][0] is not None:
            return [a for a in started[e.key][0] if a in chars]
        if e.ce is None:
            src = chars.get(e.src)
            if src is None or not any(a is e.ability for a in src.abilities):
                return []
        return self._affected(e, chars)

    def _sig(self, e, chars, started):
        """(exists, affected set) of an effect evaluated on `chars` (CR 613.8a)."""
        if e.ce is None:
            src = chars.get(e.src)
            exists = src is not None and any(a is e.ability for a in src.abilities)
            if e.key in started and started[e.key][0] is not None:
                exists = True
        else:
            exists = True
        prev = self._partial
        self._partial = chars
        try:
            aff = tuple(sorted(self._affected(e, chars))) if exists else ()
        finally:
            self._partial = prev
        return (exists, aff)

    def _pt_counters(self, chars):
        for oid, c in chars.items():
            o = self.s.objs[oid]
            # CR 122.1j: a hone counter on an Equipment gives +1/+0 to any creature that
            # Equipment is attached to; a counter that modifies power, applied in layer 7c
            # (613.4c) with the creature's own counters.
            n = o.counters.get("hone", 0) if o.counters else 0
            if n > 0 and "Equipment" in c.subtypes and o.attached_to in chars \
                    and chars[o.attached_to].power is not None:
                chars[o.attached_to].power += n
            if c.power is None or not o.counters:
                continue
            for k, n in o.counters.items():
                if n <= 0:
                    continue
                if k == "+1/+1":
                    c.power += n
                    c.toughness += n
                elif k == "-1/-1":
                    c.power -= n
                    c.toughness -= n
                elif k.startswith(("+", "-")) and "/" in k:
                    a, b = k.split("/")
                    c.power += int(a) * n
                    c.toughness += int(b) * n
