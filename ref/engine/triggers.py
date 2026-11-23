"""Triggered abilities (CR 603) and replacement/prevention effects (CR 614-616)."""
from .decisions import Decision, Opt
from .objects import StackItem
from ..cards.model import DB
from .. import Unsupported


class PendingTrigger:
    __slots__ = ("ability", "source", "source_chars", "controller", "event", "data", "index",
                 "delayed")

    def __init__(self, ability, source, source_chars, controller, event, data, index, delayed=None):
        self.ability = ability
        self.source = source
        self.source_chars = source_chars
        self.controller = controller
        self.event = event
        self.data = data
        self.index = index      # 1-based index among the source's triggered abilities
        self.delayed = delayed


_NONBF_TRIGGERS = {}


def _nonbf_trigger_zones(name):
    """Zones other than the battlefield where a card's triggered abilities function."""
    if name not in _NONBF_TRIGGERS:
        zs = set()
        try:
            card = DB.get(name)
            for f in card.faces:
                for ab in f.abilities():
                    if ab.kind == "triggered":
                        zs.update(z for z in ab.zones if z != "battlefield")
        except Unsupported:
            pass
        _NONBF_TRIGGERS[name] = zs
    return _NONBF_TRIGGERS[name]


class TriggersMixin:
    # ------------------------------------------------------------------ triggering
    def check_triggers(self, ev):
        s = self.s
        typ = ev["type"]
        if typ == "zone" and ev.get("to") == "battlefield" and self._entering_doesnt_trigger(ev):
            return
        lookback = ev.get("bf_lki")
        # Only the permanents with a triggered ability for this event
        # type are matched (_trig_index; a pure performance measure, see there).
        ti = self._trig_index()
        tset = ti[2].get(typ, ()) if ti is not None else None
        # Battlefield (current state) and, for look-back, the battlefield before the event.
        for oid in list(s.battlefield):
            if tset is not None and oid not in tset and s.chars_cache is ti[0]:
                continue
            o = s.objs.get(oid)
            if o is None or o.phased_out:
                continue
            c = self.chars(oid)
            self._match_abilities(ev, typ, oid, c, c.controller, lookback_only=False)
        if lookback is not None:
            for oid, c in lookback.items():
                self._match_abilities(ev, typ, oid, c, c.controller, lookback_only=True)
        # Abilities that function in other zones (graveyard, hand, exile, stack).
        for p in s.players:
            for zname in ("graveyard", "hand", "exile"):
                for oid in list(p.zone(zname)):
                    o = s.objs.get(oid)
                    if o is None or zname not in _nonbf_trigger_zones(o.card):
                        continue
                    c = self.chars(oid)
                    for i, ab in enumerate(self._triggered(c)):
                        if zname in ab.zones and typ in ab.trigger.events:
                            data = ab.trigger.match(self, ev, oid, c, o.owner)
                            if data:
                                self._add_pending(ab, oid, c, o.owner, ev, data, i + 1)
        # Abilities of spells that function on the stack ("When you cast this spell", CR 113.6c).
        for it in list(s.stack):
            if it.kind != "spell":
                continue
            o = s.objs.get(it.obj)
            if o is None or "stack" not in _nonbf_trigger_zones(o.card):
                continue
            c = self.chars(it.obj)
            for i, ab in enumerate(self._triggered(c)):
                if "stack" in ab.zones and typ in ab.trigger.events:
                    data = ab.trigger.match(self, ev, it.obj, c, it.controller)
                    if not data:
                        continue
                    if ab.condition is not None and not ab.condition(
                            self, {"source": it.obj, "controller": it.controller, "event": ev, "data": data}):
                        continue  # CR 603.4
                    self._add_pending(ab, it.obj, c, it.controller, ev, data, i + 1)
        # Delayed triggered abilities (CR 603.7).
        keep = []
        for d in s.delayed:
            fired = False
            if d.get("this_turn") is not None and d["this_turn"] != s.turn:
                continue  # CR 603.7b: a delayed trigger with the duration "this turn" has ended
            if typ in d["trigger"].events:
                data = d["trigger"].match(self, ev, d.get("source"), None, d["controller"])
                if data:
                    ab = d["ability"]
                    s.pending.append(PendingTrigger(ab, d.get("source"), d.get("source_chars"),
                                                    d["controller"], ev, dict(data, **d.get("data", {})),
                                                    self.after_own_triggers(d.get("source"), d.get("source_chars")),
                                                    delayed=d))
                    fired = True
            if not (fired and d.get("once", True)):
                keep.append(d)
        s.delayed = keep

    def _entering_doesnt_trigger(self, ev):
        """Doorkeeper Thrull: "Artifacts and creatures entering don't cause abilities to
        trigger." The entering permanent is looked at as it exists on the battlefield, and the
        static applies to a Doorkeeper entering at the same time (rulings 2024-02-02; CR
        603.6a: the newcomers are checked too)."""
        parts = self.rule_parts("entering_doesnt_trigger")
        if not parts:
            return False
        new = ev.get("new")
        if new is None or new not in self.s.objs or self.s.objs[new].zone != "battlefield":
            return False
        c = self.chars(new)
        return any(bool(set(part.data["types"]) & c.types) for part, _ in parts)

    def after_own_triggers(self, src, chars=None):
        """Canonical number of a delayed (CR 603.7) or reflexive (603.12) triggered ability of
        source `src`: after the source's own triggered abilities (format §3.1 OrderTriggers), so
        `trigger:<ref>:<n>` can't collide with one of them (campaign11m 907699: an exiled Earthbender
        Ascension's earthbend return and the returned Ascension's enters trigger were both
        `trigger:key:Earthbender Ascension#4:1`). The source as it is on the battlefield, else as it
        last existed there (`chars`); no source (an emblem): 1."""
        s = self.s
        c = self.chars(src) if src is not None and src in s.battlefield else chars
        if c is None and src is not None and src in s.objs:
            c = self.chars(src)
        if c is None:
            return 1
        return sum(1 for a in getattr(c, "abilities", ()) if getattr(a, "kind", None) == "triggered") + 1

    def _triggered(self, c):
        return [a for a in c.abilities if a.kind == "triggered"]

    def _trig_index(self):
        """Event type -> the battlefield objects whose characteristics
        have a triggered ability for it that functions on the battlefield. A pure performance
        measure, like combat._rule_index: every event checked every permanent's abilities, so a
        Krenko, Mob Boss board (a thousand Goblin tokens, each entering one event, Enduring
        Courage's trigger per Goblin) was quadratic and games ran past a 900 s timeout
        (rt5-t 1100981, 1304624). Built from the layer-computed characteristics, it lives as long
        as that cache (s.chars_cache, rebuilt after every touch) and the battlefield list are the
        same; check_triggers scans every permanent again once the cache changes. None while the
        layers are being computed (partial characteristics)."""
        s = self.s
        if self._partial is not None or not s.battlefield:
            return None
        if s.chars_cache is None:
            self.chars(s.battlefield[0])
        ti = getattr(s, "trig_index", None)
        if ti is not None and ti[0] is s.chars_cache and ti[1] == s.battlefield:
            return ti
        idx = {}
        for oid in s.battlefield:
            c = self.chars(oid)
            for ab in c.abilities:
                if ab.kind != "triggered" or "battlefield" not in ab.zones:
                    continue
                for e in ab.trigger.events:
                    idx.setdefault(e, set()).add(oid)
        ti = (s.chars_cache, list(s.battlefield), idx)
        s.trig_index = ti
        return ti

    def _match_abilities(self, ev, typ, oid, c, ctrl, lookback_only):
        for i, ab in enumerate(self._triggered(c)):
            tr = ab.trigger
            if typ not in tr.events or "battlefield" not in ab.zones:
                continue
            lb = tr.lookback
            if getattr(tr, "lookback_for", None) is not None:
                lb = tr.lookback_for(ev)  # a trigger with a look-back part and a current part
            if lookback_only != bool(lb and ev.get("bf_lki") is not None):
                continue
            data = tr.match(self, ev, oid, c, ctrl)
            if not data:
                continue
            if ab.condition is not None:
                ctx = {"source": oid, "controller": ctrl, "event": ev, "data": data}
                if not ab.condition(self, ctx):
                    continue  # CR 603.4: intervening "if" false when the event occurs
            self._add_pending(ab, oid, c, ctrl, ev, data, i + 1)

    def _add_pending(self, ab, oid, c, ctrl, ev, data, index):
        s = self.s
        if ab.limit is not None:
            k = ("trigger_limit", oid, id(ab))
            n = s.turn_flags.get(k, 0)
            if n >= ab.limit:
                return
            s.turn_flags[k] = n + 1
        times = 1 + self._extra_triggers(oid, c, ctrl, ev)
        for _ in range(times):
            s.pending.append(PendingTrigger(ab, oid, c, ctrl, ev, data if isinstance(data, dict) else {}, index))

    def _extra_triggers(self, oid, c, ctrl, ev):
        """CR 603.2d: "If a triggered ability of another creature you control of the chosen type
        triggers, it triggers an additional time" (Roaming Throne). Counts such statics of other
        permanents on the battlefield controlled by the trigger's controller. For a look-back
        trigger (603.10) whose event also removed such a permanent, which statics apply would
        need the look-back as well: fail loudly there."""
        s = self.s
        if c is None or "Creature" not in c.types:
            return 0
        n = 0
        for x in s.battlefield:
            if x == oid:
                continue
            cx = self.chars(x)
            if cx.controller != ctrl:
                continue
            for a in cx.abilities:
                if getattr(a, "extra_trigger", None) == "chosen_type_creature":
                    t = s.objs[x].chosen.get("type")
                    if t is not None and t in c.subtypes:
                        n += 1
        lki = ev.get("bf_lki") if isinstance(ev, dict) else None
        if isinstance(lki, dict) and c.subtypes:
            for x, cx in lki.items():
                if x != oid and x not in s.battlefield and getattr(cx, "controller", None) == ctrl and any(
                        getattr(a, "extra_trigger", None) for a in getattr(cx, "abilities", ())):
                    raise Unsupported("an additional-trigger static left the battlefield in the same event (CR 603.10)")
        return n

    # ------------------------------------------------------------------ to the stack
    def put_triggers_on_stack(self):
        """CR 603.3b: APNAP order; each player orders their own triggers."""
        s = self.s
        batch, s.pending = s.pending, []
        # The triggers of this batch not yet on the stack
        # still distinguish the objects they act on (obj_sig's "psrc"/"ev", CR 603.3b: all of them
        # are put on the stack at once, targets chosen as each is put). A warped Starfield
        # Shepherd whose warp exile waits below The Mind Stone's "exile up to one other target
        # nonland permanent" isn't interchangeable with an identical hard-cast one (campaign21
        # 1310463: ours merged them and blinked the warped one, the EUT the other).
        left_all = list(batch)
        self._unstacked_triggers = left_all
        try:
            self._put_batch_on_stack(batch, left_all)
        finally:
            self._unstacked_triggers = None

    def _put_batch_on_stack(self, batch, left_all):
        s = self.s
        for p in (s.active, 1 - s.active):
            mine = [t for t in batch if t.controller == p]
            while mine:
                if len(mine) == 1:
                    t = mine[0]
                else:
                    opts = []
                    # The stack and battlefield don't change while the options are built: index
                    # what _stack_links and _trigger_source_look scan for every option once
                    # (a thousand Goblins' triggers: quadratic scans per option otherwise).
                    self._look_index = self._build_look_index()
                    try:
                        for t in mine:
                            src = self.ref(t.source) if t.source is not None else "delayed"
                            canon = f"trigger:{src}:{t.index}"
                            # Triggers of identical sources (or the same source) are interchangeable.
                            # (CR 603.3b: the order of interchangeable triggers is not a real choice.)
                            sig = ("trig", t.source_chars.name if t.source_chars is not None else src,
                                   t.source_chars.controller if t.source_chars is not None else None, t.index)
                            if t.source is not None and t.source in s.battlefield:
                                # Same-named sources that visibly differ (counters, P/T, attachments:
                                # two Sazh's Chocobos, one equipped) aren't identical: their triggers'
                                # order is the controller's choice (603.3b; campaign11m 906621).
                                sig = sig + (self._trigger_source_look(t.source),)
                            elif t.source is not None and t.source_chars is not None:
                                # A source that
                                # left the battlefield (a dies trigger) is told apart by its last
                                # known information (CR 603.10a, 608.2h): two Goldvein Hydras of
                                # different power dying together create different numbers of
                                # Treasures, so their order is the controller's choice (603.3b).
                                # rt4-f 1011004: ours merged them, the EUT asked.
                                lo = s.lki_obj.get(t.source)
                                sig = sig + (("lki", t.source_chars.power, t.source_chars.toughness,
                                              tuple(sorted(lo.counters.items())) if lo is not None else ()),)
                            # Two instances of one ability whose events differ (another object, another
                            # amount, another player) aren't interchangeable either: two Enduring
                            # Tenacity triggers for 2 and 3 life (campaign10 761322), Enduring
                            # Curiosity's draws for two attackers (603.3b; format §3.1 OrderTriggers).
                            sig = sig + self._trigger_event_sig(t)
                            if t.delayed is not None:
                                # A delayed trigger acts on the objects its effect was bound to
                                # (CR 603.7c: "Sacrifice them" of mobilize or Dalkovan Encampment,
                                # one per resolution): two whose objects differ (one set lost a
                                # Warrior) do different things, so their order is a choice (603.3b);
                                # identical sets stay one option (format §3.1 Trigger identity).
                                sig = sig + (self._delayed_referents_look(t),)
                            if t.source is not None:
                                # The cards exiled with the
                                # source (CR 607.2a; format §3: "linked/exiled-with cards" make
                                # objects not identical), on the battlefield or not (a leaves-the-
                                # battlefield trigger, CR 603.10a). Two Unidentified Hoverships
                                # destroyed together, one with an exiled card: "the exiled card's
                                # owner manifests dread" does something for one only, so their order
                                # is a choice (603.3b). rt3-f 1211629: the EUT asked, ours merged.
                                lk = self._linked_cards_look(t.source)
                                if lk:
                                    sig = sig + (("linked",) + lk,)
                            opts.append(Opt(canon, t, sig=sig))
                    finally:
                        self._look_index = None
                    d = Decision(p, "OrderTriggers", opts, prompt="Choose the next triggered ability to put on the stack (the last one resolves first).")
                    self._name_trigger_instances(d, mine)
                    if len(d.options) == 1:
                        t = d.options[0].data
                    else:
                        t = self.ask(d)
                mine.remove(t)
                left_all.remove(t)
                self.put_trigger(t)

    # Events whose object is the source of the damage or life gain ("deals damage", lifelink and
    # other gains), not an "obj" (format §3.1 OrderTriggers, occurrence suffix).
    _EVENT_SOURCE_OBJ = ("damage", "source_dealt_damage", "lifegain")
    # Events that name no object for the suffix: a player's life loss, a draw (the drawn card is
    # hidden), steps and searches.
    _EVENT_NO_OBJ = ("lifeloss", "draw", "step", "combat_damage_step", "attackers_declared",
                     "blockers_declared", "scry", "surveil", "search", "shuffle", "reveal_hand", "state")

    def trigger_event_obj(self, t):
        """The object a pending trigger's event names (format §3.1 OrderTriggers `@<event ref>`):
        the damage source for "deals damage" events, the object that moved for zone changes
        (entering, leaving), the spell for cast triggers, the source of a life gain, else the
        object the event is about (an attacking or blocking creature, a permanent that got
        counters or became tapped, a discarded card). None when it names none."""
        return self.event_obj(t.event)

    def event_obj(self, ev):
        """trigger_event_obj for a trigger event (also of an ability already on the stack)."""
        if not isinstance(ev, dict):
            return None
        typ = ev.get("type")
        if typ in self._EVENT_NO_OBJ:
            return None
        if typ in self._EVENT_SOURCE_OBJ:
            x = ev.get("source")
        elif typ == "zone":
            # The new object, or the one that left when that's gone (a token that died ceased to
            # exist, 704.5d): both have the card's key.
            x = ev.get("new")
            if x is None or (x not in self.s.objs and x not in self.s.lki_obj):
                x = ev.get("old")
        elif typ == "targeted":
            x = ev.get("target")
        elif typ in ("activated", "mana"):
            x = ev.get("source")
        else:
            x = ev.get("obj")
            if x is None or (x not in self.s.objs and x not in self.s.lki_obj and ev.get("old") is not None):
                # A sacrificed token's graveyard object ceased to exist (CR 111.7, 704.5d): the
                # permanent that was sacrificed, as for "zone" (Biotech Specialist's two
                # triggers for a Clue and a Food: `@key:clue`, `@key:food`).
                x = ev.get("old")
        return x if isinstance(x, int) and not isinstance(x, bool) else None

    def _trigger_event_sig(self, t):
        """What tells two instances of one triggered ability apart by their events (CR 603.3b):
        the event's object (its visible state: identical objects stay interchangeable), the
        amount and the player."""
        s = self.s
        ev = t.event if isinstance(t.event, dict) else {}
        x = self.trigger_event_obj(t)
        look = None
        if x is not None:
            if x in s.battlefield:
                look = ("bf", self.name_of(x), self.ctrl(x), self._trigger_source_look(x))
            elif x in s.objs:
                o = s.objs[x]
                look = (o.zone, self.name_of(x), o.owner, o.token, self._stack_links(x))
            else:
                lc = s.lki.get(x)
                lo = s.lki_obj.get(x)
                look = ("gone", getattr(lc, "name", None), getattr(lc, "controller", None),
                        getattr(lo, "token", None), self._stack_links(x))
        n = ev.get("n")
        pl = ev.get("player")
        return ("ev", look, n if isinstance(n, (int, float)) else None, pl if isinstance(pl, int) else None)

    def _name_trigger_instances(self, d, batch):
        """Format §3.1 OrderTriggers occurrence suffix (agreed with the EUT team 2026-11-08): where
        two options would share `trigger:<ref>:<n>` (two instances of one ability of one source),
        each becomes `trigger:<ref>:<n>@<event ref>`, the ref of the object its event names (no
        `@` when it names none); instances that still share a string get `/k` each, k the 1-based
        rank by amount ascending, then by the order the events happened (CR 608.2c; `batch` is in
        that order). The plain string stays an alternative of the first instance (option order)."""
        groups = {}
        for o in d.options:
            groups.setdefault(o.canon, []).append(o)
        order = {id(t): i for i, t in enumerate(batch)}

        def at(x, base):
            # An alternative keeps its own `trigger:<ref>:<n>` (an identical other source's
            # instance) and gets its own event ref (format §3.1).
            base = base.split("@", 1)[0]
            e = self.trigger_event_obj(x.data)
            r = self.ref(e) if e is not None else None
            return f"{base}@{r}" if r is not None and not r.startswith("obj:") else base

        def amount(x):
            n = (x.data.event if isinstance(x.data.event, dict) else {}).get("n")
            return n if isinstance(n, (int, float)) else 0

        for base, members in groups.items():
            if len(members) < 2:
                continue
            for o in members:
                o.canon = at(o, base)
                for a in o.alts:
                    a.canon = at(a, a.canon)
            named = {}
            for o in members:
                named.setdefault(o.canon, []).append(o)
            for c, same in named.items():
                if len(same) < 2:
                    continue
                ranked = sorted(same, key=lambda x: (amount(x), order.get(id(x.data), 0)))
                for k, o in enumerate(ranked, 1):
                    o.canon = f"{o.canon}/{k}"
                    for a in o.alts:
                        a.canon = f"{a.canon}/{k}"
            first = members[0]
            first.alts.append(Opt(base, first.data, sig=first.sig))

    def _delayed_referents_look(self, t):
        """The objects a pending delayed trigger's bound effect names (SacrificeObjs, ExileObjs,
        ReturnObjs `ids`), as their visible state; those no longer on the battlefield as "gone"
        (the effect skips them). () when the effect names none."""
        eff = getattr(t.ability, "effect", None)
        ids = getattr(eff, "ids", None)
        if not ids:
            return ()
        s = self.s
        out = []
        for x in ids:
            if x in s.objs and x in s.battlefield:
                out.append(("bf", self.name_of(x), self.ctrl(x), self._trigger_source_look(x)))
            else:
                out.append(("gone",))
        return tuple(sorted(out, key=repr))

    def _trigger_source_look(self, oid):
        """What tells two same-named trigger sources (or event objects) on the battlefield apart
        for ordering triggers: their visible state, and what is already on the stack about them
        (an ability of theirs, a spell or ability targeting them, an ability whose event names
        them), as obj_sig. Not the triggers being ordered, which name each other for two
        identical Angels and are symmetric (s.pending is empty while ordering)."""
        s = self.s
        o = s.objs[oid]
        c = self.chars(oid)
        li = getattr(self, "_look_index", None)
        if li is not None:
            att = tuple(sorted(self.name_of(a) or "" for a in li[1].get(oid, ())))
        else:
            att = tuple(sorted(self.name_of(a) or "" for a in s.battlefield if s.objs[a].attached_to == oid))
        # The prepared designation (CR 722.3a: "a designation that acts as a marker which rules
        # and effects can identify"; 722.3c: only a prepared permanent's controller may cast its
        # prepare spell's copy) tells a prepared source from an unprepared twin (format §3: chosen
        # values and designations that can matter). Campaign17 1001712 and 39 more: prowess of a
        # prepared and an unprepared Pompous Battlemage, ordered by the EUT; ours merged them and
        # the first trigger to resolve pumped the other Battlemage.
        # What the source itself is attached to (CR 301.5, 303.4): an Equipment on a creature and
        # an unattached twin aren't interchangeable. Campaign17 1005205: two Doc Ock's Tentacles,
        # one on an older Hearth Elemental; the trigger resolving first moves its Tentacles, so
        # the order decides which creature keeps +4/+4. The EUT asked, ours had merged them.
        h = o.attached_to
        host = None
        if h is not None and h in s.objs and h in s.battlefield:
            ho, hc = s.objs[h], self.chars(h)
            host = (self.name_of(h), self.ctrl(h), ho.tapped, ho.damage, tuple(sorted(ho.counters.items())),
                    hc.power, hc.toughness)
        # The modes a "choose one that hasn't been chosen this turn" ability of the source has
        # already used this turn (tracked per object, casting.py): two Monuments to Endurance
        # with different modes left do different things, and the modes are chosen as each trigger
        # is put on the stack (CR 603.3c, 700.2b), so their order is a choice (603.3b).
        # Campaign17 1022040: one Monument had drawn, the other made a Treasure; ours merged
        # their next triggers and resolved them in another order than the EUT's.
        modes = tuple(sorted((repr(k[2:]), repr(v)) for k, v in s.turn_flags.items()
                             if isinstance(k, tuple) and len(k) >= 2 and k[0] == "modes_chosen" and k[1] == oid))
        # Summoning sickness, where it can matter (as
        # obj_sig: a creature without haste whose controller is the active player, or with a
        # {T}/{Q} ability; CR 302.6). Two Guiding Hydras with eight +1/+1 counters each, one cast
        # this turn: whichever trigger resolves first decides which Hydra loses a counter, i.e.
        # whether the one that can attack is the bigger one, so their order is the controller's
        # choice (CR 603.3b). rt3-t 1101557 and 3 more: the EUT asked, ours had merged them.
        sick = bool(o.sick and "Creature" in c.types and not c.has_kw("haste") and (
            c.controller == s.active or any(a.kind == "activated" and (a.cost.tap or a.cost.untap) for a in c.abilities)))
        # The source's keywords and types (as
        # obj_sig): a Mockingbird that entered as a copy of Tiger-Seal is a flying Bird Tiger-Seal
        # (Mockingbird's copy exception, CR 707.9b), so its upkeep trigger and the real Tiger-Seals'
        # aren't interchangeable once a player can respond to one (Tishana's Tidebinder countering
        # the top one decides which Seal stays untapped): their order is the controller's choice
        # (CR 603.3b). rt4-f 1014453: ours merged them.
        vis = (tuple(c.keywords()), tuple(sorted(c.types)), tuple(sorted(c.subtypes)))
        return (o.tapped, o.damage, tuple(sorted(o.counters.items())), c.power, c.toughness, att, o.token,
                bool(o.flags.get("prepared")), host, self._stack_links(oid), modes, sick, vis)

    def _linked_cards_look(self, oid):
        """The cards in exile linked to `oid` ("exiled with",
        CR 607.2a), as (name, owner, face down) sorted; () when none."""
        from ref.engine.effects import exiled_with_ids
        s = self.s
        return tuple(sorted((self.name_of(x) or "", s.objs[x].owner, bool(s.objs[x].face_down))
                            for x in exiled_with_ids(self, oid)))

    def _build_look_index(self):
        """(object -> its _stack_links entries, host -> the permanents attached to it), in one
        pass over the stack and the battlefield; the same answers as the per-object scans."""
        links = {}
        for it in self.s.stack:
            seen = set()
            for grp in (it.targets or []):
                for t in grp:
                    if t in seen:
                        continue
                    seen.add(t)
                    links.setdefault(t, []).append(("tgt", it.id))
            if it.kind == "spell":
                continue
            if it.source is not None:
                links.setdefault(it.source, []).append(("src", it.id))
            ev = (getattr(it, "ctx", None) or {}).get("event")
            if isinstance(ev, dict):
                named = []
                for x in (ev.get("new"), ev.get("obj"), self.event_obj(ev)):
                    if x is not None and x not in named:
                        named.append(x)
                for x in named:
                    links.setdefault(x, []).append(("ev", it.id))
        att = {}
        s = self.s
        for a in s.battlefield:
            h = s.objs[a].attached_to
            if h is not None:
                att.setdefault(h, []).append(a)
        return links, att

    def _stack_links(self, oid):
        """The stack objects that make an object differ from an identical one (CR 603.3b: once
        Youthful Valkyrie #1's trigger is on the stack, its other trigger and Valkyrie #2's aren't
        interchangeable, their order decides which Valkyrie gets a counter first; the EUT asks
        it too, R4 ordering 2026-11-08).
        An ability on the stack whose trigger event names the object (its event ref, format §3.1,
        or the event's moved / affected object) links it whether or not that ability refers to
        its event: trigger identity convention agreed with the EUT team
        (2026-11-08; interfaces/scenario-and-state.md §3.1 *Trigger identity*). Two Impact
        Tremors-style instances for two identical Soldiers stay one option until one of them is
        on the stack; then the other Soldier's triggers and the first one's are told apart.
        Both are legal (603.3b: ordering identical triggers is a choice without consequence);
        this one needs no analysis of what an ability reads."""
        li = getattr(self, "_look_index", None)
        if li is not None:
            return tuple(li[0].get(oid, ()))
        out = []
        for it in self.s.stack:
            if any(t == oid for grp in (it.targets or []) for t in grp):
                out.append(("tgt", it.id))
            if it.kind == "spell":
                continue
            if it.source == oid:
                out.append(("src", it.id))
            ev = (getattr(it, "ctx", None) or {}).get("event")
            if isinstance(ev, dict) and oid in (ev.get("new"), ev.get("obj"), self.event_obj(ev)):
                out.append(("ev", it.id))
        return tuple(out)

    def put_trigger(self, t):
        s = self.s
        it = StackItem("triggered", t.controller, s.new_id())
        it.source = t.source
        it.source_lki = t.source_chars
        it.ability = t.ability
        src_obj = s.objs.get(t.source)
        if src_obj is None:
            src_obj = s.lki_obj.get(t.source)
        # CR 107.3m: an ETB ability of a permanent refers to the X of the spell it was.
        x = src_obj.x if src_obj is not None else 0
        it.ctx = {"event": t.event, "data": t.data, "source": t.source,
                  "source_chars": t.source_chars, "controller": t.controller,
                  "delayed": t.delayed, "x": x, "state_key": (t.data or {}).get("state_key")}
        o = s.objs.get(t.source)
        it.key = f"trigger:{o.key if o else (t.source_chars.name if t.source_chars else 'delayed')}:{t.index}"
        s.stack.append(it)
        ab = t.ability
        # Modes then targets (CR 603.3c-d); no legal targets -> removed from the stack.
        if ab.modes is not None:
            modes = self.choose_modes(t.controller, ab.modes, it, "trigger")
            if modes is None:
                s.stack.remove(it)
                return
            it.ctx["modes"] = modes
        specs = self.target_specs(ab, it.ctx)
        if specs:
            ok = self.choose_targets(t.controller, specs, it)
            if not ok:
                s.stack.remove(it)
                return
        # No touch here. A triggered ability put on the stack changes
        # no object's characteristics (layers._compute_all covers the battlefield and spells
        # only), and the recomputation it forced, once per trigger, made a thousand Goblins'
        # Enduring Courage triggers quadratic (rt5-t 1100981).
        self.emit({"type": "ability_put", "item": it.id, "controller": t.controller})
        if it.targets:
            self.became_targets(it)

    # ------------------------------------------------------------------ replacement (614-616)
    def replacements(self, etype, ev):
        s = self.s
        out = []
        for oid in list(s.battlefield):
            c = self.chars(oid)
            for ai, ab in enumerate(c.abilities):
                if ab.kind != "static":
                    continue
                if ab.condition is not None and not ab.condition(self, oid):
                    continue
                for pi, part in enumerate(ab.parts):
                    if part.kind == "replacement" and part.event == etype and part.applies(self, ev, oid):
                        out.append(((oid, id(ab), pi), part, oid))
        if etype == "zone" and ev.get("to") == "battlefield":
            # The entering permanent's own replacement effects (614.1c, 614.12).
            if not ev["opts"].get("face_down"):
                card = DB.get(ev["card"])
                fidx = ev["opts"].get("face", 0)
                face = card.faces[fidx] if fidx < len(card.faces) else card.faces[0]
                tok = ev.get("token_obj")
                cv = None  # copiable values that replace the card's face (CR 707.2)
                if ev.get("lki_obj") is not None and ev["lki_obj"].flags.get("copiable") is not None:
                    cv = ev["lki_obj"].flags["copiable"]
                    abilities = cv.abilities
                elif tok is not None and tok.flags.get("copiable") is not None:
                    # A token copy has the copied object's copiable values (CR 707.2, 111.4), so
                    # its own "enters with/as" abilities are those (a token copy of a Mockingbird
                    # that copied Sleep-Cursed Faerie enters tapped with three stun counters).
                    cv = tok.flags["copiable"]
                    abilities = cv.abilities
                else:
                    abilities = face.abilities() if not card.is_token or True else []
                # CR 614.12: only abilities the permanent would have on the battlefield count,
                # with continuous effects that already exist (e.g. Zhao's conqueror counter:
                # nonbasic lands lose all abilities, so a shockland's "you may pay 2 life" and
                # a land's own "enters tapped" don't apply).
                keep = self._would_have_abilities(ev) if any(
                    ab.kind == "static" and any(p.kind == "replacement" and p.event == "enter_self" for p in ab.parts)
                    for ab in abilities) else None
                for ai, ab in enumerate(abilities):
                    if ab.kind != "static":
                        continue
                    if keep is not None and getattr(ab, "text", None) and ab.text not in keep:
                        continue
                    for pi, part in enumerate(ab.parts):
                        if part.kind == "replacement" and part.event == "enter_self" and part.applies(self, ev, None):
                            out.append((("self", ai, pi), part, None))
                if cv is not None:
                    # Types and numbers of the copiable values too: a token copy of a transformed
                    # The Legend of Roku is Avatar Roku, a creature, not a Saga (CR 707.2, 712.8a:
                    # the face that's up; campaign12 804495, The Fire Crystal), so no lore counter.
                    types, subtypes, loyalty, defense = cv.types, cv.subtypes, cv.loyalty, cv.defense
                    saga = "Saga" in cv.subtypes
                else:
                    types, subtypes, loyalty, defense = face.types, face.subtypes, face.loyalty, face.defense
                    saga = card.layout == "saga" or "Saga" in face.subtypes
                if "Planeswalker" in types and isinstance(loyalty, int):
                    out.append((("loyalty",), _LOYALTY, None))  # CR 306.5b
                if "Battle" in types and isinstance(defense, int):
                    out.append((("defense",), _DEFENSE, None))  # CR 310.4b
                if saga:
                    out.append((("lore",), _LORE, None))  # CR 714.3a
        if etype == "zone" and ev.get("from") == "battlefield" and ev.get("to") == "graveyard":
            # CR 122.1h: finality counters make one replacement effect "If this permanent would be
            # put into a graveyard from the battlefield, exile it instead" (not an ability).
            fo = s.objs.get(ev.get("old"))
            if fo is not None and fo.counters.get("finality", 0) > 0:
                out.append((("finality", fo.id), _FINALITY, None))
        if etype == "destroy" and not ev.get("sba"):
            # CR 122.1c: shield counters ("destroyed as the result of an effect": not 704.5g).
            so = s.objs.get(ev.get("obj"))
            if so is not None and so.zone == "battlefield" and so.counters.get("shield", 0) > 0:
                out.append((("shield", so.id), _SHIELD_DESTROY, None))
        if etype == "damage" and not isinstance(ev.get("target"), tuple):
            so = s.objs.get(ev.get("target"))
            if so is not None and so.zone == "battlefield" and (
                    so.counters.get("shield", 0) > 0
                    or (ev.get("batch") is not None and s.turn_flags.get(("shield_event", so.id, ev["batch"])))):
                out.append((("shield", so.id), _SHIELD_DAMAGE, None))
        for ce in s.effects:
            if ce.kind == "replacement" and ce.data.get("event") == etype:
                part = ce.data["part"]
                if part.applies(self, ev, ce):
                    out.append((("ce", ce.id), part, ce))
        return out

    def _would_have_abilities(self, ev):
        """Texts of the abilities the entering permanent would have on the battlefield (CR
        614.12), or None when unknown (enters as a copy: its copiable values decide)."""
        s = self.s
        if ev["opts"].get("copy_of") is not None or (ev.get("lki_obj") is not None and ev["lki_obj"].flags.get("copiable") is not None):
            return None
        tok = ev.get("token_obj")
        if tok is not None:
            c = self.chars(tok.id)
        else:
            o = s.objs.get(ev.get("old"))
            if o is None or o.zone == "battlefield":
                return None
            saved = (o.zone, o.controller, o.face, o.face_down)
            o.zone = "battlefield"
            o.controller = ev["opts"].get("controller", o.owner)
            o.face = ev["opts"].get("face", 0) or 0
            # It enters face up unless the move says otherwise: a card played from a face-down
            # exile (Outrageous Robbery, CR 406.3) has its own abilities on the battlefield.
            o.face_down = bool(ev["opts"].get("face_down"))
            s.battlefield.append(o.id)
            s.touch()
            try:
                c = self.chars(o.id)
            finally:
                s.battlefield.remove(o.id)
                o.zone, o.controller, o.face, o.face_down = saved
                s.touch()
        return {getattr(a, "text", "") for a in c.abilities}

    def would_have_chars(self, ev):
        """The entering card's characteristics as it would exist on the battlefield (CR 614.12):
        its own static abilities, the continuous effects that already exist, and the counters
        that the replacement effects applied so far put on it (impending's time counters make an
        Overlord a noncreature enchantment, 702.176a; a planeswalker's loyalty counters make
        Kaito, Bane of Nightmares a creature during its controller's turn, 306.5b). None for a
        copy, a token or a face-down permanent (the caller has their characteristics)."""
        s = self.s
        opts = ev["opts"]
        if opts.get("copy_of") is not None or opts.get("face_down") or ev.get("token_obj") is not None or (
                ev.get("lki_obj") is not None and ev["lki_obj"].flags.get("copiable") is not None):
            return None
        o = s.objs.get(ev.get("old"))
        if o is None or o.zone == "battlefield":
            return None
        saved = (o.zone, o.controller, o.face, o.face_down, o.counters)
        o.zone = "battlefield"
        o.controller = opts.get("controller", o.owner)
        o.face = opts.get("face", 0) or 0
        o.face_down = False
        o.counters = dict(opts.get("counters") or {})  # 400.7: none carried over from its old zone
        s.battlefield.append(o.id)
        s.touch()
        try:
            c = self.chars(o.id)
        finally:
            s.battlefield.remove(o.id)
            o.zone, o.controller, o.face, o.face_down, o.counters = saved
            s.touch()
        return c

    def replace(self, etype, ev):
        applied = ev.setdefault("applied", set())
        while True:
            cands = [c for c in self.replacements(etype, ev) if c[0] not in applied]
            if not cands:
                return ev
            selfs = [c for c in cands if c[1].self_replacement]
            # CR 616.1a, 616.1c: a self-replacement effect first, else an effect that makes the
            # object a copy as it enters (it decides what the others see, 614.12), else any.
            pool = selfs or [c for c in cands if getattr(c[1], "copy_effect", False)] or cands
            if len(pool) > 1 and (self._order_matters(pool) or self._commuting_order_matters(etype, ev, pool, applied)):
                who = self.affected_player(etype, ev)
                opts = []
                for c in pool:
                    src = self.ref(c[2]) if isinstance(c[2], int) else ("self" if c[2] is None else f"effect:{c[2].id}")
                    # Format §3.1: choose:<ref of the replacement effect's source>.
                    if c[2] is None and ev.get("lki_obj") is not None:
                        src = f"key:{ev['lki_obj'].key}"
                    opts.append(Opt(f"choose:{src}", c, sig=(src, c[0][-1], c[1].text)))
                c = self.ask(Decision(who, "ChooseReplacement", opts,
                                      prompt="Choose which replacement effect to apply first (CR 616.1)."))
            else:
                c = pool[0]
            applied.add(c[0])
            c[1].apply(self, ev, c[2])
            if ev.get("cancel"):
                return ev

    def _commuting_order_matters(self, etype, ev, pool, applied):
        """Effects that modify how a permanent enters ("enters tapped", "enters with counters")
        usually commute, but not when one changes whether another applies (CR 614.12, 616.1f):
        an Overlord cast for its impending cost, under Authority of the Consuls, enters untapped
        if its time counters are put on first (it isn't a creature then) and tapped otherwise,
        and its controller chooses (616.1e). Tries each first choice on a copy of the event,
        then the rest in order (commuting effects only: their application asks nothing)."""
        if etype != "zone" or ev.get("to") != "battlefield" or not all(getattr(c[1], "commutes", False) for c in pool):
            return False
        import copy

        def outcome(first):
            ev2 = dict(ev)
            ev2["opts"] = copy.deepcopy(ev["opts"])
            done = set(applied) | {first[0]}
            first[1].apply(self, ev2, first[2])
            for _ in range(20):
                nxt = [c for c in self.replacements(etype, ev2) if c[0] not in done and getattr(c[1], "commutes", False)]
                if not nxt:
                    break
                done.add(nxt[0][0])
                nxt[0][1].apply(self, ev2, nxt[0][2])
            o = ev2["opts"]
            return (bool(o.get("tapped")), tuple(sorted((o.get("counters") or {}).items())), ev2.get("to"))
        return len({outcome(c) for c in pool}) > 1

    def _order_matters(self, pool):
        # Enters-tapped / enters-with-counters style ETB modifications commute; ask otherwise.
        # Pure redirects to the same zone ("exile it instead") give the same event whichever
        # applies first (the others then no longer apply): no choice to make (CR 616.1).
        redirs = {getattr(c[1], "redirect", None) for c in pool}
        if len(redirs) == 1 and None not in redirs:
            return False
        # Identical effects (same text, same controller, no reference to their own source):
        # whichever applies first, the result is the same (e.g. two "Players can't gain life").
        texts = {c[1].text for c in pool}
        if len(texts) == 1 and texts.pop() and "~" not in pool[0][1].text:
            ctrls = {self.ctrl(c[2]) if isinstance(c[2], int) else getattr(c[2], "controller", None) for c in pool}
            if len(ctrls) == 1 and None not in ctrls:
                return False
        return not all(getattr(c[1], "commutes", False) for c in pool)

    def affected_player(self, etype, ev):
        s = self.s
        if etype in ("draw", "lifegain"):
            return ev["player"]
        if etype == "damage":
            t = ev["target"]
            return t[1] if isinstance(t, tuple) else self.ctrl(t)
        if etype == "zone":
            o = s.objs.get(ev.get("old"))
            if o is not None:
                return self.ctrl(o.id) if o.zone in ("battlefield", "stack") else o.owner
            return ev.get("owner", s.active)
        if "obj" in ev:
            return self.ctrl(ev["obj"])
        return s.active


class _Finality:
    """CR 122.1h: the finality counter's replacement effect."""
    kind = "replacement"
    event = "zone"
    self_replacement = False
    redirect = "exile"
    text = "finality counter (CR 122.1h)"

    def applies(self, g, ev, src):
        return ev.get("from") == "battlefield" and ev.get("to") == "graveyard"

    def apply(self, g, ev, src):
        ev["to"] = "exile"


_FINALITY = _Finality()


class _ShieldDestroy:
    """CR 122.1c: one or more shield counters make one replacement effect "If this permanent would
    be destroyed as the result of an effect, instead remove a shield counter from it" (not the
    state-based action for lethal damage, 704.5g: shield rulings, e.g. Proud Pack-Rhino
    2024-06-07). Not an ability: it works on a permanent that lost all abilities."""
    kind = "replacement"
    event = "destroy"
    self_replacement = False
    text = "shield counter (CR 122.1c)"

    def applies(self, g, ev, src):
        return True

    def apply(self, g, ev, src):
        ev["cancel"] = True
        g.remove_counters(ev["obj"], "shield", 1)


class _ShieldDamage:
    """CR 122.1c: ... and one prevention effect "If damage would be dealt to this permanent,
    prevent that damage and remove a shield counter from it". Applied once per damage event:
    damage dealt at the same time (combat damage from two blockers, 510.2) is all prevented and
    one counter is removed (rulings: "only one shield counter is removed"). Damage that can't be
    prevented is dealt, and the counter is still removed (615.12, 615.12a)."""
    kind = "replacement"
    event = "damage"
    self_replacement = False
    text = "shield counter (CR 122.1c)"

    def applies(self, g, ev, src):
        return ev["n"] > 0

    def apply(self, g, ev, src):
        from .effects import _cant_prevent
        tgt, batch = ev["target"], ev.get("batch")
        tf = g.s.turn_flags
        key = ("shield_event", tgt, batch)
        if batch is None or not tf.get(key):
            if batch is not None:
                tf[key] = True
            g.remove_counters(tgt, "shield", 1)
        if not _cant_prevent(g, ev):
            ev["prevented"] = ev.get("prevented", 0) + ev["n"]
            ev["n"] = 0
            ev["cancel"] = True


_SHIELD_DESTROY = _ShieldDestroy()
_SHIELD_DAMAGE = _ShieldDamage()


class _EnterCounters:
    kind = "replacement"
    event = "enter_self"
    self_replacement = False
    commutes = True

    def __init__(self, kind, value_fn, text):
        self.counter = kind
        self.value_fn = value_fn
        self.text = text

    def applies(self, g, ev, src):
        return True

    def apply(self, g, ev, src):
        n = self.value_fn(g, ev)
        cs = ev["opts"].setdefault("counters", {})
        cs[self.counter] = cs.get(self.counter, 0) + n


def _copiable_of(ev):
    """The entering object's copiable values when they replace its card's face (a copy, CR 707.2)."""
    for o in (ev.get("lki_obj"), ev.get("token_obj")):
        if o is not None and o.flags.get("copiable") is not None:
            return o.flags["copiable"]
    return None


def _loyalty(g, ev):
    cv = _copiable_of(ev)
    if cv is not None and isinstance(cv.loyalty, int):
        return cv.loyalty
    card = DB.get(ev["card"])
    f = card.faces[ev["opts"].get("face", 0)]
    return f.loyalty


def _defense(g, ev):
    cv = _copiable_of(ev)
    if cv is not None and isinstance(cv.defense, int):
        return cv.defense
    card = DB.get(ev["card"])
    f = card.faces[ev["opts"].get("face", 0)]
    return f.defense


_LOYALTY = _EnterCounters("loyalty", _loyalty, "enters with loyalty counters (CR 306.5b)")
_DEFENSE = _EnterCounters("defense", _defense, "enters with defense counters (CR 310.4b)")
_LORE = _EnterCounters("lore", lambda g, ev: 1, "enters with a lore counter (CR 714.3a)")
