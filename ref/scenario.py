"""scenario/v0 loading and state/v0 output (interfaces/scenario-and-state.md)."""
import copy

from .engine.objects import State, Obj, StackItem, Combat
from .engine.game import Game
from .engine.rules import Rules
from .engine.mana import Unit
from .cards.model import DB
from . import Unsupported

STEP_NAMES = ["Untap", "Upkeep", "Draw", "Main1", "BeginCombat", "DeclareAttackers",
              "DeclareBlockers", "FirstStrikeDamage", "CombatDamage", "EndCombat", "Main2", "End",
              "Cleanup"]


def _entry(e):
    if isinstance(e, str):
        return {"name": e}
    return dict(e)


def _may_look(e, zone):
    """A face-down exile entry's `may_look` (scenario/v0 §1.2): the players who may look at the
    card (CR 406.3), a list of player indexes."""
    v = e["may_look"]
    if zone != "exile" or not e.get("face_down"):
        raise ValueError(f"{e.get('key') or e['name']}: may_look is for face-down exile entries only")
    if not isinstance(v, list) or any(p not in (0, 1) or isinstance(p, bool) for p in v):
        raise ValueError(f"{e.get('key') or e['name']}: may_look must be a list of player indexes (0, 1)")
    return sorted(set(v))


class Loader:
    def __init__(self, scn):
        self.scn = scn
        self.s = State(scn.get("seed", 0))
        self.keys = {}
        self.auto = {}
        self.may_look = {}  # face-down exiled object id -> players who may look (406.3)

    def key_for(self, name, given):
        if given:
            return given
        n = self.auto.get(name, 0) + 1
        self.auto[name] = n
        return f"{name}#{n}"

    def new_obj(self, e, owner, zone):
        s = self.s
        name = e["name"]
        if e.get("token"):
            from .cards.effects_text import PREDEF, predefined_token
            from .cards.model import token_def
            base = name[:-6] if name.endswith(" Token") else name
            td = e.get("token_def")
            if td is not None:
                # CR 111.4: a token's characteristics are those the creating effect gives it.
                name = token_def(base, td.get("types", ["Creature"]), td.get("subtypes", []),
                                 td.get("colors", []), td.get("power"), td.get("toughness"),
                                 td.get("text", ""), named=e["name"]).name
            elif base in PREDEF:
                name = predefined_token(base)().name
        DB.get(name)  # unknown names fail here
        o = Obj(s.new_id(), name, owner, zone)
        o.key = self.key_for(name, e.get("key"))
        if o.key in self.keys:
            raise ValueError(f"duplicate key {o.key}")
        self.keys[o.key] = o.id
        o.timestamp = s.new_ts()
        o.token = bool(e.get("token", False))
        if o.token:
            card = DB.get(name)
            if not card.is_token:
                # A token copy of a real card (or a predefined token card such as Treasure).
                pass
        o.face = int(e.get("face", 0))
        o.face_down = bool(e.get("face_down", False))
        o.face_down_kind = e.get("face_down_as")
        if e.get("counters"):
            o.counters = {k: v for k, v in e["counters"].items() if v}
        s.objs[o.id] = o
        return o

    def load(self):
        scn = self.scn
        s = self.s
        if scn.get("format") != "scenario/v0":
            raise Unsupported(f"format {scn.get('format')!r}")
        s.turn = int(scn.get("turn", 2))
        s.active = int(scn.get("active", 0))
        s.step = scn.get("step", "Main1")
        if s.step not in STEP_NAMES:
            raise Unsupported(f"step {s.step!r}")
        s.priority = int(scn.get("priority", s.active))
        s.first_player = s.active if s.turn % 2 == 1 else 1 - s.active
        rules = scn.get("rules_options") or {}
        if scn.get("died_this_turn"):
            # §1.1 (v2): creatures that died this turn as they last existed; implies the count.
            died = list(scn["died_this_turn"])
            s.turn_flags["creatures_died"] = len(died)
            s.turn_flags["died_lki"] = died
        elif scn.get("creatures_died_this_turn"):
            s.turn_flags["creatures_died"] = int(scn["creatures_died_this_turn"])  # §1.1
        if scn.get("descended_this_turn"):
            s.turn_flags["descended"] = bool(scn["descended_this_turn"])
        s.mana_at_priority = bool(rules.get("mana_abilities_at_priority", False))
        players = scn.get("players", [{}, {}])
        for p, pd in enumerate(players):
            pl = s.players[p]
            pl.life = int(pd.get("life", 20))
            pl.poison = int(pd.get("poison", 0))
            pl.counters = dict(pd.get("counters", {}))
            if pl.poison:
                pl.counters["poison"] = pl.poison
            pl.lands_played = int(pd.get("lands_played_this_turn", 0))
            pl.speed = int(pd.get("speed", 0))
            pl.mulligans = int(pd.get("mulligans", 0))
            pl.spells_cast = list(pd.get("spells_cast_this_turn", []))
            nc = sum(1 for n in pl.spells_cast if "Creature" not in DB.get(n).faces[0].types)
            if nc:
                pl.turn_stats["noncreature_cast"] = nc  # as casting.cast counts them
            pl.life_lost = int(pd.get("life_lost_this_turn", 0))
            pl.life_gained = int(pd.get("life_gained_this_turn", 0))
            for zname in ("library", "hand", "graveyard", "exile"):
                for e in pd.get(zname, []):
                    e = _entry(e)
                    o = self.new_obj(e, p, zname)
                    pl.zone(zname).append(o.id)
                    if "may_look" in e:
                        self.may_look[o.id] = _may_look(e, zname)
            for sym in pd.get("mana_pool", []):
                pl.pool.append(Unit(sym))
        # Battlefield, in timestamp order.
        bf = [_entry(e) for e in scn.get("battlefield", [])]
        made = []
        for e in bf:
            ctrl = int(e["controller"])
            owner = int(e.get("owner", ctrl))
            o = self.new_obj(e, owner, "battlefield")
            o.controller = ctrl
            o.tapped = bool(e.get("tapped", False))
            o.sick = bool(e.get("summoning_sick", False))
            o.damage = int(e.get("damage", 0))
            o.chosen = dict(e.get("chosen") or {})
            o.entered_turn = s.turn - 1 if not o.sick else s.turn
            s.battlefield.append(o.id)
            made.append((o, e))
        r = Rules(s)
        for o, e in made:
            c = r.chars(o.id)
            if "Planeswalker" in c.types and "loyalty" not in o.counters and "loyalty" not in (e.get("counters") or {}):
                # An explicit "loyalty": 0 stays 0 (format §1.3: only a walker without `loyalty`
                # gets its printed loyalty; Sanctum Lurker keeps such a walker, 704.5i).
                face = DB.get(o.card).faces[o.face]
                if isinstance(face.loyalty, int):
                    o.counters["loyalty"] = face.loyalty  # 306.5b
            if "Saga" in c.subtypes and "lore" not in o.counters and not e.get("counters"):
                pass
            if e.get("attached_to"):
                t = e["attached_to"]
                o.attached_to = ("player", int(t[1])) if t in ("p0", "p1") else self.keys[t]
        s.touch()
        # Linked exiled cards (CR 607, 610.3).
        for o, e in made:
            ex = e.get("exiled_cards") or []
            if not ex:
                continue
            ids = [self.keys[k.replace("key:", "")] for k in ex]
            s.linked.setdefault((o.id, "exiled"), []).extend(ids)
            text = DB.get(o.card).faces[o.face].text
            if "until" in text and "leaves the battlefield" in text:
                s.until_leaves.append({"host": o.id, "objs": ids, "zone": "exile",
                                       "back": "battlefield"})
        for p, pd in enumerate(players):
            for e in pd.get("exile", []):
                e = _entry(e)
                if e.get("exiled_with"):
                    host = self.keys[e["exiled_with"].replace("key:", "")]
                    oid = self.keys[e["key"]] if e.get("key") else None
                    if oid is not None:
                        s.linked.setdefault((host, "exiled"), []).append(oid)
                        text = DB.get(s.objs[host].card).faces[s.objs[host].face].text
                        if "until" in text and "leaves the battlefield" in text:
                            rec = next((x for x in s.until_leaves if x["host"] == host), None)
                            if rec is None:
                                s.until_leaves.append({"host": host, "objs": [oid], "zone": "exile", "back": "battlefield"})
                            elif oid not in rec["objs"]:
                                rec["objs"].append(oid)
        # Combat.
        if s.step in ("BeginCombat", "DeclareAttackers", "DeclareBlockers", "FirstStrikeDamage",
                      "CombatDamage", "EndCombat"):
            s.combat = Combat()
            pp = scn.get("phase_point", "priority")
            idx = STEP_NAMES.index(s.step)
            if idx > STEP_NAMES.index("DeclareAttackers") or (s.step == "DeclareAttackers" and pp != "start"):
                s.combat.declared = True
            if idx > STEP_NAMES.index("DeclareBlockers") or (s.step == "DeclareBlockers" and pp != "start"):
                s.combat.blockers_declared = True
            for o, e in made:
                if e.get("attacking"):
                    a = e["attacking"]
                    s.combat.attackers[o.id] = ("player", int(a[1])) if a in ("p0", "p1") else self.keys[a.replace("key:", "")]
            for o, e in made:
                if e.get("blocking"):
                    bl = e["blocking"] if isinstance(e["blocking"], list) else [e["blocking"]]
                    atts = [self.keys[k.replace("key:", "")] for k in bl]
                    s.combat.blockers[o.id] = atts
                    for a in atts:
                        s.combat.blocked.add(a)
            for o, e in made:
                if e.get("blocked"):
                    s.combat.blocked.add(o.id)
            if s.step in ("CombatDamage", "EndCombat") and pp != "start":
                pass
            if s.step == "CombatDamage" and any(
                    r.chars(x).has_kw("first strike") or r.chars(x).has_kw("double strike")
                    for x in list(s.combat.attackers) + list(s.combat.blockers)):
                s.combat.first_strike_step = True
                s.combat.dealt_first = {x for x in list(s.combat.attackers) + list(s.combat.blockers)
                                        if r.chars(x).has_kw("first strike") or r.chars(x).has_kw("double strike")}
        # Stack (bottom first).
        for e in scn.get("stack", []):
            self.load_stack_entry(e, r)
        pp = scn.get("phase_point", "priority")
        if pp == "priority":
            s.point = ("priority",)
        elif pp == "start":
            s.point = ("step",)
        elif pp == "mulligan":
            s.point = ("start",)
            s.mulligan_done = False
        else:
            raise Unsupported(f"phase_point {pp!r}")
        s.touch()
        s.know.init_from_state(s, self.may_look)  # who knows which card in the loaded position (knowledge.py)
        return s

    def load_stack_entry(self, e, r):
        s = self.s
        kind = e.get("kind", "spell")
        ctrl = int(e["controller"])
        it = StackItem(kind, ctrl, s.new_id())
        targets = [self._ref(t) for t in e.get("targets", [])]
        if kind == "spell":
            o = self.new_obj(e, int(e.get("owner", ctrl)), "stack")
            o.controller = ctrl
            o.x = int(e.get("x", 0))
            it.obj = o.id
            it.key = o.key
            modes = [m - 1 for m in e.get("mode", [])]
            o.modes = modes
            it.ctx = {"source": o.id, "controller": ctrl, "x": o.x, "modes": modes, "kicked": int(e.get("kicked", 0)),
                      "item": it.id, "how": "normal"}
            s.stack.append(it)
            s.touch()
            face = DB.get(o.card).faces[o.face]
            sa = next((a for a in face.abilities() if a.kind == "spell"), None)
            it.targets = self._group(targets, r.target_specs(sa, it.ctx) if sa else [], face)
        else:
            src = self.keys[e["source"].replace("key:", "")]
            it.source = src
            c = r.chars(src)
            it.source_lki = c
            n = int(e.get("ability", 1))
            want = "triggered" if kind == "triggered" else "activated"
            abil = [a for a in c.abilities if a.kind == want and not (want == "activated" and a.is_mana)]
            if n > len(abil):
                raise Unsupported(f"{e['source']} has no {want} ability #{n}")
            it.ability = abil[n - 1]
            it.key = f"{kind}:{s.objs[src].key}:{n}"
            it.ctx = {"source": src, "controller": ctrl, "x": int(e.get("x", 0)), "item": it.id,
                      "source_chars": c, "event": {}, "data": dict(e.get("data", {}))}
            if e.get("mode"):
                it.ctx["modes"] = [m - 1 for m in e["mode"]]
            s.stack.append(it)
            it.targets = self._group(targets, r.target_specs(it.ability, it.ctx), None)
        it.ctx["targets"] = it.targets
        if e.get("key"):
            self.keys.setdefault(e["key"], it.obj if it.obj else ("stack", it.id))

    def _group(self, targets, specs, face):
        if not targets:
            return []
        if face is not None and "Aura" in face.subtypes:
            return [targets]
        if len(specs) <= 1:
            return [targets]
        # One target per target word, in order (mostly true for the corpus).
        groups = []
        i = 0
        for sp in specs:
            k = sp.n[1] if isinstance(sp.n[1], int) else 1
            k = min(k, len(targets) - i) if sp is not specs[-1] else len(targets) - i
            groups.append(targets[i:i + max(k, 0)])
            i += max(k, 0)
        return groups

    def _ref(self, t):
        if t in ("p0", "p1"):
            return ("player", int(t[1]))
        if t.startswith("stack:"):
            # A preset entry targeting an earlier ability entry: "stack:N", N its 1-based position
            # from the bottom (format §1.3; spells are targeted by key).
            n = int(t[len("stack:"):])
            if not 1 <= n <= len(self.s.stack) or self.s.stack[n - 1].kind == "spell":
                raise ValueError(f"preset target {t!r}: no ability entry at that position (spells by key)")
            return ("stack", self.s.stack[n - 1].id)
        k = t.replace("key:", "")
        return self.keys[k]


def load_scenario(scn, hooks=None, step_cap=20000):
    s = Loader(scn).load()
    return Game(s, hooks=hooks, step_cap=step_cap)


# =========================================================================== canonical state

def find_ref(r, ref):
    """Resolve a canonical ref to an object id (any zone), ('player', p), or None."""
    s = r.s
    if ref in ("p0", "p1"):
        return ("player", int(ref[1]))
    if ref.startswith("stack:"):
        # An ability on the stack by its position from the bottom (format §2); None once gone.
        n = ref[len("stack:"):]
        if n.isdigit() and 1 <= int(n) <= len(s.stack) and s.stack[int(n) - 1].kind != "spell":
            return ("stack", s.stack[int(n) - 1].id)
        return None
    if ref.startswith("key:") or ref.startswith("new:"):
        k = ref[4:] if ref.startswith("key:") else ref
        for oid, o in s.objs.items():
            if o.key == k:
                return oid
        return None
    if ref.startswith("name:"):
        n = ref[5:]
        found = [oid for oid, o in s.objs.items() if o.zone != "library" and r.name_of(oid) == n]
        if len(found) > 1:
            raise ValueError(f"ambiguous {ref}")
        return found[0] if found else None
    raise ValueError(f"bad ref {ref!r}")


def obj_state(r, oid):
    s = r.s
    o = s.objs[oid]
    c = r.chars(oid)
    comb = s.combat
    att = None
    blk = []
    if comb is not None:
        if oid in comb.attackers:
            d = comb.attackers[oid]
            att = r.ref(d) if d is not None else "none"
        if oid in comb.blockers:
            blk = sorted(r.ref(a) for a in comb.blockers[oid])
    exiled_with = None
    for (host, tag), ids in s.linked.items():
        if oid in ids:
            exiled_with = r.ref(host) if host in s.objs else "gone"
    return {"zone": o.zone, "name": c.name, "controller": c.controller if o.zone in ("battlefield", "stack") else o.owner,
            "owner": o.owner, "tapped": o.tapped, "sick": bool(o.sick and "Creature" in c.types),
            "damage": o.damage, "counters": dict(sorted(o.counters.items())),
            "power": c.power, "toughness": c.toughness, "types": sorted(c.types),
            "subtypes": sorted(c.subtypes), "keywords": c.keywords(), "colors": sorted(c.colors),
            "attached_to": r.ref(o.attached_to) if o.attached_to is not None else None,
            "face_down": o.face_down, "token": o.token, "exiled_with": exiled_with,
            "attacking": att, "blocking": blk, "key": o.key}


def canonical_state(game):
    """state/v0 of the game's current (in-progress) state."""
    import json
    r = Rules(game.view)
    s = r.s
    out = {"format": "state/v0", "turn": s.turn, "active": s.active, "step": s.step,
           "priority": s.priority, "players": [], "objects": [], "stack": []}
    for pl in s.players:
        out["players"].append({
            "life": pl.life, "poison": pl.poison,
            "hand": sorted(r.name_of(o) for o in pl.hand),
            "library_count": len(pl.library),
            "graveyard": [r.name_of(o) for o in pl.graveyard],
            "exile": sorted(r.name_of(o) for o in pl.exile),
            "mana_pool": sorted(u.color for u in pl.pool),
            "counters": dict(sorted(pl.counters.items())),
            "speed": pl.speed})
    objs = []
    # Spells on the stack are objects too (zone "stack"; format §5), abilities are not.
    spells = [it.obj for it in s.stack if it.kind == "spell" and it.obj is not None]
    for oid in list(s.battlefield) + spells + [o for pl in s.players for o in pl.graveyard + pl.exile]:
        objs.append(obj_state(r, oid))
    objs.sort(key=lambda d: (d["zone"], d["controller"], d["name"] or "", json.dumps(d, sort_keys=True)))
    out["objects"] = objs
    for it in reversed(s.stack):
        src = it.obj if it.kind == "spell" else it.source
        out["stack"].append({"source": r.ref(src) if src is not None else None, "kind": it.kind,
                             "controller": it.controller,
                             "targets": [r.ref(t) for g in it.targets for t in g]})  # stack:N for abilities
    if game.pending is not None:
        out["pending"] = {"player": game.pending.player, "kind": game.pending.kind,
                          "options": game.pending.canons()}
    if s.result is not None:
        out["result"] = s.result
    return out
