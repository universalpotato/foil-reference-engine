"""Zone changes, events and basic game actions (CR 400, 120-122, 701).

Every zone change goes through `move_batch`: replacement effects are applied (614), a new
object is created in the destination zone (400.7), last known information of the old object is
kept (608.2h, 603.10), and a `zone` event is emitted with a snapshot of the battlefield as it
was before the batch (for leaves-the-battlefield look-back, 603.10a).
"""
import copy

from .objects import Obj
from .. import Unsupported

# Performance limit (not a rule): token creation past this many permanents raises Unsupported.
# Raised from 1,000 to 3,000 (z3, 2026-11-14) after removing the per-call battlefield scans that
# made big boards quadratic (combat._rule_index: rule_parts; triggers._build_look_index: ordering
# triggers). What remains is linear per recomputation: every touch recomputes every permanent's
# characteristics (layers._compute_all), so a thousand life-gain triggers on a 3,000-permanent
# board take minutes; doubling past that would take hours.
MAX_PERMANENTS = 3000

PUBLIC = ("battlefield", "graveyard", "exile", "stack")


class ZonesMixin:
    # ------------------------------------------------------------------ helpers
    def obj(self, oid):
        return self.s.objs.get(oid)

    def ctrl(self, oid):
        """Current controller (layer 2) of a permanent/spell; owner for cards elsewhere."""
        o = self.s.objs.get(oid)
        if o is None:
            c = self.s.lki.get(oid)
            return c.controller if c else None
        if o.zone in ("battlefield", "stack"):
            return self.chars(oid).controller
        return o.owner

    def zone_list(self, o):
        s = self.s
        if o.zone == "battlefield":
            return s.battlefield
        if o.zone == "stack":
            return None
        if o.zone == "command":
            return s.command
        return s.players[o.owner].zone(o.zone)

    def new_key(self, name):
        n = self.s.key_counts.get(name, 0) + 1
        self.s.key_counts[name] = n
        return f"new:{name}:{n}"

    def ref(self, x):
        """Canonical reference string of an object id, ('player', p) or ('stack', item id).
        An activated or triggered ability on the stack is named by its stack object (CR 113.7a,
        115.1: a target of its own, not its source): `stack:N`, N its 1-based position from the
        bottom of the stack, `stack:gone` once it has left (format §2)."""
        if isinstance(x, tuple):
            if x[0] == "stack":
                pos = next((i for i, it in enumerate(self.s.stack) if it.id == x[1]), None)
                return f"stack:{pos + 1}" if pos is not None else "stack:gone"
            return f"p{x[1]}"
        o = self.s.objs.get(x)
        if o is None:
            o = self.s.lki_obj.get(x)
            if o is None:
                return f"obj:{x}"
        return o.key if o.key.startswith("new:") else f"key:{o.key}"

    def name_of(self, oid):
        try:
            c = self.chars(oid)
        except KeyError:
            return "?"
        return c.name or "face-down"

    def emit(self, ev):
        """Check triggered abilities right away (CR 603.2)."""
        know = getattr(self.s, "know", None)
        if know is not None:
            know.event(self, ev)  # who saw what (knowledge.py): reveals, looks, searches
        self.check_triggers(ev)
        if ev.get("type") == "targeted":
            # Whether an object has become the target of a spell or ability a player controls
            # this turn, recorded after the event's triggers are checked ("for the first time each
            # turn", valiant: ref/cards/round2_a2.py), whatever the object's abilities were then.
            # One dict entry (copied on write): not a per-object key, which obj_sig would count
            # as a difference between identical objects.
            tb = dict(self.s.turn_flags.get("targeted_by") or {})
            tb[(ev.get("target"), ev.get("controller"))] = True
            self.s.turn_flags["targeted_by"] = tb

    def lki_snapshot(self):
        return {oid: self.chars(oid) for oid in self.s.battlefield}

    # ------------------------------------------------------------------ zone changes
    def move(self, oid, to, **kw):
        r = self.move_batch([(oid, to, kw)])
        return r[0] if r else None

    def move_batch(self, moves, cause=None):
        """moves: list of (oid, to_zone, opts). opts: pos ('top'|'bottom'|int), tapped, controller,
        counters ({kind: n}), face_down, attacking, cause, owner_library. Returns new ids (None
        for moves that didn't happen)."""
        s = self.s
        bf_lki = self.lki_snapshot() if any(s.objs[m[0]].zone == "battlefield" for m in moves if m[0] in s.objs) else None
        evs = []
        results = []
        planned = []
        for oid, to, opts in moves:
            o = s.objs.get(oid)
            if o is None:
                results.append(None)
                continue
            ev = {"type": "zone", "old": oid, "from": o.zone, "to": to, "opts": dict(opts),
                  "card": o.card, "owner": o.owner, "cause": opts.get("cause", cause),
                  "applied": set(), "lki_obj": copy.copy(o)}
            if to == "battlefield":
                ev["opts"].setdefault("controller", opts.get("controller", o.owner))
            ev = self.replace("zone", ev)
            if ev.get("cancel"):
                results.append(None)
                continue
            if ev["to"] == "battlefield" and not self._may_enter_battlefield(o, ev["opts"]):
                results.append(None)  # CR 304.4, 307.4: it remains in its previous zone
                continue
            planned.append(ev)
        # LKI before anything moves (608.2h).
        for ev in planned:
            oid = ev["old"]
            o = s.objs[oid]
            try:
                ev["lki"] = self.chars(oid)
            except Unsupported:
                raise
            ev["lki_obj"] = copy.copy(o)
            ev["lki_obj"].counters = dict(o.counters)
            ev["controller"] = self.ctrl(oid)
        # One id per batch of simultaneous zone changes (one event, CR 603.2c: "whenever one or
        # more cards leave your graveyard" triggers once). In the state, so re-runs agree.
        tf = s.turn_flags
        tf["move_batches"] = tf.get("move_batches", 0) + 1
        mbid = (s.turn, tf["move_batches"])
        for ev in planned:
            nid = self._do_move(ev)
            ev["new"] = nid
            ev["bf_lki"] = bf_lki
            ev["move_batch"] = mbid
            results.append(nid)
        s.touch()
        hosts = []
        for ev in planned:
            self.after_move(ev, hosts)
        if hosts:
            # CR 610.3d: the "until ~ leaves" returns of hosts that left at the same time are
            # simultaneous too (two exiled Hinterland Sanctifiers each see the other enter, 603.6a).
            self.until_leaves_returns(*hosts)
        for ev in planned:
            self.emit(ev)
        return [r for r in results] if len(results) == len(moves) else results

    def _may_enter_battlefield(self, o, opts):
        """CR 304.4, 307.4: an instant or sorcery can't enter the battlefield; it remains in its
        previous zone. A double-faced card put onto the battlefield from a zone other than the
        stack enters front face up unless the effect says "transformed" (712.14, 712.14a), so
        Summon: Esper Maduin exiled and returned (front face: the sorcery Esper Origins) stays in
        exile (ruling 2025-06-06); 712.14b says the same for modal double-faced cards. Not for
        spells resolving from the stack, face-down entries (708.2: a face-down 2/2 whatever the
        card is), tokens, copies and permanents entering as a copy."""
        if o.zone in ("stack", "battlefield") or opts.get("face_down") or opts.get("copy_of") is not None:
            return True
        if o.token or o.is_copy or o.flags.get("copiable") is not None:
            return True
        from ..cards.model import DB
        rc = DB.get(o.card)
        face = opts.get("face", 0) if rc.layout == "transform" else 0
        if face >= len(rc.faces):
            return True
        types = set(rc.faces[face].types)
        return not types or not types <= {"Instant", "Sorcery"}

    def _remove_from_zone(self, o):
        s = self.s
        if o.zone == "battlefield":
            s.battlefield.remove(o.id)
        elif o.zone == "stack":
            for it in s.stack:
                if it.kind == "spell" and it.obj == o.id:
                    # The spell as it last existed on the stack, with the decisions made for it
                    # (CR 608.2h, 707.10: "copy this spell" after it left; casting.copy_spell).
                    s.lki_spell[o.id] = it
            s.stack = [it for it in s.stack if not (it.kind == "spell" and it.obj == o.id)]
        elif o.zone == "command":
            s.command.remove(o.id)
        elif o.zone in ("library", "hand", "graveyard", "exile"):
            s.players[o.owner].zone(o.zone).remove(o.id)

    def _do_move(self, ev):
        s = self.s
        o = s.objs[ev["old"]]
        to = ev["to"]
        opts = ev["opts"]
        s.lki[o.id] = ev["lki"]
        s.lki_obj[o.id] = ev["lki_obj"]
        self._remove_from_zone(o)
        if ev["from"] == "battlefield":
            self._left_battlefield(o)
        del s.objs[o.id]
        know = getattr(s, "know", None)
        if to == "none":
            if know is not None:
                know.moved(self, o, None)
            return None
        n = Obj(s.new_id(), o.card, o.owner, to)
        n.key = o.key
        n.token = o.token
        if o.copy_of_spell and to == "battlefield":
            n.token = True  # CR 707.10f, 608.3f: a copy of a permanent spell becomes a token as it resolves
        if o.is_copy and ev["from"] == "stack" and to == "battlefield":
            n.token = True  # CR 707.12, 608.3f: a cast copy of a permanent card too (Uldaros Theorix)
        n.face = 0
        if o.token or o.is_copy:
            n.flags["copiable"] = o.flags.get("copiable")
        n.is_copy = o.is_copy
        n.timestamp = s.new_ts()  # CR 613.7d
        n.zone_ts = n.timestamp
        s.objs[n.id] = n
        if to == "battlefield":
            n.controller = opts.get("controller", ev.get("enter_controller", o.owner))
            n.tapped = bool(opts.get("tapped"))
            n.face = opts.get("face", 0)
            n.face_down = bool(opts.get("face_down"))
            n.face_down_kind = opts.get("face_down_kind")
            n.sick = True
            n.entered_turn = s.turn
            for k, v in (opts.get("counters") or {}).items():
                if v:
                    n.counters[k] = n.counters.get(k, 0) + v
            if opts.get("chosen"):
                n.chosen = dict(opts["chosen"])
            if opts.get("prepared"):
                n.flags["prepared"] = True  # "enters prepared" (CR 722.3a, ref/cards/recent.py)
            if opts.get("copy_of") is not None:
                cop = opts["copy_of"].copy()
                n.flags["copiable"] = cop  # CR 707.5: enters as a copy (layer 1)
            if o.zone == "stack":
                n.cast = o.cast
                n.x = o.x
                n.kicked = o.kicked
                n.mana_spent = o.mana_spent
                n.face = o.face if o.face and opts.get("face") is None else n.face
                n.chosen.update(o.chosen)
                from ..cards.model import DB
                rc = DB.get(o.card)
                if rc.layout == "split" and "Room" in rc.faces[0].subtypes:
                    # CR 709.5d: the half cast as the spell is unlocked as it enters (Rooms:
                    # ref/cards/round2_a2.py, layers.base_chars, altcast.room_unlock_options).
                    n.flags["unlocked"] = (o.face or 0,)
            if opts.get("attach_to") is not None:
                n.attached_to = opts["attach_to"]
            s.battlefield.append(n.id)
            if opts.get("attacking") is not None and s.combat is not None:
                s.combat.attackers[n.id] = opts["attacking"]
        elif to == "stack":
            n.controller = opts.get("controller", o.owner)
            n.face = opts.get("face", 0)
            n.face_down = bool(opts.get("face_down"))
        elif to == "library":
            lib = s.players[o.owner].library
            pos = opts.get("pos", "top")
            if pos == "top":
                lib.insert(0, n.id)
            elif pos == "bottom":
                lib.append(n.id)
            else:
                lib.insert(min(int(pos), len(lib)), n.id)
        elif to in ("hand", "graveyard", "exile"):
            s.players[o.owner].zone(to).append(n.id)
            if to == "exile" and opts.get("face_down"):
                n.face_down = True
        elif to == "command":
            s.command.append(n.id)
        else:
            raise Unsupported(f"zone {to}")
        if opts.get("link") is not None:
            # CR 607.2a: "exiled with" links: (source obj id, tag)
            s.linked.setdefault(opts["link"], []).append(n.id)
        if know is not None:
            know.moved(self, o, n)  # who still knows the card (knowledge.py, CR 400.7)
        return n.id

    def _left_battlefield(self, o):
        s = self.s
        # Things attached to it stay; SBA handle them (704.5m-n). Unattach this object.
        o.attached_to = None
        if s.combat is not None:
            self.remove_from_combat(o.id)

    def after_move(self, ev, hosts=None):
        """Bookkeeping right after a move: CR 610.3 returns for "until ~ leaves" (collected in
        `hosts` when given, for one simultaneous return of a batch, 610.3d)."""
        s = self.s
        if ev["from"] == "battlefield":
            if hosts is None:
                self.until_leaves_returns(ev["old"])
            else:
                hosts.append(ev["old"])
            if ev["to"] == "graveyard" and "Creature" in ev["lki"].types:
                s.turn_flags["creatures_died"] = s.turn_flags.get("creatures_died", 0) + 1
                k = f"creatures_died_p{ev['controller']}"
                s.turn_flags[k] = s.turn_flags.get(k, 0) + 1
            if ev["to"] == "graveyard" and "Artifact" in ev["lki"].types:
                # "artifacts that were put into graveyards from the battlefield this turn" (Anzrag's
                # Rampage; tokens included, ruling 2024-02-02).
                s.turn_flags["artifacts_to_gy"] = s.turn_flags.get("artifacts_to_gy", 0) + 1
            # Leaving the battlefield to any zone, tokens included (ref/cards/round2_a2.py): void
            # (an ability word, CR 207.2c: "if a nonland permanent left the battlefield this turn",
            # Plasma Bolt, Tragic Trajectory) and "each creature that left the battlefield under
            # your control this turn" (Kutzil's Flanker; the controller as it last existed).
            if "Land" not in ev["lki"].types:
                s.turn_flags["nonland_left"] = s.turn_flags.get("nonland_left", 0) + 1
            if "Creature" in ev["lki"].types:
                k = f"creatures_left_p{ev['controller']}"
                s.turn_flags[k] = s.turn_flags.get(k, 0) + 1
        if ev["to"] == "graveyard":
            # CR 700.11: a player descended if a permanent card was put into their graveyard from
            # anywhere this turn (tokens and copies aren't cards).
            n = s.objs.get(ev.get("new"))
            if n is not None and n.zone == "graveyard" and not n.token and not n.is_copy and (
                    {"Artifact", "Battle", "Creature", "Enchantment", "Land", "Planeswalker"} & self.chars(n.id).types):
                k = f"descended_p{n.owner}"
                s.turn_flags[k] = s.turn_flags.get(k, 0) + 1

    def remove_from_combat(self, oid):
        c = self.s.combat
        if c is None:
            return
        c.attackers.pop(oid, None)
        c.blockers.pop(oid, None)
        for b, atts in c.blockers.items():
            if oid in atts:
                atts.remove(oid)
        for a, d in list(c.attackers.items()):
            if d == oid:
                c.attackers[a] = None  # attacking a planeswalker that left (CR 506.4c)

    # ------------------------------------------------------------------ "until" effects (610.3)
    def until_leaves_returns(self, *hosts):
        s = self.s
        keep = []
        returns = []
        for rec in s.until_leaves:
            if rec["host"] in hosts:
                returns.append(rec)
            else:
                keep.append(rec)
        s.until_leaves = keep
        moves = []
        for rec in returns:
            for oid in rec["objs"]:
                o = s.objs.get(oid)
                if o is None or o.zone != rec["zone"]:
                    continue  # it already left exile: a new object, not returned (400.7)
                to = rec.get("back", "battlefield")
                moves.append((oid, to, {"controller": o.owner, "cause": "until"}))  # 610.3c
        if moves:
            self.move_batch(moves)

    # ------------------------------------------------------------------ actions
    def tap(self, oid):
        o = self.s.objs[oid]
        if o.tapped:
            return False
        o.tapped = True
        self.s.touch()
        self.emit({"type": "tapped", "obj": oid})
        return True

    def untap(self, oid):
        o = self.s.objs[oid]
        if not o.tapped:
            return False
        # CR 122.1d: stun counters replace untapping.
        if o.counters.get("stun", 0) > 0:
            o.counters["stun"] -= 1
            if o.counters["stun"] == 0:
                del o.counters["stun"]
            self.s.touch()
            return False
        ev = self.replace("untap", {"type": "untap", "obj": oid, "applied": set()})
        if ev.get("cancel"):
            return False
        o.tapped = False
        self.s.touch()
        self.emit({"type": "untapped", "obj": oid})
        return True

    def add_counters(self, oid, kind, n, source=None):
        if n <= 0:
            return 0
        o = self.s.objs.get(oid)
        if o is None:
            return 0
        ev = self.replace("counters", {"type": "counters", "obj": oid, "kind": kind, "n": n,
                                       "source": source, "applied": set()})
        if ev.get("cancel") or ev["n"] <= 0:
            return 0
        n = ev["n"]
        o.counters[kind] = o.counters.get(kind, 0) + n
        if o.zone == "battlefield":
            # CR 613.7c: every counter of that kind gets the new counter's timestamp (keyword
            # counters apply in layer 6 in timestamp order with the other effects, 613.1f).
            o.counter_ts = dict(o.counter_ts, **{kind: self.s.new_ts()})
        self.s.touch()
        self.emit({"type": "counter_added", "obj": oid, "kind": kind, "n": n, "source": source})
        return n

    def remove_counters(self, oid, kind, n):
        o = self.s.objs.get(oid)
        if o is None:
            return 0
        have = o.counters.get(kind, 0)
        k = min(have, n)
        if k <= 0:
            return 0
        o.counters[kind] = have - k
        if o.counters[kind] == 0:
            del o.counters[kind]
        self.s.touch()
        self.emit({"type": "counter_removed", "obj": oid, "kind": kind, "n": k})
        return k

    def player_counters(self, p, kind, n):
        pl = self.s.players[p]
        pl.counters[kind] = pl.counters.get(kind, 0) + n
        if kind == "poison":
            pl.poison += n

    def gain_life(self, p, n, source=None):
        if n <= 0:
            return 0
        ev = self.replace("lifegain", {"type": "lifegain", "player": p, "n": n, "source": source,
                                       "applied": set()})
        if ev.get("cancel") or ev["n"] <= 0:
            return 0
        pl = self.s.players[p]
        pl.life += ev["n"]
        pl.life_gained += ev["n"]
        self.s.touch()
        self.emit({"type": "lifegain", "player": p, "n": ev["n"], "source": source})
        return ev["n"]

    def lose_life(self, p, n, source=None):
        if n <= 0:
            return 0
        ev = self.replace("lifeloss", {"type": "lifeloss", "player": p, "n": n, "source": source,
                                       "applied": set()})
        n = ev["n"]
        if ev.get("cancel") or n <= 0:
            return 0
        pl = self.s.players[p]
        pl.life -= n
        pl.life_lost += n
        self.s.touch()
        self.emit({"type": "lifeloss", "player": p, "n": n, "source": source})
        return n

    def pay_life(self, p, n):
        """CR 119.4: paying life is losing life (only if life >= n, checked by the caller)."""
        return self.lose_life(p, n)

    def draw(self, p, n=1):
        drawn = []
        if n >= 1:
            # An instruction to draw n cards, before the replacements of each draw (Quantum
            # Riddler: "if you would draw one or more cards, you draw that many plus one", CR
            # 614.1a; rulings 2025-07-25).
            ev0 = self.replace("draw_n", {"type": "draw_n", "player": p, "n": n, "applied": set()})
            n = 0 if ev0.get("cancel") else ev0["n"]
        for _ in range(n):
            ev = self.replace("draw", {"type": "draw", "player": p, "applied": set()})
            if ev.get("cancel"):
                continue
            pl = self.s.players[p]
            if not pl.library:
                pl.drew_from_empty = True  # CR 704.5b
                continue
            top = pl.library[0]
            self.draw_hook(p, top)
            top = pl.library[0]
            nid = self.move(top, "hand", cause="draw")
            if nid is not None:
                drawn.append(nid)
                pl.turn_stats["draws"] = pl.turn_stats.get("draws", 0) + 1
                self.emit({"type": "draw", "player": p, "obj": nid,
                           "nth": pl.turn_stats["draws"]})
        return drawn

    def draw_hook(self, p, top):
        """Hook for the differential harness (align hidden library order before a draw)."""

    def look_hook(self, p, i):
        pass

    def explore_hook(self, p):
        pass

    def dig_hook(self, p, n, rest_to=None, pick_to=None):
        """Before the top n cards of p's library are looked at by a "look at the top N" effect
        (`rest_to`: the public zone the cards not chosen go to, if any)."""

    def top_hook(self, p, n, zone):
        """Before the top n cards of p's library are put into a public zone (mill, exile)."""

    def mill(self, p, n):
        pl = self.s.players[p]
        if n > 0 and pl.library:
            self.top_hook(p, min(n, len(pl.library)), "graveyard")  # CR 701.17a: the top cards
        ids = pl.library[:n]
        return [x for x in self.move_batch([(i, "graveyard", {"cause": "mill"}) for i in ids]) if x]

    def discard(self, p, oid, cause=None):
        # Names discarded this game (bookkeeping for the differential harness; no rule reads it).
        # A tuple: player flags are copied shallowly on clone, so values must not be mutated.
        fl = self.s.players[p].flags
        fl["discarded"] = tuple(fl.get("discarded", ())) + (self.s.objs[oid].card,)
        nid = self.move(oid, "graveyard", cause="discard")
        self.s.players[p].turn_stats["discards"] = self.s.players[p].turn_stats.get("discards", 0) + 1
        self.emit({"type": "discard", "player": p, "obj": nid, "cause": cause})
        return nid

    def shuffle(self, p):
        lib = self.s.players[p].library
        self.s.rng.shuffle(lib)
        self.shuffle_hook(p)
        if getattr(self.s, "know", None) is not None:
            self.s.know.shuffle(self, p)  # CR 701.20d: the shuffled cards can't be told apart
        self.emit({"type": "shuffle", "player": p})

    def shuffle_hook(self, p):
        pass

    def destroy(self, oid, cause=None, regen=True):
        """CR 701.8. Indestructible permanents aren't destroyed (702.12b)."""
        c = self.chars(oid)
        if c.has_kw("indestructible"):
            return None
        ev = self.replace("destroy", {"type": "destroy", "obj": oid, "applied": set(), "regen": regen})
        if ev.get("cancel"):
            return None
        return self.move(oid, "graveyard", cause=cause or "destroy")

    def sacrifice(self, oid, cause=None):
        """CR 701.21: only permanents you control; not "destroy" (indestructible doesn't help)."""
        p = self.ctrl(oid)
        # The battlefield as it was: "whenever you sacrifice ..." looks back (CR 603.10a).
        bf_lki = self.lki_snapshot() if oid in self.s.objs and self.s.objs[oid].zone == "battlefield" else None
        nid = self.move(oid, "graveyard", cause="sacrifice")
        if p is not None:  # "permanents you've sacrificed this turn" (Obsessive Pursuit)
            ts = self.s.players[p].turn_stats
            ts["sacrificed"] = ts.get("sacrificed", 0) + 1
        # CR 603.10a: abilities that trigger when a player sacrifices a permanent look back in
        # time: the battlefield before the sacrifice, the sacrificed permanent included (The
        # Sackville-Bagginses token's own "Whenever you sacrifice a token"; campaign12 812531).
        self.emit({"type": "sacrificed", "obj": nid, "old": oid, "player": p,
                   "lki": self.s.lki.get(oid), "bf_lki": bf_lki})
        return nid

    def sacrifice_batch(self, oids):
        """Permanents one instruction sacrifices ("Sacrifice them", "each player sacrifices ...")
        leave the battlefield at the same time (CR 701.21a, 608.2c; 101.4: the choices first, then
        the action): one event, so "whenever one or more creatures die" triggers once (603.2c,
        603.10a; Chainsaw and mobilize's Warriors, round 2 merge recheck 983096)."""
        oids = [o for o in oids if o in self.s.objs and self.s.objs[o].zone == "battlefield"]
        if len(oids) <= 1:
            return [self.sacrifice(o) for o in oids]
        ps = [self.ctrl(o) for o in oids]
        bf_lki = self.lki_snapshot()  # "whenever you sacrifice ..." looks back (CR 603.10a)
        res = self.move_batch([(o, "graveyard", {"cause": "sacrifice"}) for o in oids])
        for oid, p, nid in zip(oids, ps, res):
            if p is not None:
                ts = self.s.players[p].turn_stats
                ts["sacrificed"] = ts.get("sacrificed", 0) + 1
            self.emit({"type": "sacrificed", "obj": nid, "old": oid, "player": p, "bf_lki": bf_lki,
                       "lki": self.s.lki.get(oid)})
        return res

    def create_token(self, carddef, controller, n=1, tapped=False, attacking=None, counters=None,
                     copiable=None, face_down=False, attach_to=None):
        """CR 111.2: a token is created on the battlefield under the creating player's control.
        attach_to: an Aura token created attached to that object (a Role "attached to target
        creature", CR 303.7, 111.10j-r; the effect says what it enchants, 303.4f). A replacement
        may change it as the token enters (303.4f) or cancel the token (303.4g)."""
        s = self.s
        out = []
        ev = self.replace("token", {"type": "token", "n": n, "controller": controller,
                                    "card": carddef.name, "applied": set()})
        n = ev["n"]
        if len(s.battlefield) + n > MAX_PERMANENTS:
            # Not a rules gap: the engine recomputes characteristics, triggers and block legality
            # over the whole battlefield, so thousands of permanents (e.g. Krenko, Mob Boss
            # doubling Goblins every turn) take hours. Stop loudly instead of hanging.
            raise Unsupported(f"more than {MAX_PERMANENTS} permanents on the battlefield "
                              f"(reference engine performance limit; {len(s.battlefield)} + {n} tokens)")
        entering = set()  # tokens created by one effect enter at the same time
        # Identical tokens entering together don't apply replacement effects to each other
        # (CR 614.12: only effects that already exist and the token's own abilities count), so
        # if none applied to the first token of the batch, none applies to the others: skip the
        # battlefield-wide replacement check for them (huge batches, e.g. Krenko, Mob Boss).
        plain = None
        for _ in range(n):
            o = Obj(s.new_id(), carddef.name, controller, "battlefield")
            entering.add(o.id)
            o.token = True
            o.controller = controller
            kname = carddef.faces[0].name if not copiable else copiable.name
            if copiable is not None and copiable.room is not None:
                # A token copy of a Room enters with both doors locked and
                # so has no name (CR 709.5, 709.5d); its handle names the card the way the EUT's
                # does: `new:<A // B> (both doors locked):<n>` (format §2).
                kname = f"{copiable.room[0]} (both doors locked)"
            o.key = self.new_key(kname or "token")
            o.timestamp = s.new_ts()
            o.tapped = tapped
            o.entered_turn = s.turn
            if attach_to is not None:
                o.attached_to = attach_to  # "create a Role token attached to that creature" (303.7, 111.10)
            if copiable is not None:
                o.flags["copiable"] = copiable
            if counters:
                o.counters.update(counters)
            # Enter-the-battlefield replacements of the token itself (614.1c, e.g. counters).
            eev = {"type": "zone", "old": None, "from": None, "to": "battlefield",
                   "opts": {"tapped": tapped, "counters": dict(counters or {}),
                            **({"attach_to": attach_to} if attach_to is not None else {})},
                   "card": carddef.name, "owner": controller, "applied": set(), "token_obj": o,
                   # The token is already listed on the battlefield (its own "enters with"
                   # abilities apply, 614.12), but it and its batch haven't entered yet: effects
                   # counting what's already there must skip them (614.12a).
                   "entering": entering}
            s.objs[o.id] = o
            s.battlefield.append(o.id)
            s.touch()
            if plain is None:
                opts0 = {"tapped": tapped, "counters": dict(counters or {}),
                         **({"attach_to": attach_to} if attach_to is not None else {})}
                eev = self.replace("zone", eev)
                plain = not eev.get("applied") and eev["opts"] == opts0
            elif not plain:
                eev = self.replace("zone", eev)
            if eev.get("cancel"):
                # CR 303.4g: an Aura token with nothing it could enchant isn't created (a token
                # copy of an Aura, Adagia ruling 2025-07-25).
                s.battlefield.remove(o.id)
                del s.objs[o.id]
                entering.discard(o.id)
                s.touch()
                continue
            if eev["opts"].get("attach_to") is not None:
                o.attached_to = eev["opts"]["attach_to"]  # 303.4f: chosen as it enters
            o.tapped = bool(eev["opts"].get("tapped"))
            for k, v in (eev["opts"].get("counters") or {}).items():
                if v:
                    o.counters[k] = v
            if eev["opts"].get("chosen"):
                o.chosen.update(eev["opts"]["chosen"])
            if eev["opts"].get("copy_of") is not None:
                # CR 707.5: the token's own "enter as a copy" replacement (a token copy of
                # Superior Spider-Man has Mind Swap, 707.2) makes it enter as that copy.
                o.flags["copiable"] = eev["opts"]["copy_of"].copy()
            if eev["opts"].get("prepared"):
                # "This creature enters prepared" of a token copy of a preparation card (CR 722.2b,
                # 722.3a: the prepare spell is part of its copiable values; Three Steps Ahead
                # copying Carnivorous Cultivator, campaign r2b2 981208). Was set but not kept.
                o.flags["prepared"] = True
            s.touch()
            if attacking is not None and s.combat is not None:
                s.combat.attackers[o.id] = attacking
            out.append(o.id)
        for oid in out:
            self.emit({"type": "zone", "old": None, "new": oid, "from": None, "to": "battlefield",
                       "card": s.objs[oid].card, "owner": controller, "token": True,
                       "lki": None, "bf_lki": None, "controller": controller})
            self.emit({"type": "token_created", "obj": oid, "controller": controller})
        return out

    # ------------------------------------------------------------------ damage (CR 120)
    def damage_batch_start(self):
        """Damage dealt simultaneously (combat damage, CR 510.2; one effect dealing damage to
        several recipients): lifelink gains are collected per source until damage_batch_end."""
        prev = getattr(self, "_lifelink_batch", None)
        self._lifelink_batch = {}
        self._dealt_batches = list(getattr(self, "_dealt_batches", None) or []) + [{}]
        self._damage_ids = list(getattr(self, "_damage_ids", None) or []) + [self._new_damage_id()]
        return prev

    def _new_damage_id(self):
        """An id per simultaneous damage event ("damage" events carry it as "batch": "whenever one
        or more opponents are dealt noncombat damage" triggers once per event, Master of Barbs,
        ref/cards/round2_a2.py). Kept in the state so it's the same on every re-run."""
        tf = self.s.turn_flags
        tf["damage_ids"] = tf.get("damage_ids", 0) + 1
        return tf["damage_ids"]

    def damage_batch_end(self, prev):
        batch, self._lifelink_batch = self._lifelink_batch, prev
        for (ctrl, src), n in batch.items():  # one life gain event per source (702.15e)
            self.gain_life(ctrl, n, source=src)
        stack = getattr(self, "_dealt_batches", None) or [{}]
        dealt, self._dealt_batches = stack[-1], stack[:-1]
        self._damage_ids = (getattr(self, "_damage_ids", None) or [None])[:-1]
        for src, (n, sc, combat) in dealt.items():
            self._emit_source_dealt(src, n, sc, combat)

    def _emit_source_dealt(self, src, n, sc, combat):
        """"Whenever ~ deals damage": one event per source for damage it dealt simultaneously
        (CR 603.2c, 510.2), with the total amount ("that much")."""
        self.emit({"type": "source_dealt_damage", "source": src, "n": n, "combat": combat,
                   "source_chars": sc})

    def deal_damage(self, source, target, amount, combat=False, source_chars=None, source_controller=None):
        """target: obj id or ('player', p). Returns damage actually dealt. source_controller: the
        controller of a source that is no object here, an emblem (CR 114.2: owned and controlled
        by its player), for "a source you control" (Tomik, Izzet Sparkmage)."""
        if amount <= 0:
            return 0
        sc = source_chars or (self.chars(source) if source in self.s.objs else self.s.lki.get(source))
        ids = getattr(self, "_damage_ids", None)
        ev = {"type": "damage", "source": source, "target": target, "n": amount,
              "combat": combat, "applied": set(), "source_chars": sc,
              "source_controller": source_controller if sc is None else None,
              # The simultaneous damage event this is part of (None: an event of its own); a
              # shield counter's prevention applies once per event (CR 122.1c, 615.12a).
              "batch": ids[-1] if ids else None}
        ev = self.replace("damage", ev)
        n = ev["n"]
        if ev.get("cancel") or n <= 0:
            return 0
        target = ev["target"]
        s = self.s
        if isinstance(target, tuple):
            p = target[1]
            if sc is not None and sc.has_kw("infect"):
                self.player_counters(p, "poison", n)  # CR 702.90b
            else:
                # CR 120.3a: damage to a player causes that much life loss (a life-loss event).
                ev2 = self.replace("lifeloss", {"type": "lifeloss", "player": p, "n": n, "source": source,
                                                "applied": set(), "damage": True})
                k = ev2["n"] if not ev2.get("cancel") else 0
                if k > 0:
                    s.players[p].life -= k
                    s.players[p].life_lost += k
                    self.emit({"type": "lifeloss", "player": p, "n": k, "source": source})
        else:
            o = s.objs.get(target)
            if o is None or o.zone != "battlefield":
                return 0
            tc = self.chars(target)
            if "Planeswalker" in tc.types:
                self.remove_counters(target, "loyalty", n)  # CR 120.3c
            if "Battle" in tc.types:
                self.remove_counters(target, "defense", n)
            if "Creature" in tc.types:
                if sc is not None and (sc.has_kw("infect") or sc.has_kw("wither")):
                    self.add_counters(target, "-1/-1", n)  # CR 120.3d
                else:
                    o.damage += n  # CR 120.3e
                if sc is not None and sc.has_kw("deathtouch"):
                    o.deathtouched = True
        s.touch()
        # Lifelink (CR 702.15b): the source's controller gains that much life. Damage one source
        # deals to several recipients at the same time is one life gain event (CR 119.9; e.g. the
        # Enduring Tenacity ruling: a trampling or double-blocked lifelinker triggers it once),
        # so inside a simultaneous batch (damage_batch_start) the gains are summed per source.
        if sc is not None and sc.has_kw("lifelink"):
            batch = getattr(self, "_lifelink_batch", None)
            if batch is not None:
                k = (sc.controller, source)
                batch[k] = batch.get(k, 0) + n
            else:
                self.gain_life(sc.controller, n, source=source)
        if sc is not None and getattr(sc, "controller", None) in (0, 1):
            # Damage dealt this turn by sources a player controlled as it was dealt, per color
            # and combat / noncombat (Temple of Power: "red sources you controlled dealt 4 or
            # more noncombat damage this turn"; ruling 2023-11-10: control at that time counts,
            # several sources add up). Key ("dealt", color or None for any, combat).
            st = s.players[sc.controller].turn_stats
            for col in sorted(getattr(sc, "colors", None) or ()) + [None]:
                k = ("dealt", col, bool(combat))
                st[k] = st.get(k, 0) + n
        ids = getattr(self, "_damage_ids", None)
        self.emit({"type": "damage", "source": source, "target": target, "n": n,
                   "combat": combat, "source_chars": sc,
                   "source_controller": sc.controller if sc else None,
                   "batch": ids[-1] if ids else self._new_damage_id()})
        dealt = getattr(self, "_dealt_batches", None)
        if dealt:
            cur = dealt[-1]
            m, _, _ = cur.get(source, (0, sc, combat))
            cur[source] = (m + n, sc, combat)
        else:
            self._emit_source_dealt(source, n, sc, combat)
        return n
