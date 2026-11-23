"""Game objects, players and the game state (plain data, cheap to deepcopy).

Object identity follows CR 400.7: an object that moves from one zone to another becomes a new
object with no memory of its previous existence. Here that means a new `id`. The scenario
`key` names the *card* and follows it across zones (for tests and reports), and is never used
by the rules.
"""
import random


class Chars:
    """Characteristics after continuous effects (CR 109.3, 613)."""
    __slots__ = ("name", "mana_cost", "colors", "types", "subtypes", "supertypes", "power",
                 "toughness", "loyalty", "defense", "abilities", "controller", "face",
                 "text_name", "prepare", "mv_front", "room")

    def __init__(self):
        self.prepare = None       # card whose face 1 is this object's prepare spell (CR 722.2b)
        # : (Room card name, halves whose name/cost/text these values hold):
        # the existence of each half of a Room is a copiable value (CR 709.5, 709.5b).
        self.room = None
        # mana_cost is the front face's, for the mana value of a transformed permanent or spell
        # only (CR 712.8c, 712.8e); not a copiable value: a copy of a back face has mana value 0
        # (202.3b), so the copiable path drops it (Layers.base_chars).
        self.mv_front = False
        self.name = None
        self.mana_cost = []
        self.colors = set()
        self.types = set()
        self.subtypes = set()
        self.supertypes = set()
        self.power = None
        self.toughness = None
        self.loyalty = None
        self.defense = None
        self.abilities = []
        self.controller = None
        self.face = None

    def copy(self):
        c = Chars()
        c.name = self.name
        c.mana_cost = list(self.mana_cost)
        c.colors = set(self.colors)
        c.types = set(self.types)
        c.subtypes = set(self.subtypes)
        c.supertypes = set(self.supertypes)
        c.power = self.power
        c.toughness = self.toughness
        c.loyalty = self.loyalty
        c.defense = self.defense
        c.abilities = list(self.abilities)
        c.controller = self.controller
        c.face = self.face
        c.prepare = self.prepare
        c.mv_front = self.mv_front
        c.room = self.room
        return c

    def __deepcopy__(self, memo):
        return self.copy()

    def is_(self, t):
        return t in self.types

    def has_kw(self, kw):
        for a in self.abilities:
            if getattr(a, "keyword", None) == kw:
                return True
        return False

    def kw(self, kw):
        """The first keyword ability named kw (for parameterized keywords like ward)."""
        for a in self.abilities:
            if getattr(a, "keyword", None) == kw:
                return a
        return None

    def keywords(self):
        return sorted({a.keyword for a in self.abilities if getattr(a, "keyword", None)})

    @property
    def mana_value(self):
        from .mana import mana_value
        return mana_value(self.mana_cost)

    def __repr__(self):
        return f"Chars({self.name} {self.power}/{self.toughness} {sorted(self.types)})"


class Obj:
    """A card, token, copy of a card, or spell (CR 109.1)."""
    __slots__ = ("id", "card", "owner", "controller", "zone", "face", "face_down", "tapped",
                 "counters", "damage", "deathtouched", "attached_to", "token", "timestamp",
                 "key", "chosen", "sick", "cast", "x", "modes", "targets", "kicked",
                 "copy_of_spell", "is_copy", "entered_turn", "phased_out", "flags",
                 "face_down_kind", "mana_spent", "zone_ts", "cast_from", "counter_ts")

    def __init__(self, id, card, owner, zone):
        self.id = id
        self.card = card          # CardDef name (str)
        self.owner = owner
        self.controller = owner   # base controller (before layer 2)
        self.zone = zone
        self.face = 0
        self.face_down = False
        self.face_down_kind = None
        self.tapped = False
        self.counters = {}
        self.damage = 0
        self.deathtouched = False
        self.attached_to = None   # obj id, or ("player", p)
        self.token = False
        self.timestamp = 0
        self.key = None
        self.chosen = {}
        self.sick = True          # CR 302.6: not continuously controlled since turn start
        self.cast = None          # cast info dict for spells/permanents that were cast
        self.x = 0
        self.modes = []
        self.targets = []
        self.kicked = 0
        self.copy_of_spell = False
        self.is_copy = False
        self.entered_turn = None
        self.phased_out = False
        self.flags = {}
        self.mana_spent = {}
        self.zone_ts = 0
        self.cast_from = None
        self.counter_ts = {}      # counter kind -> timestamp of the last one put on (CR 613.7c)

    def __repr__(self):
        return f"Obj#{self.id}({self.card}@{self.zone})"


class StackItem:
    """An object on the stack: a spell (obj) or an ability (CR 113.3, 405)."""
    __slots__ = ("kind", "obj", "source", "source_lki", "ability", "controller", "targets",
                 "ctx", "id", "key", "uncounterable")

    def __init__(self, kind, controller, id):
        self.kind = kind          # 'spell' | 'activated' | 'triggered'
        self.obj = None           # spell's Obj id
        self.source = None        # source obj id (abilities)
        self.source_lki = None    # Chars of the source when the ability was put on the stack
        self.ability = None       # ability object
        self.controller = controller
        self.targets = []         # list of target groups: list[list[ref]]
        self.ctx = {}
        self.id = id
        self.key = None
        self.uncounterable = False

    def __repr__(self):
        return f"Stack#{self.id}({self.kind} {self.ability!r})"


class Player:
    __slots__ = ("index", "life", "poison", "counters", "library", "hand", "graveyard", "exile",
                 "pool", "lands_played", "drew_from_empty", "lost", "mulligans", "kept",
                 "speed", "land_plays", "spells_cast", "life_lost", "life_gained", "won",
                 "max_hand", "flags", "turn_stats")

    def __init__(self, index):
        self.index = index
        self.life = 20
        self.poison = 0
        self.counters = {}
        self.library = []   # obj ids, top first
        self.hand = []
        self.graveyard = []  # bottom first
        self.exile = []
        self.pool = []      # mana Units
        self.lands_played = 0
        self.land_plays = 1
        self.drew_from_empty = False
        self.lost = False
        self.won = False
        self.mulligans = 0
        self.kept = False
        self.speed = 0
        self.spells_cast = []     # names cast this turn
        self.life_lost = 0
        self.life_gained = 0
        self.max_hand = 7
        self.flags = {}
        self.turn_stats = {}

    def zone(self, z):
        return {"library": self.library, "hand": self.hand, "graveyard": self.graveyard,
                "exile": self.exile}[z]


class Combat:
    __slots__ = ("attackers", "blockers", "blocked", "first_strike_step", "dealt_first",
                 "declared", "blockers_declared", "removed")

    def __init__(self):
        self.attackers = {}   # attacker id -> ('player', p) | obj id (planeswalker/battle)
        self.blockers = {}    # blocker id -> [attacker ids]
        self.blocked = set()  # attacker ids that are blocked (CR 509.1h)
        self.first_strike_step = False
        self.dealt_first = set()  # creatures that dealt damage in the first-strike step
        self.declared = False
        self.blockers_declared = False
        self.removed = set()


class ContEffect:
    """A continuous effect from a resolved spell/ability (CR 611.2) or a rule effect.
    `parts` maps layer -> function(g, obj, chars); `affected` is a locked list of obj ids
    (CR 611.2c) or None with `filter` for effects that apply to a changing set."""
    __slots__ = ("id", "source", "controller", "timestamp", "duration", "parts", "affected",
                 "filter", "kind", "data", "until")

    def __init__(self, id):
        self.id = id
        self.source = None
        self.controller = None
        self.timestamp = 0
        self.duration = "eot"
        self.parts = {}
        self.affected = None
        self.filter = None
        self.kind = "layer"
        self.data = {}
        self.until = None


class State:
    def __init__(self, seed=0):
        self.players = [Player(0), Player(1)]
        self.objs = {}
        self.battlefield = []     # obj ids
        self.stack = []           # StackItems, bottom first
        self.resolving = None     # id of the StackItem resolving now (CR 608.2n, 608.3a): for views only, no rule reads it
        self.command = []
        self.turn = 1
        self.active = 0
        self.step = "Untap"
        self.priority = 0
        self.passes = 0
        self.next_id = 1
        self.next_ts = 1
        self.effects = []         # ContEffects
        self.delayed = []         # delayed triggered abilities (CR 603.7)
        self.pending = []         # triggered abilities waiting to be put on the stack
        self.events = []          # events not yet checked for triggers
        self.combat = None
        self.rng = random.Random(seed)
        self.result = None        # None | {"winner": p} | {"draw": True}
        self.point = ("start",)   # where the main loop resumes (see game.py)
        self.log = []
        self.key_counts = {}
        self.turn_flags = {}      # per-turn history (creatures died, etc.)
        self.linked = {}          # (source obj id, link name) -> [obj ids] (CR 607)
        self.until_leaves = []    # [(host id, [exiled ids], return info)] CR 610.3
        self.first_player = 0
        self.extra_turns = []
        self.steps_done = 0
        self.mulligan_done = True
        self.lki = {}             # obj id -> Chars at the moment it left its zone
        self.lki_obj = {}         # obj id -> Obj copy at the moment it left
        self.lki_spell = {}       # obj id of a spell -> its StackItem as it left the stack (608.2h)
        self.state_triggers_on_stack = set()
        self.skip_draw_first = True
        self.chars_cache = None
        self.monarch = None
        self.day_night = None
        from .knowledge import Knowledge
        self.know = Knowledge()   # who knows which hidden card (knowledge.py; no rule reads it)

    def new_id(self):
        i = self.next_id
        self.next_id += 1
        return i

    def new_ts(self):
        t = self.next_ts
        self.next_ts += 1
        return t

    def touch(self):
        self.chars_cache = None

    def opp(self, p):
        return 1 - p


# ============================================================================ fast copies
# Game states are copied on every submit (game.py). Immutable data (card IR, recorded events,
# last-known characteristics) is shared; everything mutable is copied.

def _obj_clone(o):
    n = Obj.__new__(Obj)
    for k in Obj.__slots__:
        setattr(n, k, getattr(o, k))
    n.counters = dict(o.counters)
    n.chosen = dict(o.chosen)
    n.flags = dict(o.flags)
    n.targets = list(o.targets)
    n.modes = list(o.modes)
    n.mana_spent = dict(o.mana_spent) if o.mana_spent else {}
    n.counter_ts = dict(o.counter_ts) if o.counter_ts else {}
    return n


def _ctx_clone(ctx):
    n = dict(ctx)
    if "vars" in n:
        n["vars"] = {k: (list(v) if isinstance(v, list) else v) for k, v in n["vars"].items()}
    for k in ("targets", "legal", "mode_targets"):
        if isinstance(n.get(k), list):
            n[k] = [list(g) if isinstance(g, list) else g for g in n[k]]
    if isinstance(n.get("modes"), list):
        n["modes"] = list(n["modes"])
    if isinstance(n.get("paid"), dict):
        n["paid"] = dict(n["paid"])
    if isinstance(n.get("paid_additional"), list):
        n["paid_additional"] = list(n["paid_additional"])
    if isinstance(n.get("data"), dict):
        n["data"] = dict(n["data"])
    return n


def _item_clone(it):
    n = StackItem.__new__(StackItem)
    for k in StackItem.__slots__:
        setattr(n, k, getattr(it, k))
    n.targets = [list(g) for g in it.targets]
    n.ctx = _ctx_clone(it.ctx)
    return n


def _player_clone(p):
    n = Player.__new__(Player)
    for k in Player.__slots__:
        setattr(n, k, getattr(p, k))
    for k in ("library", "hand", "graveyard", "exile", "spells_cast"):
        setattr(n, k, list(getattr(p, k)))
    n.pool = [u.__deepcopy__(None) for u in p.pool]
    n.counters = dict(p.counters)
    n.flags = dict(p.flags)
    n.turn_stats = dict(p.turn_stats)
    return n


def _ce_clone(ce):
    n = ContEffect.__new__(ContEffect)
    for k in ContEffect.__slots__:
        setattr(n, k, getattr(ce, k))
    n.parts = dict(ce.parts)
    n.affected = list(ce.affected) if ce.affected is not None else None
    n.data = dict(ce.data)
    return n


def _combat_clone(c):
    n = Combat.__new__(Combat)
    for k in Combat.__slots__:
        setattr(n, k, getattr(c, k))
    n.attackers = dict(c.attackers)
    n.blockers = {k: list(v) for k, v in c.blockers.items()}
    n.blocked = set(c.blocked)
    n.dealt_first = set(c.dealt_first)
    n.removed = set(c.removed)
    return n


def _state_deepcopy(self, memo):
    import random as _random
    n = State.__new__(State)
    d = dict(self.__dict__)
    n.__dict__ = d
    d["players"] = [_player_clone(p) for p in self.players]
    d["objs"] = {k: _obj_clone(o) for k, o in self.objs.items()}
    d["battlefield"] = list(self.battlefield)
    d["stack"] = [_item_clone(it) for it in self.stack]
    d["command"] = list(self.command)
    d["effects"] = [_ce_clone(ce) for ce in self.effects]
    d["delayed"] = [dict(x, data=dict(x.get("data") or {})) for x in self.delayed]
    d["pending"] = [_pending_clone(t) for t in self.pending]
    d["events"] = []
    d["combat"] = _combat_clone(self.combat) if self.combat is not None else None
    rng = _random.Random()
    rng.setstate(self.rng.getstate())
    d["rng"] = rng
    d["log"] = []
    d["key_counts"] = dict(self.key_counts)
    d["turn_flags"] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.turn_flags.items()}
    d["linked"] = {k: list(v) for k, v in self.linked.items()}
    d["until_leaves"] = [dict(r, objs=list(r["objs"])) for r in self.until_leaves]
    d["lki"] = dict(self.lki)
    d["lki_obj"] = dict(self.lki_obj)
    d["lki_spell"] = dict(getattr(self, "lki_spell", None) or {})  # items are never changed once off the stack
    d["state_triggers_on_stack"] = set(self.state_triggers_on_stack)
    d["extra_turns"] = list(self.extra_turns)
    d["chars_cache"] = None
    d["rule_index"] = None  # combat._rule_index: tied to chars_cache
    d["trig_index"] = None  # triggers._trig_index: tied to chars_cache
    if getattr(self, "know", None) is not None:
        d["know"] = self.know.copy()
    # Harness library-alignment counters (harness/lockstep.py Hooks): mutated in place, so each
    # copy needs its own list (a shared one advances on every re-run of an action).
    for k in ("flags_draws", "flags_looks", "flags_explores", "flags_digs"):
        if k in d:
            d[k] = list(d[k])
    return n


def _pending_clone(t):
    from .triggers import PendingTrigger
    n = PendingTrigger.__new__(PendingTrigger)
    for k in PendingTrigger.__slots__:
        setattr(n, k, getattr(t, k))
    n.data = dict(t.data) if isinstance(t.data, dict) else t.data
    return n


State.__deepcopy__ = _state_deepcopy
