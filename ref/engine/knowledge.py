"""Who knows what: each player's record of hidden information (interfaces/input-fuzzing.md, K).

Derived from the Comprehensive Rules and from this engine's own zone changes and events, never
from what the engine under test shows. Kept in the game state (`State.know`), so it is replayed,
copied and committed with the state (game.py re-runs units on scratch copies). No rule reads it.

Hidden information (CR 400.2): the cards in libraries (401.2) and hands (402.3), face-down
permanents and spells (708.5) and cards exiled face down (406.3). A *slot* is one such hidden
object, by its object id. What player p knows about the slots is a list of **bags**: a bag is a
set of slots S and a multiset of card names K with |K| <= |S|, meaning "the cards of S include the
cards K" (p knows those cards are somewhere among S, not which slot is which). Bags of one player
are disjoint. Exact knowledge ("this object is Shock") is a bag ({x}, {Shock}). Slots in no bag are
unknown to p.

What is known without a bag (derived when asked, `known`):
- a player's own hand (402.3);
- face-down permanents and spells the player controls (708.5). The controller also gets a bag for
  it (it may look at it at any time; the stronger reading is that it did), so a player who loses
  control of a face-down permanent still knows it: the same object, perfect recall (EUT team
  answer Q3, 2026-11-08);
- cards exiled face down the player may look at (406.3: the permission sticks until the card
  leaves exile, `look`), e.g. "you may look at and play those cards" (flags["may_play"]).

How knowledge changes (perfect recall while the object can be identified):
- **public**: a card that moves to a public zone face up is seen by everyone (400.2); a card seen in
  a public zone that moves to a hidden one is still known to everyone (it moved in plain sight,
  603.10a: "an object that all players can see is put into a hand or library");
- **look / reveal**: a card looked at is seen by that player only (701.20e: scry 701.22a, surveil
  701.25a, "look at the top N", search 701.23a: every card of the searched zone, "look at target
  player's hand"); a revealed card is seen by every player (701.20a);
- **hidden moves** (draw, library -> hand, hand -> library, library -> face-down exile or
  battlefield): the move itself is seen, so a player who knew the card knows it in its new place
  (the object is identified by where it went: the top card of the library went to the hand);
- **reordering**: a shuffle (701.20d: revealed cards that are reordered "become new objects") or
  a random order (Dig "in a random order") ends position knowledge: every bag that touches the
  reordered cards merges into one; cards put on the library "in any order" (401.4: "doesn't reveal
  the order") do the same for every player but the one who chose;
- **scry** (701.22a): how many of the looked-at cards went to the bottom and how many stayed on
  top is public (EUT team answer Q2, 2026-11-08: the cards move in plain sight, only the order is
  hidden), so both players record the split (`splits`, shown in `view`); which of the cards went
  where is the scrying player's alone (401.4): for every other player the scried cards become one
  bag. Surveil (701.25a) needs no record: the cards it puts into the graveyard are public there
  (400.2), so the split is too; the kept cards' order is hidden like a scry's top group;
- **face-down**: a face-down permanent that leaves the battlefield is revealed (708.9); one turned
  face up is public (708.8).

Cases the CR doesn't settle are not decided here; where
the model must pick, it picks the *stronger* knowledge (more is known), which keeps hidden
perturbations sound (a perturbation the player could tell apart is never generated).

`learned[p]`: every card (scenario key -> name) p identified at some moment, also briefly (a card
revealed and shuffled away before the next decision): what p's own event log can still tell.
"""

PUBLIC = ("battlefield", "graveyard", "exile", "stack", "command")


def _hidden(o):
    """Is object o's identity hidden from some player (CR 400.2, 406.3, 708.5)?"""
    return o.zone in ("library", "hand") or bool(o.face_down)


def _minus(names, name):
    """Multiset `names` (sorted tuple) without one `name`, if present."""
    if name in names:
        i = names.index(name)
        return names[:i] + names[i + 1:]
    return names


class Knowledge:
    __slots__ = ("bags", "where", "learned", "look", "next_bag", "rng_uses", "lib_after_rng", "hand_since", "splits")

    def __init__(self):
        self.bags = ({}, {})      # p -> {bag id: (frozenset slots, sorted tuple of names)}
        self.where = ({}, {})     # p -> {slot: bag id}
        self.learned = ({}, {})   # p -> {key: name}
        self.look = (frozenset(), frozenset())  # p -> face-down exiled slots p may look at (406.3)
        self.next_bag = 1
        # Harness bookkeeping (no knowledge): library reorderings left to chance (shuffles,
        # "in a random order") so far, and library events since the first one (a card leaving a
        # library, a library card looked at or revealed). Until such an event, the outcome of the
        # random reordering is invisible to every player (harness/knowfuzz.py seed variants).
        self.rng_uses = 0
        self.lib_after_rng = 0
        # Turn each card in a hand entered it: a card being put into a hand is seen by everyone,
        # so which hidden hand card is the old one is public under the "moves are seen" reading.
        # Cards of a loaded
        # position: turn 0.
        self.hand_since = {}
        # Public scry splits, in order: (player, turn, cards kept on top, cards put on the bottom).
        # Both players saw them (CR 701.22a; EUT team answer Q2): part of what each one knows.
        self.splits = ()

    def copy(self):
        n = Knowledge.__new__(Knowledge)
        n.bags = (dict(self.bags[0]), dict(self.bags[1]))
        n.where = (dict(self.where[0]), dict(self.where[1]))
        n.learned = (dict(self.learned[0]), dict(self.learned[1]))
        n.look = self.look
        n.next_bag = self.next_bag
        n.rng_uses = self.rng_uses
        n.lib_after_rng = self.lib_after_rng
        n.hand_since = dict(self.hand_since)
        n.splits = self.splits
        return n

    # ------------------------------------------------------------------ bag primitives
    def _new_bag(self, p, slots, names):
        b = self.next_bag
        self.next_bag += 1
        self.bags[p][b] = (frozenset(slots), tuple(sorted(names)))
        for x in slots:
            self.where[p][x] = b
        return b

    def _leave(self, p, x, name):
        """Slot x leaves p's bag (it became public, or p now knows it exactly): the bag keeps the
        other slots, with one copy of x's name fewer if the bag counted it (it may have been that
        copy)."""
        b = self.where[p].pop(x, None)
        if b is None:
            return
        S, K = self.bags[p].pop(b)
        S = S - {x}
        K = _minus(K, name)
        if S:
            self.bags[p][b] = (S, K)
            # where[p] entries of the other slots still point at b

    def _exact(self, p, x, name, key):
        self._leave(p, x, name)
        self._new_bag(p, [x], [name])
        if key is not None:
            self.learned[p][key] = name

    def _transfer(self, old, new):
        for p in (0, 1):
            b = self.where[p].pop(old, None)
            if b is None:
                continue
            S, K = self.bags[p][b]
            if new is None:
                S = S - {old}
            else:
                S = (S - {old}) | {new}
                self.where[p][new] = b
            if S:
                self.bags[p][b] = (S, K[:len(S)] if len(K) > len(S) else K)
            else:
                del self.bags[p][b]
        if old in self.look[0] or old in self.look[1]:
            self.look = tuple((lk - {old}) | ({new} if (new is not None and old in lk) else set()) for lk in self.look)

    def merge(self, p, slots):
        """p can no longer tell the slots apart (shuffle, random order): every bag of p that
        touches them becomes one bag over all of them."""
        slots = set(slots)
        names = []
        touched = {self.where[p][x] for x in slots if x in self.where[p]}
        if not touched:
            return
        allslots = set(slots)
        for b in touched:
            S, K = self.bags[p].pop(b)
            allslots |= S
            names.extend(K)
        for x in allslots:
            self.where[p].pop(x, None)
        self._new_bag(p, allslots, names)

    # ------------------------------------------------------------------ events
    def init_from_state(self, s, may_look=None):
        """A loaded position: every card in a public zone face up is known to both (400.2), every
        card in a hand to its owner (402.3), a face-down permanent to its controller (708.5), a
        face-down exiled card to the players who may look at it (406.3; `may_look`: {object id:
        [players]} from the scenario entries, else the default below)."""
        for o in s.objs.values():
            if o.face_down and o.zone in ("battlefield", "stack"):
                self._new_bag(o.controller, [o.id], [o.card])  # 708.5; kept if control changes
            if o.key is None:
                continue
            if not _hidden(o):
                for p in (0, 1):
                    self.learned[p][o.key] = o.card
            elif o.zone == "hand":
                self.learned[o.owner][o.key] = o.card
            elif o.face_down and o.zone in ("battlefield", "stack"):
                self.learned[o.controller][o.key] = o.card
        # A face-down exiled card of a loaded position: who may look at it depends on what exiled
        # it (406.3: foretell its owner, 702.143a; hideaway the exiling permanent's controller,
        # 702.75a; some effects nobody). The entry's `may_look` says (scenario/v0 §1.2; EUT team
        # answer Q4, 2026-11-08). Without it, assume the stronger knowledge: its owner may look,
        # and so may the controller of the permanent it is exiled with.
        may_look = may_look or {}
        for pl in s.players:
            for x in pl.exile:
                o = s.objs[x]
                if not o.face_down:
                    continue
                if x in may_look:
                    lookers = set(may_look[x])
                else:
                    lookers = {o.owner}
                    for (host, _tag), ids in s.linked.items():
                        if x in ids and host in s.objs:
                            lookers.add(s.objs[host].controller)
                for p in lookers:
                    self.look = tuple((lk | {x}) if q == p else lk for q, lk in enumerate(self.look))
                    self._new_bag(p, [x], [o.card])
                    if o.key is not None:
                        self.learned[p][o.key] = o.card

    def _knows_obj(self, r, p, o):
        """Does p know hidden object o exactly (a bag, or without one: own hand 402.3, a face-down
        permanent or spell p controls 708.5, a face-down exiled card p may look at 406.3)?"""
        b = self.where[p].get(o.id)
        if b is not None:
            S, K = self.bags[p][b]
            if len(K) == len(S) and len(set(K)) == 1:
                return True
        if o.zone == "hand" and o.owner == p:
            return True
        if o.face_down and o.zone in ("battlefield", "stack") and r.ctrl(o.id) == p:
            return True
        return o.id in self.look[p]

    def moved(self, r, o, n):
        """Zone change of old object o into new object n (None: it ceased to exist), CR 400.7.
        Called by zones._do_move once n exists (o is no longer in s.objs; its last known
        information is)."""
        name = o.card
        if o.zone == "library" and self.rng_uses:
            self.lib_after_rng += 1
        self.hand_since.pop(o.id, None)
        if n is not None and n.zone == "hand":
            self.hand_since[n.id] = r.s.turn
        if not _hidden(o):
            # Seen by everyone before the move (400.2). Still known in a hidden zone: it moved in
            # plain sight (603.10a: "an object that all players can see is put into a hand or
            # library"); exiled face down from a public zone, the same (406.3 doesn't make players
            # forget a card they saw).
            if n is not None and _hidden(n):
                for p in (0, 1):
                    self._exact(p, n.id, name, n.key)
            return
        if n is None or not _hidden(n) or (o.face_down and o.zone == "battlefield"):
            # Becomes public (400.2), ceases to exist, or a face-down permanent leaves the
            # battlefield: its owner reveals it (708.9).
            for p in (0, 1):
                self._leave(p, o.id, name)
                if o.key is not None and n is not None:
                    self.learned[p][o.key] = name
            self._transfer(o.id, None)
            if n is not None and _hidden(n):
                for p in (0, 1):
                    self._exact(p, n.id, name, n.key)
            return
        # Hidden -> hidden (a draw, library -> hand, hand -> library, library -> face-down exile or
        # battlefield): the move is seen, so whoever knew the card knows it in its new place, and
        # partial knowledge follows it.
        knowers = [p for p in (0, 1) if self._knows_obj(r, p, o)]
        self._transfer(o.id, n.id)
        for p in knowers:
            self._exact(p, n.id, name, n.key)
        if n.zone == "hand" and n.key is not None:
            self.learned[n.owner][n.key] = name  # 402.3
        if n.face_down and n.zone == "battlefield":
            self._exact(n.controller, n.id, name, n.key)  # 708.5 (kept if control changes)

    def controls(self, r, p, slots):
        """p gained control of these permanents: p may look at the face-down ones (708.5), and
        keeps knowing them after control changes again (perfect recall)."""
        self.looks(r, p, [x for x in slots if x in r.s.objs and r.s.objs[x].face_down])

    def looks(self, r, p, slots):
        """Player p looks at these cards (701.20e)."""
        for x in slots:
            o = r.s.objs.get(x)
            if o is not None and _hidden(o):
                if o.zone == "library" and self.rng_uses:
                    self.lib_after_rng += 1
                self._exact(p, x, o.card, o.key)

    def reveal(self, r, slots):
        """The cards are revealed to every player (701.20a)."""
        for p in (0, 1):
            self.looks(r, p, slots)

    def may_look(self, r, p, slots):
        """p may look at these face-down exiled cards from now on (406.3)."""
        self.look = tuple((lk | set(slots)) if q == p else lk for q, lk in enumerate(self.look))
        self.looks(r, p, slots)

    def reorder(self, r, slots, chooser=None):
        """The cards were put in an order other players don't learn (401.4, any order: `chooser`
        knows it) or nobody learns (random order, shuffle: chooser None)."""
        if chooser is None and len(slots) > 1:
            self.rng_uses += 1
        for p in (0, 1):
            if p != chooser:
                self.merge(p, slots)

    def scried(self, r, p, top, bottom):
        """p scried (701.22a): the cards `top` went back on top and `bottom` to the bottom, each
        group in an order p chose. The number each way is public (both players record it); which
        card went where isn't (401.4: the order isn't revealed, and the cards are face down): every
        other player who knew some of them now knows only that they are among the scried cards."""
        self.splits = self.splits + ((p, r.s.turn, len(top), len(bottom)),)
        for q in (0, 1):
            if q != p:
                self.merge(q, list(top) + list(bottom))

    def shuffle(self, r, owner):
        """CR 701.20d: shuffled cards can't be told apart any more."""
        lib = r.s.players[owner].library
        if len(lib) > 1:
            self.rng_uses += 1
        for p in (0, 1):
            self.merge(p, lib)

    def event(self, r, ev):
        """Knowledge from an emitted event (zones.emit)."""
        t = ev.get("type")
        s = r.s
        if t == "reveal":
            if ev.get("obj") is not None:
                self.reveal(r, [ev["obj"]])
        elif t == "reveal_hand":
            hand = list(s.players[ev["player"]].hand)
            if ev.get("to") is None:
                self.reveal(r, hand)
            else:
                self.looks(r, ev["to"], hand)
        elif t == "search":
            # 701.23a: the searcher looks at every card of the searched library.
            self.looks(r, ev["player"], list(s.players[ev.get("library", ev["player"])].library))
        elif t == "beheld":
            o = s.objs.get(ev.get("obj"))
            if o is not None and o.zone == "hand":
                self.reveal(r, [o.id])  # 701.4a: "Reveal a [quality] card from your hand"
        elif t == "turned_face_up":
            o = s.objs.get(ev.get("obj"))
            if o is not None:
                for p in (0, 1):
                    self._leave(p, o.id, o.card)
                    if o.key is not None:
                        self.learned[p][o.key] = o.card

    def sync(self, r):
        """Permissions that live on objects: face-down exiled cards a player may look at and play
        (flags["may_play"]) are looked at as soon as the permission exists (406.3: it then sticks
        until the card leaves exile); a face-down permanent is looked at by its controller (708.5;
        for control changes without a `controls` call, e.g. an effect's duration ending)."""
        for x in r.s.battlefield:
            o = r.s.objs[x]
            if o.face_down:
                c = r.ctrl(x)
                b = self.where[c].get(x)
                if b is None or tuple(map(len, self.bags[c][b])) != (1, 1):
                    self.looks(r, c, [x])
        for p in (0, 1):
            for pl in r.s.players:
                for x in pl.exile:
                    o = r.s.objs[x]
                    if o.face_down and x not in self.look[p]:
                        mp = o.flags.get("may_play") or {}
                        if mp.get("player") == p:
                            self.may_look(r, p, [x])

    # ------------------------------------------------------------------ queries
    def known(self, r, p):
        """{slot: name} of every hidden object p knows exactly now."""
        self.sync(r)
        s = r.s
        out = {}
        for b, (S, K) in self.bags[p].items():
            if len(S) == 1 and len(K) == 1:
                out[next(iter(S))] = K[0]
            elif len(K) == len(S) and len(set(K)) == 1:
                for x in S:
                    out[x] = K[0]
        out.update({x: s.objs[x].card for x in s.players[p].hand})
        for x in s.battlefield:
            o = s.objs[x]
            if o.face_down and r.ctrl(x) == p:
                out[x] = o.card  # 708.5
        for it in s.stack:
            if it.kind == "spell" and it.obj in s.objs and s.objs[it.obj].face_down and it.controller == p:
                out[it.obj] = s.objs[it.obj].card
        for x in self.look[p]:
            if x in s.objs and s.objs[x].face_down:
                out[x] = s.objs[x].card
        return {x: n for x, n in out.items() if x in s.objs and _hidden(s.objs[x])}

    def partial(self, r, p):
        """p's bags that aren't exact knowledge: [(slots, names)]."""
        known = self.known(r, p)
        out = []
        for S, K in self.bags[p].values():
            S2 = frozenset(x for x in S if x not in known and x in r.s.objs)
            if S2 and K and not (len(S2) == len(K) and len(set(K)) == 1):
                out.append((S2, K))
        return out


def hidden_slots(s):
    """Every hidden object now: (owner, zone, index or None, object id)."""
    out = []
    for pl in s.players:
        for i, x in enumerate(pl.library):
            out.append((pl.index, "library", i, x))
        for x in pl.hand:
            out.append((pl.index, "hand", None, x))
        for x in pl.exile:
            if s.objs[x].face_down:
                out.append((pl.index, "exile", None, x))
    for x in s.battlefield:
        if s.objs[x].face_down:
            out.append((s.objs[x].owner, "battlefield", None, x))
    return out


def knows(r, p):
    """For player p at this moment (spec K): which hidden objects p knows, and which hidden facts
    are free to vary.

    Returns {"known": [{"key", "name", "zone", "owner", "pos"}], (pos: index from the top of the
    library when p knows it, else None), "partial": [{"keys", "names"}] (p knows these cards are
    among those slots), "unknown": [{"key", "zone", "owner"}] (identity free to vary, within its
    owner's cards and the bags), "library_order": {owner: [indexes whose card p can't place]},
    "learned": {key: name}, "scry_splits": [{"player", "turn", "top", "bottom"}] (public: every
    scry so far, how many cards each way)}."""
    s = r.s
    k = s.know
    known = k.known(r, p)
    out = {"known": [], "partial": [], "unknown": [], "library_order": {0: [], 1: []},
           "learned": dict(k.learned[p]),
           "scry_splits": [{"player": q, "turn": t, "top": a, "bottom": b} for q, t, a, b in k.splits]}
    for owner, zone, i, x in hidden_slots(s):
        o = s.objs[x]
        if x in known:
            # A library card p knows exactly is one p can place: reorderings make bags of several
            # cards (merge), so an exact card's position is known.
            pos = i if zone == "library" else None
            out["known"].append({"key": o.key, "name": known[x], "zone": zone, "owner": owner, "pos": pos})
        else:
            out["unknown"].append({"key": o.key, "zone": zone, "owner": owner})
            if zone == "library":
                out["library_order"][owner].append(i)
    for S, K in k.partial(r, p):
        out["partial"].append({"keys": sorted(s.objs[x].key or f"obj:{x}" for x in S), "names": list(K)})
    return out


def view(r, p, pending=None):
    """Everything player p may know now, canonical (scenario keys, not engine ids): the public
    state, the hidden cards p knows (with library positions where p knows them), p's partial
    knowledge, every card p ever identified, and p's own pending decision. Two positions with
    equal views are indistinguishable to p (the hidden-perturbation filter, harness/knowfuzz.py)."""
    import json
    from ..scenario import obj_state
    s = r.s
    k = s.know
    info = knows(r, p)
    players = []
    for pl in s.players:
        players.append({"life": pl.life, "poison": pl.poison, "hand_count": len(pl.hand),
                        "hand_entered": sorted(k.hand_since.get(x, 0) for x in pl.hand),
                        "library_count": len(pl.library),
                        "graveyard": [s.objs[o].key for o in pl.graveyard],
                        "exile_face_down": sum(1 for o in pl.exile if s.objs[o].face_down),
                        "mana_pool": sorted(u.color for u in pl.pool),
                        "counters": dict(sorted(pl.counters.items())), "speed": pl.speed,
                        "lands_played": pl.lands_played, "mulligans": pl.mulligans})
    objs = []
    spells = [it.obj for it in s.stack if it.kind == "spell" and it.obj is not None]
    for oid in list(s.battlefield) + spells + [o for pl in s.players for o in pl.graveyard + pl.exile]:
        d = obj_state(r, oid)
        if s.objs[oid].face_down:
            d["key"] = None  # which card it is: in `known` if p knows it
            d["face_down_as"] = s.objs[oid].face_down_kind
        objs.append(d)
    objs.sort(key=lambda d: json.dumps(d, sort_keys=True))
    stack = []
    for it in reversed(s.stack):
        src = it.obj if it.kind == "spell" else it.source
        stack.append({"source": r.ref(src) if src is not None else None, "kind": it.kind,
                      "controller": it.controller, "targets": [r.ref(t) for g in it.targets for t in g]})
    out = {"turn": s.turn, "active": s.active, "step": s.step, "players": players,
           "objects": objs, "stack": stack,
           "known": sorted(info["known"], key=lambda d: json.dumps(d, sort_keys=True)),
           "partial": sorted(info["partial"], key=lambda d: json.dumps(d, sort_keys=True)),
           "learned": sorted(info["learned"].items()),
           # Public, in order (not in harness/knowfuzz.py's `ref_pub` keys: a different split is
           # something p can tell apart, not a misalignment of the two runs).
           "scry_splits": info["scry_splits"]}
    if pending is not None and pending.player == p:
        out["decision"] = {"kind": pending.kind, "subject": pending.subject,
                           "options": sorted(pending.canons())}
    return out
