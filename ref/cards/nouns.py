"""Noun phrases of Oracle text -> Filters; number words.

Strict: anything not understood raises Unsupported (a card is supported only if all of its
text compiles).
"""
import re

from ..engine.effects import Filter
from .. import Unsupported

NUMS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
        "seven": 7, "eight": 8, "nine": 9, "ten": 10, "another": 1}
CARD_TYPES = {"artifact": "Artifact", "creature": "Creature", "enchantment": "Enchantment",
              "land": "Land", "planeswalker": "Planeswalker", "instant": "Instant",
              "sorcery": "Sorcery", "battle": "Battle", "kindred": "Kindred"}
PLURALS = {"artifacts": "artifact", "creatures": "creature", "enchantments": "enchantment",
           "lands": "land", "planeswalkers": "planeswalker", "instants": "instant",
           "sorceries": "sorcery", "battles": "battle", "permanents": "permanent",
           "spells": "spell", "cards": "card", "tokens": "token"}
COLORS = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}
OUTLAW_TYPES = ("Assassin", "Mercenary", "Pirate", "Rogue", "Warlock")  # CR 700.12


def num(w):
    w = w.lower()
    if w.isdigit():
        return int(w)
    if w in NUMS:
        return NUMS[w]
    raise Unsupported(f"number word {w!r}")


def _subtypes():
    from . import subtypes
    return subtypes.ALL


def singular(w):
    lw = w.lower()
    if lw in PLURALS:
        return PLURALS[lw]
    st = _subtypes()
    if w in st:
        return w
    # Subtype plurals: Elves, Dwarves, Wolves, Faeries, Zombies...
    for cand in (w[:-1], w[:-2], w[:-3] + "f", w[:-3] + "fe", w[:-3] + "y"):
        if cand in st:
            return cand
    if w.endswith("ves") and w[:-3] + "f" in st:
        return w[:-3] + "f"
    if w == "Elves":
        return "Elf"
    if w == "Dwarves":
        return "Dwarf"
    return w


def parse_noun(np, allow_spell=False):
    """'creature an opponent controls with mana value 3 or less' -> Filter."""
    text = np.strip()
    t = text
    kw = {}
    types, any_types, subtypes, any_subtypes, not_types, not_subtypes = [], [], [], [], [], []
    colors, not_colors, supertypes, not_supertypes, keywords, not_keywords = [], [], [], [], [], []
    customs = []

    def take(pattern):
        nonlocal t
        m = re.search(pattern, t)
        if m:
            t = (t[:m.start()] + t[m.end():]).strip()
            t = re.sub(r"\s{2,}", " ", t)
        return m

    # Controller / ownership clauses.
    if take(r"\byou control\b") or take(r"\byou own\b") and False:
        kw["controller"] = "you"
    if take(r"\b(an opponent|your opponents|opponents) controls?\b"):
        kw["controller"] = "opp"
    if take(r"\byou don't control\b"):
        kw["controller"] = "opp"
    if take(r"\byou own\b"):
        kw["owner"] = "you"
    if take(r"\ban opponent owns\b"):
        kw["owner"] = "opp"
    # "with mana value N or less/greater", "with power N or greater"
    m = take(r"\bwith mana value (\d+) or (less|greater)\b")
    if m:
        kw["mv"] = ("<=" if m.group(2) == "less" else ">=", int(m.group(1)))
    m = take(r"\bwith mana value X or (less|greater)\b")
    if m:
        # X of the resolving spell or ability (CR 107.3a).
        kw["mv"] = ("<=" if m.group(1) == "less" else ">=", lambda g, src, ctx: (ctx or {}).get("x", 0))
    m = take(r"\bwith mana value (\d+)\b")
    if m:
        kw["mv"] = ("==", int(m.group(1)))
    m = take(r"\bwith (power|toughness) (\d+) or (less|greater)\b")
    if m:
        kw[m.group(1)] = ("<=" if m.group(3) == "less" else ">=", int(m.group(2)))
    m = take(r"\bwith (flying|reach|trample|deathtouch|lifelink|haste|vigilance|defender|first strike|menace|hexproof|flash)\b")
    if m:
        keywords.append(m.group(1))
    m = take(r"\bwithout (flying|reach|trample|deathtouch|lifelink|haste|vigilance|defender|first strike|menace|hexproof)\b")
    if m:
        not_keywords.append(m.group(1))
    m = take(r"\bwith (a|one or more) ([+\-]1/[+\-]1|stun|oil|quest|lore) counters? on (it|them)\b")
    if m:
        kw["has_counters"] = m.group(2)
    m = take(r"\bwith counters on (it|them)\b")
    if m:
        kw["has_counters"] = "any"
    m = take(r"\bwith no counters on (it|them)\b")
    if m:
        customs.append(lambda g, c, o, src, ctx: o is None or not any(v > 0 for v in o.counters.values()))
    m = take(r"\bwith power or toughness (\d+) or (less|greater)\b")
    if m:
        n = int(m.group(1))
        le = m.group(2) == "less"

        def pt_or(g, c, o, src, ctx, n=n, le=le):
            vals = [v for v in (c.power, c.toughness) if v is not None]
            return any((v <= n) if le else (v >= n) for v in vals)
        customs.append(pt_or)
    # "attacking or blocking creature" (Divine Verdict, Elspeth's Smite, Gideon's Memorial): one
    # of the two (an attacking creature, CR 508.1k, or a blocking one, 509.1g), not both, which no
    # creature is (campaign13 830768: Gideon's Memorial's ability was never offered).
    m = take(r"\battacking or blocking\b")
    if m:
        kw["attacking_or_blocking"] = True
    words = [w for w in re.split(r"[\s,]+", t) if w]
    # Strip articles / quantifiers.
    out = []
    for w in words:
        lw = w.lower()
        if lw == "and/or":
            out.append("or")
            continue
        if lw in ("a", "an", "target", "the", "each", "all", "of", "up", "to", "one", "two",
                  "three"):
            continue
        out.append(w)
    words = out
    i = 0
    alt = bool(re.search(r",\s*(or|and/or)\s", t))  # "Lizard, Mouse, Otter, or Raccoon"
    or_group = []
    while i < len(words):
        w = words[i]
        lw = w.lower()
        if lw == "or":
            alt = True
            i += 1
            continue
        if lw == "and":
            alt = True
            i += 1
            continue
        if lw == "other" or lw == "another":
            kw["other"] = True
        elif lw in ("attacking",):
            kw["attacking"] = True
        elif lw in ("blocking",):
            kw["blocking"] = True
        elif lw == "tapped":
            kw["tapped"] = True
        elif lw == "untapped":
            kw["tapped"] = False
        elif lw == "token":
            kw["token"] = True
        elif lw == "tokens":
            kw["token"] = True
        elif lw == "nontoken":
            kw["token"] = False
        elif lw == "face-down":
            customs.append(lambda g, c, o, src, ctx: o is not None and bool(o.face_down))
        elif lw == "legendary":
            supertypes.append("Legendary")
        elif lw == "nonlegendary":
            not_supertypes.append("Legendary")
        elif lw == "basic":
            supertypes.append("Basic")
        elif lw == "nonbasic":
            not_supertypes.append("Basic")
        elif lw == "snow":
            supertypes.append("Snow")
        elif lw in COLORS:
            if alt or _next_is_or(words, i):
                kw.setdefault("any_colors", []).append(COLORS[lw])
            else:
                colors.append(COLORS[lw])
        elif lw.startswith("non") and lw[3:] in COLORS:
            not_colors.append(COLORS[lw[3:]])
        elif lw == "colorless":
            kw["colorless"] = True
        elif lw == "multicolored":
            kw["multicolored"] = True
        elif lw in ("permanent", "permanents"):
            kw["permanent"] = True
        elif lw in ("nonland",):
            kw["nonland"] = True
        elif lw.startswith("non") and (lw[3:] in CARD_TYPES or lw[3:].rstrip("s") in CARD_TYPES):
            not_types.append(CARD_TYPES[lw[3:].rstrip("s") if lw[3:] not in CARD_TYPES else lw[3:]])
        elif lw == "non-outlaw":
            # CR 700.12: an outlaw has the Assassin, Mercenary, Pirate, Rogue, and/or Warlock
            # creature types; a non-outlaw has none of them.
            not_subtypes.extend(OUTLAW_TYPES)
        elif lw.startswith("non-") and singular(w[4:]) in _subtypes():
            not_subtypes.append(singular(w[4:]))
        elif lw in CARD_TYPES or singular(lw) in CARD_TYPES:
            ct = CARD_TYPES[lw if lw in CARD_TYPES else singular(lw)]
            (any_types if alt or _next_is_or(words, i) else types).append(ct)
        elif lw in ("spell", "spells"):
            if not allow_spell:
                raise Unsupported(f"spell noun in {text!r}")
            kw["spell"] = True
        elif lw in ("card", "cards"):
            kw["card"] = True
        elif singular(w) in _subtypes():
            st = singular(w)
            (any_subtypes if alt or _next_is_or(words, i) else subtypes).append(st)
        else:
            raise Unsupported(f"noun phrase {text!r} (word {w!r})")
        i += 1
    if any_types and types:
        # "creature or planeswalker": both alternatives.
        any_types = types + any_types
        types = []
    if any_types and any_subtypes and not types and not subtypes:
        # "creature or Vehicle": a disjunction across card types and subtypes.
        ats, asts = tuple(any_types), tuple(any_subtypes)
        prev = None
        customs.append(lambda g, c, o, src, ctx, ats=ats, asts=asts: bool(set(ats) & c.types) or bool(set(asts) & c.subtypes))
        any_types, any_subtypes = [], []
    if customs:
        kw["custom"] = (lambda g, c, o, src, ctx, fs=tuple(customs): all(f(g, c, o, src, ctx) for f in fs))
    return Filter(types=types, any_types=any_types, subtypes=subtypes, any_subtypes=any_subtypes,
                  not_types=not_types, not_subtypes=not_subtypes, colors=colors,
                  not_colors=not_colors, supertypes=supertypes, not_supertypes=not_supertypes,
                  keywords=keywords, not_keywords=not_keywords, text=text, **kw)


def _next_is_or(words, i):
    return i + 1 < len(words) and words[i + 1].lower() in ("or", "and")
