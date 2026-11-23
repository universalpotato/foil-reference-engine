"""Card definitions from Oracle data (CR 108, 200-series characteristics).

A CardDef is immutable game data: one or more faces, each with printed characteristics and the
Oracle text that the compiler (ref.cards.compiler) turns into abilities.
"""
import gzip
import json
import os
import re

from ..engine.mana import parse_cost, cost_colors

HERE = os.path.dirname(os.path.abspath(__file__))
SUBSET = os.path.join(os.path.dirname(HERE), "data", "oracle-subset.json.gz")
FULL = "/root/mtg-data/scryfall/oracle_cards.jsonl.gz"

CARD_TYPES = ["Artifact", "Battle", "Creature", "Enchantment", "Instant", "Kindred", "Land",
              "Planeswalker", "Sorcery", "Conspiracy", "Dungeon", "Phenomenon", "Plane", "Scheme",
              "Vanguard"]
SUPERTYPES = ["Basic", "Legendary", "Ongoing", "Snow", "World"]
BASIC_LAND_TYPES = {"Plains": "W", "Island": "U", "Swamp": "B", "Mountain": "R", "Forest": "G"}
COLOR_WORDS = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}


def parse_type_line(tl):
    """'Legendary Creature — Elf Druid' -> (supertypes, types, subtypes)."""
    tl = tl or ""
    left, _, right = tl.partition("—")
    sup, typ = [], []
    for w in left.split():
        if w in SUPERTYPES:
            sup.append(w)
        elif w in CARD_TYPES:
            typ.append(w)
        elif w == "Tribal":
            typ.append("Kindred")
        else:
            typ.append(w)  # unknown word: keep, compiler may reject
    subs = right.split() if right else []
    # Multi-word subtypes ("Time Lord") are rare; keep words.
    return tuple(sup), tuple(typ), tuple(subs)


def _pt(v):
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return v  # '*', '1+*': characteristic-defining, handled by abilities


class Face:
    __slots__ = ("name", "mana_cost", "mana_cost_str", "type_line", "supertypes", "types",
                 "subtypes", "colors", "power", "toughness", "loyalty", "defense", "text",
                 "index", "card", "_abilities", "_error", "color_indicator")

    def __init__(self, card, index, rec, colors_override=None):
        self.card = card
        self.index = index
        self.name = rec["name"]
        self.mana_cost_str = rec.get("mana_cost") or ""
        self.mana_cost = parse_cost(self.mana_cost_str)
        self.type_line = rec.get("type_line") or ""
        self.supertypes, self.types, self.subtypes = parse_type_line(self.type_line)
        ci = rec.get("color_indicator")
        self.color_indicator = tuple(ci) if ci else None
        if ci:
            self.colors = frozenset(ci)
        elif colors_override is not None:
            self.colors = frozenset(colors_override)
        else:
            self.colors = frozenset(cost_colors(self.mana_cost))  # CR 202.2
        self.power = _pt(rec.get("power"))
        self.toughness = _pt(rec.get("toughness"))
        lo = rec.get("loyalty")
        self.loyalty = int(lo) if lo not in (None, "X", "*") else lo
        de = rec.get("defense")
        self.defense = int(de) if de not in (None, "X", "*") else de
        self.text = rec.get("oracle_text") or ""
        self._abilities = None
        self._error = None

    def abilities(self):
        """Compiled abilities (raises Unsupported if any part of the text isn't supported)."""
        if self._abilities is None and self._error is None:
            from .compiler import compile_face
            from .. import Unsupported
            try:
                self._abilities = compile_face(self)
            except Unsupported as e:
                self._error = Unsupported(f"[{self.name}] {e}")
            except Exception as e:  # noqa: BLE001 - remember the reason, re-raise below
                self._error = e
        if self._error is not None:
            raise self._error
        return self._abilities

    def __repr__(self):
        return f"Face({self.name})"


class CardDef:
    """A card (or token definition). layout: normal, transform, modal_dfc, adventure, split,
    prepare, omen, saga, class, case, flip, token."""

    def __init__(self, rec):
        self.name = rec["name"]
        self.layout = rec.get("layout") or "normal"
        self.oracle_id = rec.get("oracle_id")
        self.rec = rec
        fcs = rec.get("card_faces")
        if fcs and self.layout not in ("normal", "saga", "class", "case"):
            self.faces = [Face(self, i, f) for i, f in enumerate(fcs)]
        else:
            self.faces = [Face(self, 0, rec, rec.get("colors"))]
        self.is_token = rec.get("is_token", False)

    @property
    def front(self):
        return self.faces[0]

    def __repr__(self):
        return f"CardDef({self.name})"


class CardDB:
    """Name -> CardDef. Looks names up in the local Oracle subset, then the full Oracle file."""

    def __init__(self):
        self._recs = None
        self._cards = {}
        self._full = None
        self.custom = {}  # token definitions etc. registered at runtime

    def _load(self):
        if self._recs is None:
            with gzip.open(SUBSET, "rt") as f:
                self._recs = json.load(f)
            self._front = {}
            for n in self._recs:
                # Art-series cards ("Elusive Otter // Elusive Otter", layout art_series) aren't game
                # cards: a front-face name must find the real card ("Elusive Otter // Grove's
                # Bounty"), never an empty art card that compiles to nothing.
                if " // " in n and self._recs[n].get("layout") not in ("art_series",):
                    self._front.setdefault(n.split(" // ")[0], n)

    def _load_full(self):
        if self._full is None:
            self._full = {}
            if os.path.exists(FULL):
                with gzip.open(FULL, "rt") as f:
                    rank = {"normal": 0, "transform": 0, "modal_dfc": 0, "adventure": 0, "split": 0,
                            "saga": 0, "class": 0, "case": 0, "prepare": 0, "flip": 0, "meld": 0,
                            "token": 1, "double_faced_token": 2}
                    for line in f:
                        c = json.loads(line)
                        r = rank.get(c.get("layout"), 9)
                        if r == 9:
                            continue  # art series, front cards, emblems, ...
                        old = self._full.get(c["name"])
                        if old is not None and rank.get(old.get("layout"), 9) <= r:
                            continue
                        self._full[c["name"]] = c

    def record(self, name):
        self._load()
        if name in self.custom:
            return self.custom[name]
        if name in self._recs:
            return self._recs[name]
        if name in self._front:
            return self._recs[self._front[name]]
        self._load_full()
        if name in self._full:
            return self._full[name]
        for n, c in self._full.items():
            if " // " in n and n.split(" // ")[0] == name:
                return c
        raise KeyError(f"unknown card name {name!r}")

    def get(self, name):
        if name not in self._cards:
            self._cards[name] = CardDef(self.record(name))
        return self._cards[name]

    def register(self, rec):
        """Register a synthetic definition (tokens: CR 111.4)."""
        self.custom[rec["name"]] = rec
        self._cards.pop(rec["name"], None)
        return self.get(rec["name"])

    def names(self):
        self._load()
        return list(self._recs)


DB = CardDB()


def token_def(name, types, subtypes=(), colors=(), power=None, toughness=None, text="",
              supertypes=(), named=None):
    """Define a token's characteristics. CR 111.4: unless the effect names it, its name is its
    subtypes plus the word "Token". Registered under a unique synthetic key."""
    name = named or (" ".join(subtypes) + " Token")
    tl = " ".join(list(supertypes) + list(types))
    if subtypes:
        tl += " — " + " ".join(subtypes)
    key = f"{name}|{tl}|{''.join(sorted(colors))}|{power}/{toughness}|{text}"
    if key in DB.custom:
        return DB.get(key)
    rec = {"name": key, "display_name": name, "layout": "token", "type_line": tl,
           "oracle_text": text, "power": None if power is None else str(power),
           "toughness": None if toughness is None else str(toughness),
           "colors": list(colors), "mana_cost": "", "is_token": True}
    cd = DB.register(rec)
    cd.faces[0].name = name
    return cd


def norm_text(text, name):
    """Oracle text with the card's own name replaced by '~' (CR 201.5)."""
    t = text
    names = [name]
    if " // " in name:
        names += name.split(" // ")
    # Legendary short names: "Aang, at the Crossroads" is referred to as "Aang".
    for n in list(names):
        if "," in n:
            names.append(n.split(",")[0])
    for n in sorted(set(names), key=len, reverse=True):
        t = t.replace(n, "~")
    t = re.sub(r"\b[Tt]his (creature|artifact|enchantment|land|planeswalker|permanent|spell|card|Equipment|Aura|Vehicle|Saga|Class|Case|battle|token|Spacecraft|Room)\b", "~", t)
    return t


for _cls in (Face, CardDef):
    _cls.__deepcopy__ = lambda self, memo: self
    _cls.__copy__ = lambda self: self
