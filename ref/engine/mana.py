"""Mana symbols, costs and payment feasibility (CR 106, 107.4, 118, 601.2f-h).

A cost is a list of symbols:
  int n        generic mana {n}
  'W' 'U' 'B' 'R' 'G'   colored mana
  'C'          colorless mana {C} (CR 107.4b)
  'X'          variable (CR 107.3); replaced by generic before payment
  ('H', a, b)  hybrid {a/b} (CR 107.4e)
  ('2', c)     monocolored hybrid {2/c} (CR 107.4e)
  ('P', c)     Phyrexian {c/P} (CR 107.4f)
  'S'          snow (CR 107.4h)
"""
import re
from collections import Counter

from .. import Unsupported

COLORS = "WUBRG"
TYPES = "WUBRGC"


def parse_cost(s):
    """'{2}{W}{U/B}' -> [2, 'W', ('H','U','B')]. None/'' -> []."""
    if not s:
        return []
    out = []
    for sym in re.findall(r"\{([^}]+)\}", s):
        out.append(parse_symbol(sym))
    return out


def parse_symbol(sym):
    if sym.isdigit():
        return int(sym)
    if sym in COLORS or sym in ("C", "X", "S"):
        return sym
    parts = sym.split("/")
    if len(parts) == 2:
        a, b = parts
        if a == "2" and b in COLORS:
            return ("2", b)
        if b == "P" and a in COLORS:
            return ("P", a)
        if a in COLORS and b in COLORS:
            return ("H", a, b)
    if len(parts) == 3 and parts[2] == "P":
        return ("HP", parts[0], parts[1])
    raise Unsupported(f"mana symbol {{{sym}}}")


def mana_value(cost, x=0):
    """CR 202.3: total of the mana cost; X counts as its chosen value on the stack (202.3e)."""
    mv = 0
    for s in cost:
        if isinstance(s, tuple) and s[0] == "XR":
            continue
        if isinstance(s, int):
            mv += s
        elif s == "X":
            mv += x
        elif isinstance(s, tuple) and s[0] == "2":
            mv += 2
        else:
            mv += 1
    return mv


def cost_colors(cost):
    """CR 202.2: colors of the mana symbols in a cost."""
    out = set()
    for s in cost:
        if isinstance(s, str) and s in COLORS:
            out.add(s)
        elif isinstance(s, tuple):
            for c in s[1:]:
                if c in COLORS:
                    out.add(c)
    return out


def fmt_cost(cost):
    out = []
    for s in cost:
        if isinstance(s, int) or (isinstance(s, str)):
            out.append("{%s}" % s)
        elif s[0] == "H":
            out.append("{%s/%s}" % (s[1], s[2]))
        elif s[0] == "2":
            out.append("{2/%s}" % s[1])
        elif s[0] == "P":
            out.append("{%s/P}" % s[1])
        else:
            out.append("{%s}" % "/".join(s[1:]))
    return "".join(out) or "{0}"


class Unit:
    """One mana in a pool (CR 106.4). `restrict`: None or a predicate(spend_ctx) -> bool for
    "spend this mana only ..." (CR 106.6); `rider`: data applied to what it's spent on
    (e.g. Cavern of Souls' "can't be countered")."""
    __slots__ = ("color", "source", "restrict", "rider")

    def __init__(self, color, source=None, restrict=None, rider=None):
        self.color = color
        self.source = source
        self.restrict = restrict
        self.rider = rider

    def __repr__(self):
        return f"Unit({self.color})"

    def __deepcopy__(self, memo):
        return Unit(self.color, self.source, self.restrict, self.rider)


def normalize(cost, x=0):
    """Replace X by generic, merge generic. Returns (colored slots list, generic int).
    Slots: frozenset of acceptable unit colors ('C' only for {C}). {2/c} and Phyrexian are
    expanded by the caller (they are choices)."""
    generic = 0
    slots = []
    xred = 0
    for s in cost:
        if isinstance(s, tuple) and s[0] == "XR":
            xred += s[1]
            continue
        if isinstance(s, int):
            generic += s
        elif s == "X":
            generic += x
        elif isinstance(s, str) and s in TYPES:
            slots.append(frozenset(s))
        elif s == "S":
            raise Unsupported("snow mana")
        elif s[0] == "H":
            slots.append(frozenset(s[1:]))
        else:
            raise Unsupported(f"cost symbol {s} needs expansion")
    if xred:
        generic = max(0, generic - xred)
    return slots, generic


def expand_choices(cost, life_available=None):
    """Yield concrete costs for {2/c} and Phyrexian symbols (each way to pay them):
    (cost_without_those, extra_life)."""
    variants = [([], 0)]
    for s in cost:
        if isinstance(s, tuple) and s[0] == "2":
            variants = [(c + [s[1]], l) for c, l in variants] + [(c + [2], l) for c, l in variants]
        elif isinstance(s, tuple) and s[0] == "P":
            variants = [(c + [s[1]], l) for c, l in variants] + [(c, l + 2) for c, l in variants]
        elif isinstance(s, tuple) and s[0] == "HP":
            variants = [(c + [("H", s[1], s[2])], l) for c, l in variants] + [(c, l + 2) for c, l in variants]
        elif isinstance(s, tuple) and s[0] == "XR":
            variants = [(c + [s], l) for c, l in variants]
        else:
            variants = [(c + [s], l) for c, l in variants]
    seen = set()
    for c, l in variants:
        k = (repr(sorted(map(repr, c))), l)
        if k in seen:
            continue
        seen.add(k)
        if life_available is not None and l > 0 and life_available < l:
            continue
        yield c, l


def _match(slots, unit_sets):
    """Max bipartite matching: can every colored slot get a distinct unit? unit_sets: list of
    frozensets of colors each unit may be (a unit from 'any color' has all five)."""
    match = {}  # unit idx -> slot idx

    def try_slot(si, seen):
        for ui, us in enumerate(unit_sets):
            if ui in seen or not (us & slots[si]):
                continue
            seen.add(ui)
            if ui not in match or try_slot(match[ui], seen):
                match[ui] = si
                return True
        return False

    # Most constrained slots first.
    order = sorted(range(len(slots)), key=lambda i: len(slots[i]))
    for si in order:
        if not try_slot(si, set()):
            return False
    return True


def payable(slots, generic, unit_sets):
    """Can these units pay (slots, generic)?"""
    if len(unit_sets) < len(slots) + generic:
        return False
    return _match(slots, unit_sets)


def assign_unit(slots, generic, color):
    """Pay one unit of `color` into the cost (greedy but feasibility-preserving for the
    common case): exact single-color slot, then a hybrid slot containing it, then generic.
    Returns (slots, generic) after payment, or None if the unit can't pay anything."""
    for i, s in enumerate(slots):
        if s == frozenset(color):
            return slots[:i] + slots[i + 1:], generic
    for i, s in enumerate(slots):
        if color in s:
            return slots[:i] + slots[i + 1:], generic
    if generic > 0 and color is not None:
        return slots, generic - 1
    return None


def unit_assignments(slots, generic, color):
    """All distinct ways to pay one unit of `color` into the cost."""
    out = []
    seen = set()
    for i, s in enumerate(slots):
        if color in s and s not in seen:
            seen.add(s)
            out.append((slots[:i] + slots[i + 1:], generic))
    if generic > 0:
        out.append((slots, generic - 1))
    return out


def counter_of(units):
    return Counter(u.color for u in units)
