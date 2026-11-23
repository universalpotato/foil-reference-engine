"""Oracle text -> abilities (IR). Strict: a face compiles only if every line is understood;
anything else raises Unsupported with the offending text (so the coverage report is honest and
nothing is silently misplayed).

Structure: each Oracle line is a keyword line, an activated ability ("cost: effect"), a
triggered ability ("When/Whenever/At ..."), a static ability, or (for instants and sorceries)
part of the spell's effect. Effects are sentences matched by patterns in `effects.py`-style
builders below; "target ..." phrases allocate TargetSpecs in order.
"""
import re

from ..engine import ir
from ..engine.ir import (Keyword, SpellAbility, Activated, Triggered, Static, Cost, ManaProduce,
                         LayerPart, RulePart, ReplacementPart, TargetSpec, Seq, Modes, Mode, Noop)
from ..engine import effects as fx
from ..engine.effects import Filter
from ..engine.mana import parse_cost
from .model import norm_text, token_def, COLOR_WORDS
from .nouns import parse_noun, num, singular, COLORS
from . import triggers as tg
from . import overrides
from .. import Unsupported

SIMPLE_KEYWORDS = {"flying", "first strike", "double strike", "deathtouch", "haste", "hexproof",
                   "indestructible", "lifelink", "menace", "reach", "trample", "vigilance",
                   "defender", "flash", "shroud", "infect", "wither", "skulk", "changeling"}
ABILITY_WORDS = {"Landfall", "Delirium", "Threshold", "Raid", "Heroic", "Constellation",
                 "Magecraft", "Ferocious", "Revolt", "Coven", "Morbid", "Alliance",
                 "Celebration", "Valiant", "Eerie", "Survival", "Void", "Infusion", "Domain",
                 "Formidable", "Metalcraft", "Fateful hour", "Hellbent", "Battalion",
                 "Spell mastery", "Addendum", "Corrupted", "Descend 4", "Descend 8", "Bloodrush",
                 "Opus", "Flurry", "Renew", "Disappear", "Converge"}
# Not ability words: Exhaust, Max speed, Start your engines! and Paradigm are keyword abilities
# with rules meaning (CR 702.177-702.179, 702.192), so they must not be stripped.


def _keyword_names():
    """Keyword ability names from the CR 702.x headings (lowercase)."""
    import os
    txt = open(os.path.join(os.path.dirname(__file__), "..", "..", "cr", "MagicCompRules-20260925.txt")).read()
    return {m.group(1).strip().lower() for m in re.finditer(r"^702\.\d+\. (.+)$", txt, re.M)}


try:
    _KEYWORD_NAMES = _keyword_names()
except Exception:  # noqa: BLE001
    _KEYWORD_NAMES = set()


def strip_ability_word(line):
    """Ability words (CR 207.2c) and flavor words (207.2d) have no rules meaning. A flavor word
    is any other italic-style prefix "Words — " that isn't a keyword (702) or a chapter number."""
    m = re.match(r"^([A-Z][A-Za-z' !,0-9-]+?) — (.+)$", line)
    if not m:
        return line
    head = m.group(1)
    if head in ABILITY_WORDS:
        return m.group(2)
    lh = head.lower()
    if lh == "to solve":
        return line  # CR 719.3a: a Case's special keyword ability, not an ability word
    if re.fullmatch(r"[IVX]+(?:, [IVX]+)*", head) or any(lh == k or lh.startswith(k + " ") for k in _KEYWORD_NAMES):
        return line
    if re.match(r"^(?:(When|Whenever|At|As long as|If|Prevent|~|\{|Enchanted|Equipped|Creatures|Other|You|Each)\b|Pay \d+ life: )", m.group(2)):
        return m.group(2)
    return line


def strip_reminder(t):
    prev = None
    while prev != t:
        prev = t
        t = re.sub(r"\s*\([^()]*\)", "", t)
    return t.strip()


# Extension hooks: the one registry for card templates kept outside this file. Each hook
# returns None when it doesn't apply; the first non-None result wins.
# - KEYWORD_HOOKS fn(part, cx) -> [abilities]: tried first in parse_keyword (before the built-in
#   keywords; each hook matches only its own keyword: plot, warp, gift, ...);
# - LINE_HOOKS fn(line, cx) -> [abilities]: tried first in compile_line, after ability words are
#   stripped (power-up, Saga chapters);
# - POST_HOOKS fn(abilities, cx) -> abilities: after the whole face compiled, in order (gift,
#   paradigm).
# The trigger and condition parsers have the same kind of registry in triggers.py
# (TRIGGER_HOOKS, CONDITION_HOOKS), static abilities in statics.py (EXTRA_STATICS) and effect
# sentences in effects_text.py (PATTERNS, MERGE_NEXT, MERGE_PAIRS).
# Call order = registration order = import order, which is fixed: triggers.py's own parsers
# (imported above), then ref/cards/recent.py, then ref/cards/batch7.py (imported at the end of
# this file), each in its file order. PATTERNS and EXTRA_STATICS entries inserted at the front
# ([0:0]) by a later module are tried before an earlier module's.
KEYWORD_HOOKS = []
LINE_HOOKS = []
POST_HOOKS = []
# Modal headers with a condition on the number of modes ("Choose one. If you descended this turn,
# you may choose both instead."): hook(line, cx) -> None, or a function Modes -> Modes applied
# after compile_modes (ref/cards/batch9b.py).
MODAL_HEADER_HOOKS = []


class Ctx:
    """Compile-time context for one face."""

    def __init__(self, face):
        self.face = face
        self.name = face.name
        self.types = set(face.types)
        self.is_spell = bool({"Instant", "Sorcery"} & self.types)
        self.is_permanent = not self.is_spell
        self.trigger = None  # the Trigger of the triggered ability being compiled


class Targets:
    """Allocates target words while parsing one ability's effect text."""

    def __init__(self):
        self.specs = []

    def add(self, spec):
        self.specs.append(spec)
        return fx.target(len(self.specs) - 1)


def compile_face(face):
    over = overrides.get(face)
    if over is not None:
        return over
    cx = Ctx(face)
    text = norm_text(face.text, face.card.name if face.card.layout in ("normal", "saga", "class", "case", "token") else face.name)
    text = strip_reminder(text)
    m = re.fullmatch(r"([^\s,]+) the [^\s,]+", face.name)
    if m and "Legendary" in face.supertypes:
        # CR 201.5c: a shortened name ("Smaug" on Smaug the Magnificent) means the full name
        # (not when it starts another name: Tuktuk the Explorer creates "Tuktuk the Returned").
        text = re.sub(r"\b" + re.escape(m.group(1)) + r"\b(?! the\b)", "~", text)
    # CR 607.2a: "exiled with ~" refers to cards this object's own abilities exiled.
    cx.links_exile = "exiled with ~" in text
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    abilities = []
    spell_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Modal: "Choose one —" followed by bullet lines.
        m = re.match(r"^(Choose (one|two|one or both|one or more|any number)(?: or more)?(?: —|\.)?|Spree)\s*(?:—)?\s*$", line)
        hdr_fix = None
        if not m:
            for h in MODAL_HEADER_HOOKS:
                hdr_fix = h(line, cx)
                if hdr_fix is not None:
                    break
        if m or line.startswith("Choose one —") or line == "Spree" or hdr_fix is not None:
            bullets = []
            j = i + 1
            while j < len(lines) and (lines[j].startswith("•") or (line == "Spree" and lines[j].startswith("+ "))):
                bullets.append(lines[j][1:].strip() if lines[j].startswith("•") else lines[j])
                j += 1
            if not bullets:
                raise Unsupported(f"modal line without modes: {line!r}")
            modes = compile_modes(line, bullets, cx)
            if hdr_fix is not None:
                modes = hdr_fix(modes)
            if cx.is_spell:
                spell_lines.append(("modes", modes))
            else:
                raise Unsupported(f"modal permanent text: {line!r}")
            i = j
            continue
        mt = re.match(r"^((?:When|Whenever|At)\b.+?), (choose (?:one|two|one or both|one or more)(?: that hasn't been chosen this turn)?)(?: —|\.)$",
                      strip_ability_word(line))
        if mt and i + 1 < len(lines) and lines[i + 1].startswith("•"):
            # A modal triggered ability (CR 700.2b: modes are chosen as it's put on the stack).
            bullets = []
            j = i + 1
            while j < len(lines) and lines[j].startswith("•"):
                bullets.append(lines[j][1:].strip())
                j += 1
            abilities.append(compile_modal_trigger(mt.group(1), mt.group(2), bullets, line, cx))
            i = j
            continue
        res = compile_line(line, cx)
        if res == "spell":
            spell_lines.append(("line", strip_ability_word(line)))
        else:
            abilities.extend(res)
        i += 1
    if spell_lines:
        abilities.append(compile_spell(spell_lines, cx))
    elif cx.is_spell:
        abilities.append(SpellAbility(Noop(), text=""))
    for h in POST_HOOKS:
        abilities = h(abilities, cx)
    return abilities


# =============================================================================== lines

def compile_line(line, cx):
    """Returns a list of abilities, or 'spell' for an instant/sorcery effect line."""
    # Ability words (CR 207.2c) and flavor words (207.2d) have no rules meaning.
    line = strip_ability_word(line)
    for h in LINE_HOOKS:
        r = h(line, cx)
        if r is not None:
            return r
    kws = parse_keyword_line(line, cx)
    if kws is not None:
        return kws
    m = re.match(r"^As an additional cost to cast ~, (.+)\.$", line)
    if m:
        k = Keyword("additional_cost", value=parse_additional_cost(m.group(1), cx), text=line)
        return [k]
    # Loyalty abilities: "+1: ...", "−2: ...", "0: ..."
    m = re.match(r"^([+−\-]?)(\d+|X): (.+)$", line)
    if m and "Planeswalker" in cx.types:
        sign, n, rest = m.groups()
        if n == "X":
            raise Unsupported("X loyalty cost")
        v = int(n) * (-1 if sign in ("−", "-") else 1)
        eff, specs, modes = compile_effect(rest, cx, ability=True)
        return [Activated(Cost(loyalty=v), eff, specs, timing="sorcery", text=line, loyalty=v, modes=modes)]
    # Triggered abilities.
    if re.match(r"^(When|Whenever|At)\b", line):
        return [compile_triggered(line, cx)]
    # CR 702.177a: "Exhaust — [Cost]: [Effect]" means "[Cost]: [Effect]. Activate only once."
    m = re.match(r"^Exhaust — (.+)$", line)
    if m:
        act = split_activated(m.group(1))
        if act is None:
            raise Unsupported(f"exhaust ability {line!r}")
        ab = compile_activated(act[0], act[1], line, cx)
        ab.once = True  # one "Activate only once" flag (power-up too, 702.193a): casting.can_activate
        return [ab]
    # Activated abilities: "<cost>: <effect>".
    act = split_activated(line)
    if act is not None:
        return [compile_activated(act[0], act[1], line, cx)]
    st = compile_static(line, cx)
    if st is not None:
        return st
    if cx.is_spell:
        return "spell"
    raise Unsupported(f"line: {line!r}")


def split_activated(line):
    # The cost ends at the first ": " outside quotes.
    depth = 0
    for i, ch in enumerate(line):
        if ch == '"':
            depth ^= 1
        if ch == ":" and depth == 0 and i + 1 < len(line) and line[i + 1] == " ":
            cost = line[:i]
            if re.search(r"\{[^}]+\}|\b(Sacrifice|Discard|Pay|Exile|Tap|Remove|Return|Collect evidence|Blight|Put)\b", cost) and \
                    not cost.startswith(("When", "Whenever", "At ", "As ")):
                return cost, line[i + 2:]
            return None
    return None


# =============================================================================== keywords

def parse_keyword_line(line, cx):
    if re.fullmatch(r"Firebending X, where X is ~'s power\.?", line):
        return parse_keyword(line.rstrip("."), cx)
    parts = [p.strip() for p in re.split(r",\s*", line.rstrip("."))]
    out = []
    for p in parts:
        k = parse_keyword(p, cx)
        if k is None:
            return None
        out.extend(k)
    return out


def parse_keyword(p, cx):
    lp = p.lower()
    for h in KEYWORD_HOOKS:
        r = h(p, cx)
        if r is not None:
            return r
    if lp in SIMPLE_KEYWORDS:
        if lp == "changeling":
            return [Keyword("changeling"), _changeling()]
        return [Keyword(lp)]
    m = re.match(r"^ward\s*(\{[^}]+\}(?:\{[^}]+\})*)$", lp, re.I)
    if m:
        return [ward_ability(parse_cost(m.group(1).upper()), 0)]
    m = re.match(r"^ward—pay (\d+) life$", lp)
    if m:
        return [ward_ability([], int(m.group(1)))]
    m = re.match(r"^equip (\{[^}]+\}(?:\{[^}]+\})*)$", p, re.I)
    if m:
        return [equip_ability(parse_cost(m.group(1)))]
    m = re.match(r"^kicker (\{[^}]+\}(?:\{[^}]+\})*)$", p, re.I)
    if m:
        return [Keyword("kicker", value=parse_cost(m.group(1)), text=p)]
    m = re.match(r"^flashback (\{[^}]+\}(?:\{[^}]+\})*)$", p, re.I)
    if m:
        k = Keyword("flashback", value=parse_cost(m.group(1)), text=p)
        k.alt_zone = "graveyard"
        return [k]
    m = re.match(r"^enchant (.+)$", lp)
    if m:
        f = parse_noun(m.group(1))
        return [Keyword("enchant", value=f, text=p), _aura_enters_uncast(f)]
    m = re.match(r"^protection from (white|blue|black|red|green)$", lp)
    if m:
        col = COLORS[m.group(1)]
        return [Keyword("protection", value=lambda g, c, col=col: col in c.colors, text=p)]
    if lp == "prowess":
        return [prowess_ability()]
    if lp == "increment":
        return [increment_ability()]
    m = re.fullmatch(r"firebending (\d+|x), where x is ~'s power", lp)
    if m or re.fullmatch(r"firebending \d+", lp):
        # CR 702.189a: the added mana stays until end of combat; the engine empties mana pools
        # at the end of every step (106.4), so this fails loudly if it would add mana.
        def amount(g, ctx, lp=lp):
            if "where" in lp:
                c = fx.self_chars(g, ctx)
                return max(0, (c.power or 0) if c is not None else 0)
            return int(re.search(r"\d+", lp).group(0))
        t = Triggered(tg.Attacks(self_=True),
                      fx.UnsupportedIf(lambda g, ctx: amount(g, ctx) > 0, "firebending mana that lasts until end of combat (CR 702.189a)"),
                      text=p)
        t.keyword = "firebending"
        return [t]
    m = re.match(r"^cycling (\{[^}]+\}(?:\{[^}]+\})*)$", p, re.I)
    if m:
        # CR 702.29a: "[Cost], Discard this card: Draw a card." (functions only in hand)
        a = Activated(Cost(mana=parse_cost(m.group(1)), discard_self=True), fx.Draw(1), [],
                      text=p, zones=("hand",))
        a.keyword = "cycling"
        return [a]
    m = re.match(r"^harmonize (\{[^}]+\}(?:\{[^}]+\})*)$", p, re.I)
    if m:
        cost = parse_cost(m.group(1))
        k = Keyword("harmonize", value=cost, text=p)
        k.alt_zone = "graveyard"
        k.extra = {"parts": [fx.HarmonizeChoice(cost)]}
        return [k]
    m = re.match(r"^disguise (\{[^}]+\}(?:\{[^}]+\})*)$", p, re.I)
    if m:
        # CR 702.168: the disguise cost turns it face up (special action, 702.168d). Casting
        # it face down for {3} isn't implemented: fail loudly while it's in a hand.
        from .statics import runtime_guard
        return [Keyword("disguise", value=parse_cost(m.group(1)), text=p),
                runtime_guard(lambda g, o: o.zone == "hand", "casting a card face down with disguise (CR 702.168a)", p)]
    if lp == "convoke":
        # CR 702.51: paying with creatures isn't implemented: fail loudly when its owner could
        # cast it with creature help (in hand while controlling an untapped creature).
        from .statics import runtime_guard

        def could_convoke(g, o):
            if o.zone != "hand":
                return False
            if g.s.chars_cache is None or g._partial is not None:
                return True  # computing layers: can't look at other permanents; fail loudly
            return any(g.ctrl(x) == o.owner and not g.s.objs[x].tapped and "Creature" in g.chars(x).types
                       for x in g.s.battlefield)
        return [Keyword("convoke", text=p), runtime_guard(could_convoke, "convoke (CR 702.51)", p)]
    m = re.fullmatch(r"affinity for (\w+)", lp)
    if m:
        # CR 702.41a: "This spell costs {1} less to cast for each [text] you control."
        from .statics import self_cost_reduction
        w = p.split()[-1]
        return [Keyword("affinity", text=p), self_cost_reduction(1, singular(w) + " you control", p)]
    m = re.match(r"^evoke (\{[^}]+\}(?:\{[^}]+\})*)$", p, re.I)
    if m:
        # CR 702.74a: "You may cast this card by paying [cost] rather than paying its mana cost"
        # (an alternative cost, 601.2b, 601.2f-h) and "When this permanent enters, if its evoke
        # cost was paid, its controller sacrifices it."
        k = Keyword("evoke", value=parse_cost(m.group(1)), text=p)
        k.alt_zone = "hand"
        return [k, _evoke_sacrifice(p)]
    if lp == "bargain":
        # CR 702.166a: an optional additional cost.
        return [Keyword("additional_cost", value=fx.ChoiceCost([fx.BargainCost()], optional=True,
                                                               label=lambda i, a: "cost:bargain"), text=p)]
    return None


def _evoke_sacrifice(text):
    """CR 702.74a: "When this permanent enters, if its evoke cost was paid, its controller
    sacrifices it." (603.4: an intervening "if", checked as it triggers and resolves.)"""
    def paid(g, ctx):
        o = g.s.objs.get(ctx.get("source")) or g.s.lki_obj.get(ctx.get("source"))
        return bool(o is not None and o.cast and o.cast.get("how") == "evoke")

    def sac(g, ctx):
        src = ctx.get("source")
        o = g.s.objs.get(src)
        if o is not None and o.zone == "battlefield":
            g.sacrifice(src)  # its controller (as the ability resolves) sacrifices it
    t = Triggered(tg.Enters(self_=True), ir.Fn(sac, "evoke: sacrifice it"), condition=paid,
                  text=f"{text} (sacrifice)")
    return t


def _aura_enters_uncast(filt):
    """CR 303.4f-g: an Aura put onto the battlefield without being cast (and not attached by
    the effect) is attached to something it could enchant, chosen as it enters by the player
    putting it there; if nothing is legal, it stays in its current zone."""
    from ..engine.decisions import Decision, Opt

    def applies(g, ev, src):
        return ev["opts"].get("attach_to") is None and ev.get("from") != "stack" and not ev["opts"].get("face_down")

    def apply(g, ev, src):
        p = ev["opts"].get("controller", ev.get("owner"))
        old = ev.get("old")
        cands = []
        for t in g.s.battlefield:
            if t == old:
                continue
            if not filt.match(g, t, None, {"controller": p}):
                continue
            if old in g.s.objs and g.protected_from(t, old):
                continue  # CR 702.16c
            cands.append(t)
        if not cands:
            ev["cancel"] = True  # 303.4g
            return
        opts = [Opt(f"choose:{g.ref(t)}", t, sig=("aura", g.obj_sig(t))) for t in cands]
        ev["opts"]["attach_to"] = g.ask(Decision(p, "ChooseCard", opts,
                                                 prompt="Choose what the Aura enchants (CR 303.4f)."))
    part = ReplacementPart("enter_self", applies, apply, text="Aura enters attached (CR 303.4f)")
    part.commutes = True
    return Static([part], text="aura-enters")


def _changeling():
    from ..cards.subtypes import ALL

    def fn(g, oid, c, src):
        if "Creature" in c.types or "Kindred" in c.types:
            c.subtypes |= set(_CREATURE_TYPES)
    st = Static([LayerPart(4, "self", fn, "changeling: all creature types")], text="changeling", zones=("all",))
    st.cda = True
    return st


def _creature_types():
    import re as _re
    txt = open(__import__("os").path.join(__import__("os").path.dirname(__file__), "..", "..", "cr", "MagicCompRules-20260925.txt")).read()
    # CR 205.3m: "One creature type is two words long: Time Lord. All other creature types are
    # one word long: Advisor, ..., and Zubera."
    line = _re.search(r"^205\.3m .*$", txt, _re.M)
    line = line.group(0) if line else ""
    out = []
    m = _re.search(r"two words long: (.*?)\.", line)
    if m:
        out += [x.strip() for x in m.group(1).split(",") if x.strip()]
    m = _re.search(r"one word long: (.*?)\.$", line)
    if not m:
        m = _re.search(r"The creature types are (.*?)\.$", line)
    body = _re.sub(r"\(see[^)]*\)", "", m.group(1)) if m else ""
    out += [x.strip().replace("and ", "") for x in body.split(",") if x.strip()]
    return out


try:
    _CREATURE_TYPES = [t.replace("one word long: ", "") for t in _creature_types()]
except Exception:  # noqa: BLE001
    _CREATURE_TYPES = []

_SUBTYPE_RULES = {"Artifact": "205.3g", "Enchantment": "205.3h", "Land": "205.3i",
                  "Planeswalker": "205.3j", "Instant": "205.3k", "Sorcery": "205.3k",
                  "Battle": "205.3q"}
_SUBTYPES_OF = {}


def subtypes_of(card_type):
    """The subtypes correlated with a card type (CR 205.3g-m), read from the CR text."""
    if card_type in _SUBTYPES_OF:
        return _SUBTYPES_OF[card_type]
    if card_type in ("Creature", "Kindred"):
        out = frozenset(_CREATURE_TYPES)
    else:
        rule = _SUBTYPE_RULES.get(card_type)
        out = frozenset()
        if rule is not None:
            import os
            txt = open(os.path.join(os.path.dirname(__file__), "..", "..", "cr", "MagicCompRules-20260925.txt")).read()
            m = re.search(r"^" + re.escape(rule) + r" .*? types are (.*?)\.(?: |$)", txt, re.M)
            if m:
                body = re.sub(r"\(see[^)]*\)", "", m.group(1))
                out = frozenset(x.strip().replace("and ", "", 1) if x.strip().startswith("and ") else x.strip()
                                for x in body.split(",") if x.strip())
    _SUBTYPES_OF[card_type] = out
    return out


def ward_ability(cost, life):
    """CR 702.21a: "Whenever this permanent becomes the target of a spell or ability an
    opponent controls, counter it unless that player pays [cost]." """
    eff = fx.CounterUnlessPays(cost, life)
    t = Triggered(tg.WardTrigger(by_opponent=True), eff, text=f"ward {cost or str(life) + ' life'}")
    t.keyword = "ward"
    t.value = (cost, life)
    return t


def equip_ability(cost):
    """CR 702.6a: "[Cost]: Attach this permanent to target creature you control. Activate only
    as a sorcery." """
    spec = TargetSpec("creature", filt=Filter(types=["Creature"], controller="you", text="creature you control"),
                      text="creature you control")
    a = Activated(Cost(mana=cost), fx.Attach(fx.target(0)), [spec], timing="sorcery", text="equip")
    a.keyword = "equip"
    return a


def increment_ability():
    """CR 702.191a: "Whenever you cast a spell, if this permanent is a creature and the amount
    of mana spent to cast that spell is greater than this creature's power or this creature's
    toughness, put a +1/+1 counter on this creature." """
    def cond(g, ctx):
        c = fx.self_chars(g, ctx)
        if c is None or "Creature" not in c.types:
            return False
        n = fx.spell_mana_spent(g, ctx)
        return (c.power is not None and n > c.power) or (c.toughness is not None and n > c.toughness)
    t = Triggered(tg.CastSpell(None, "you"), fx.PutCounters(fx.self_permanent(), "+1/+1", 1),
                  condition=cond, text="increment")
    t.keyword = "increment"
    return t


def prowess_ability():
    """CR 702.108a."""
    t = Triggered(tg.CastSpell(Filter(not_types=["Creature"], text="noncreature"), who="you"),
                  fx.Pump(fx.self_permanent(), 1, 1), text="prowess")
    t.keyword = "prowess"
    return t


# =============================================================================== costs

def parse_cost_text(ct, cx):
    parts = [p.strip() for p in re.split(r",\s*", ct)]
    c = Cost(text=ct)
    for p in parts:
        lp = p.lower()
        if re.fullmatch(r"(\{[^}]+\})+", p):
            syms = re.findall(r"\{([^}]+)\}", p)
            if "T" in syms:
                c.tap = True
                syms = [s for s in syms if s != "T"]
            if "Q" in syms:
                c.untap = True
                syms = [s for s in syms if s != "Q"]
            c.mana += parse_cost("".join("{%s}" % s for s in syms))
            continue
        m = re.fullmatch(r"pay (\d+) life", lp)
        if m:
            c.life = int(m.group(1))
            continue
        if lp in ("sacrifice ~",):
            c.sac_self = True
            continue
        m = re.fullmatch(r"sacrifice (a|an|another|two|three) (.+)", lp)
        if m:
            # The noun keeps its case: subtypes are capitalized ("Sacrifice three Treasures").
            f = parse_noun(p[m.start(2):m.end(2)] + " you control")
            if m.group(1) == "another":
                f.other = True
            c.sacrifice = (f, num(m.group(1)) if m.group(1) not in ("another",) else 1)
            continue
        m = re.fullmatch(r"discard (a|an|two) (.+)", lp)
        if m:
            noun = m.group(2)
            f = Filter(text="card") if noun == "card" else parse_noun(noun.replace(" card", ""))
            c.discard = (f, num(m.group(1)))
            continue
        if lp == "discard ~":
            c.discard_self = True
            continue
        if lp in ("exile ~ from your graveyard", "exile ~"):
            c.exile_self = True
            continue
        m = re.fullmatch(r"exile (a|an|two|three|four|five) (.+?) cards? from your graveyard", lp)
        if m:
            noun = m.group(2)
            f = Filter(text="card") if noun in ("", "other") else parse_noun(noun)
            if noun == "other":
                f.other = True
            c.exile_from_gy = (f, num(m.group(1)))
            continue
        m = re.fullmatch(r"remove (a|an|one|two|three) counters? from ~", lp)
        if m:
            c.remove_counters = (None, num(m.group(1)))  # counters of any kind
            continue
        m = re.fullmatch(r"remove (a|an|one|two|three) (.+?) counters? from ~", lp)
        if m:
            c.remove_counters = (m.group(2), num(m.group(1)))
            continue
        m = re.fullmatch(r"put (a|an|one|two) (.+?) counters? on ~", lp)
        if m:
            c.put_counters = (m.group(2), num(m.group(1)))
            continue
        m = re.fullmatch(r"tap an untapped (.+?) you control", lp)
        if m:
            c.tap_others = (parse_noun(m.group(1)), 1)
            continue
        m = re.fullmatch(r"tap (two|three) (other )?untapped (\w+) you control", lp)
        if m:
            # "Tap two untapped artifacts you control" (Tenured Tethermage; Bladewheel Chariot's
            # "two other"): never the source itself (CR 118.3, 602.2b).
            c.tap_others = (parse_noun(singular(m.group(3))), num(m.group(1)))
            continue
        raise Unsupported(f"cost part {p!r}")
    return c


# =============================================================================== activated

def compile_activated(cost_t, eff_t, line, cx):
    cost = parse_cost_text(cost_t, cx)
    timing = "instant"
    condition = None
    limit = None
    m = re.search(r"\s*Activate only as a sorcery\.$", eff_t)
    if m:
        timing = "sorcery"
        eff_t = eff_t[:m.start()].strip()
    m = re.search(r"\s*Activate only once each turn\.$", eff_t)
    if m:
        limit = 1
        eff_t = eff_t[:m.start()].strip()
    m = re.search(r"\s*Activate only during your turn\.$", eff_t)
    if m:
        # CR 602.5b: a restriction on when the ability may be activated.
        condition = lambda g, oid, p: g.s.active == p  # noqa: E731
        eff_t = eff_t[:m.start()].strip()
    m = re.search(r"\s*Activate only if (.+?)\.$", eff_t)
    if m:
        cond_t = m.group(1)
        mm = re.fullmatch(r"(.+?) and only as a sorcery", cond_t)
        if mm:
            cond_t = mm.group(1)
            timing = "sorcery"
        mm = re.fullmatch(r"(.+?) and only once each turn", cond_t)
        if mm:
            cond_t = mm.group(1)
            limit = 1  # CR 602.5b
        condition = parse_activation_condition(cond_t, cx)
        eff_t = eff_t[:m.start()].strip()
    mana = parse_mana_effect(eff_t, cx)
    if mana is not None:
        prod, extra = mana
        return Activated(cost, extra or Noop(), [], timing=timing, text=line, mana=prod,
                         condition=condition, limit=limit)
    eff, specs, modes = compile_effect(eff_t, cx, ability=True)
    zones = ("battlefield",)
    if cost.exile_self and "from your graveyard" in cost_t:
        zones = ("graveyard",)
    if cost.discard_self:
        zones = ("hand",)
    return Activated(cost, eff, specs, timing=timing, text=line, condition=condition, limit=limit,
                     zones=zones, modes=modes)


def parse_activation_condition(t, cx):
    m = re.fullmatch(r"(?:(white|blue|black|red|green) )?sources you controlled dealt (\d+) or more (noncombat |combat )?damage this turn", t)
    if m:
        # The damage dealt this turn by sources p controlled as it was dealt (zones.deal_damage's
        # turn_stats ("dealt", color, combat)); several sources add up (Temple of Power ruling
        # 2023-11-10). A scenario starts with none dealt this turn.
        col = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}.get(m.group(1))
        need = int(m.group(2))
        kinds = {"noncombat ": (False,), "combat ": (True,)}.get(m.group(3) or "", (False, True))

        def cond(g, oid, p, col=col, need=need, kinds=kinds):
            st = g.s.players[p].turn_stats
            return sum(st.get(("dealt", col, k), 0) for k in kinds) >= need
        return cond
    if t == "an opponent lost life this turn":
        # Hired Claw ruling 2024-07-26: whether an opponent lost life, not how their life total
        # changed.
        return lambda g, oid, p: g.s.players[1 - p].life_lost > 0
    if t == "~ entered this turn or if you control a basic land":
        # Hidden Lair, Gleaming Bastion (CR 602.5b): this object entered the battlefield this
        # turn (it is the same object since then, 400.7), or its controller controls a basic land.
        return lambda g, oid, p: g.s.objs[oid].entered_turn == g.s.turn or any(
            "Basic" in g.chars(x).supertypes and "Land" in g.chars(x).types
            for x in g.s.battlefield if g.ctrl(x) == p)
    m = re.fullmatch(r"you control an? (\w+) or an? (\w+)", t)
    if m:
        a, b = m.group(1), m.group(2)
        return lambda g, oid, p, a=a, b=b: any(
            (a in g.chars(x).subtypes or b in g.chars(x).subtypes) for x in g.s.battlefield if g.ctrl(x) == p)
    m = re.fullmatch(r"you control an? (\w+)", t)
    if m:
        f = parse_noun(m.group(1))
        return lambda g, oid, p, f=f: any(f.match(g, x, oid, {"controller": p}) for x in g.s.battlefield if g.ctrl(x) == p)
    raise Unsupported(f"activation condition {t!r}")


MANA_SYM = r"\{[WUBRGC]\}"


def parse_mana_effect(t, cx):
    """Mana ability effects (CR 605.1a). Returns (ManaProduce, extra effect) or None."""
    t0 = t
    restrict = None
    rider = None
    holder = {}
    m = re.search(r"\s*Spend this mana only to cast a creature spell of the chosen type, and that spell can't be countered\.$", t)
    if m:
        restrict, rider = _chosen_type_restriction(holder)
        t = t[:m.start()].strip()
    m = re.search(r"\s*Spend this mana only to cast (.+?)\.$", t)
    if m:
        restrict = parse_spend_restriction(m.group(1), cx)
        t = t[:m.start()].strip()
    m = re.search(r"\s*Spend this mana only to activate abilities\.$", t)
    if m:
        # CR 106.6: mana that can pay only for activated abilities' costs (The Enigma Jewel),
        # mana abilities' too (602.2, 605.1a; craft is an activated ability: ruling 2023-11-10).
        restrict = lambda g, spend: spend.get("kind") == "ability"  # noqa: E731
        t = t[:m.start()].strip()
    m = re.fullmatch(r"Add ((?:" + MANA_SYM + r")+)\.", t)
    if m:
        syms = tuple(re.findall(r"\{([WUBRGC])\}", m.group(1)))
        return ManaProduce([syms], restrict=restrict), None
    m = re.fullmatch(r"Add (" + MANA_SYM + r") or (" + MANA_SYM + r")\.", t)
    if m:
        return ManaProduce([(m.group(1)[1],), (m.group(2)[1],)], restrict=restrict), None
    m = re.fullmatch(r"Add (" + MANA_SYM + r"), (" + MANA_SYM + r"), or (" + MANA_SYM + r")\.", t)
    if m:
        return ManaProduce([(m.group(i)[1],) for i in (1, 2, 3)], restrict=restrict), None
    m = re.fullmatch(r"Add (one|two|three) mana of any (one )?color\.", t)
    if m:
        mp = ManaProduce(None, any_color=True, amount=num(m.group(1)), restrict=restrict, rider=rider)
        holder["mp"] = mp
        return mp, None
    if t.startswith("Add "):
        raise Unsupported(f"mana ability {t0!r}")
    return None


def _chosen_type_restriction(holder):
    """Cavern of Souls: "Spend this mana only to cast a creature spell of the chosen type, and
    that spell can't be countered" (CR 106.6). The chosen type is the producing permanent's
    (607.2d); the spend context doesn't name the mana's source, so the spender's permanents with
    this mana ability are used, failing loudly if they chose different types or are gone.
    The payment code names the producing permanent (`mana_source`), whose chosen type is the
    one (two Caverns of Souls with different types)."""
    def chosen(g, spend):
        src = spend.get("mana_source")
        o = g.s.objs.get(src) if src is not None else None
        if o is not None and o.chosen.get("type") is not None:
            return o.chosen["type"]
        c = spend.get("chars")
        p = c.controller if c is not None else None
        types = set()
        for o in g.s.battlefield:
            if g.ctrl(o) != p:
                continue
            if any(getattr(a, "mana", None) is holder.get("mp") for a in g.chars(o).abilities):
                t = g.s.objs[o].chosen.get("type")
                if t is not None:
                    types.add(t)
        if len(types) != 1:
            raise Unsupported("spend restriction 'of the chosen type' with no single source (CR 106.6)")
        return types.pop()

    def restrict(g, spend):
        if spend.get("kind") != "spell" or "Creature" not in spend["chars"].types:
            return False
        return chosen(g, spend) in spend["chars"].subtypes

    def rider(g, spend):
        obj = spend.get("obj")
        for it in g.s.stack:
            if it.kind == "spell" and it.obj == obj:
                it.uncounterable = True
    return restrict, rider


def parse_spend_restriction(t, cx):
    m = re.fullmatch(r"an? ([A-Z][a-z]+) spell", t)
    if m:
        st = m.group(1)
        return lambda g, spend, st=st: spend.get("kind") == "spell" and st in spend["chars"].subtypes
    m = re.fullmatch(r"an? ([A-Z][a-z]+) spell or an? ([A-Z][a-z]+) spell", t)
    if m:
        # CR 106.6: "a Dragon spell or an Omen spell" (Omen is a spell subtype, CR 205.3k).
        sts = {m.group(1), m.group(2)}
        return lambda g, spend, sts=sts: spend.get("kind") == "spell" and bool(sts & spend["chars"].subtypes)
    if t in ("creature spells", "a creature spell"):
        return lambda g, spend: spend.get("kind") == "spell" and "Creature" in spend["chars"].types
    m = re.fullmatch(r"an? (artifact|enchantment|planeswalker|battle) spell", t)
    if m:
        # CR 106.6: "Spend this mana only to cast an artifact spell" (Castle Doom).
        ct = m.group(1).capitalize()
        return lambda g, spend, ct=ct: spend.get("kind") == "spell" and ct in spend["chars"].types
    if t in ("instant and sorcery spells", "an instant or sorcery spell", "instant or sorcery spells"):
        return lambda g, spend: spend.get("kind") == "spell" and bool({"Instant", "Sorcery"} & spend["chars"].types)
    raise Unsupported(f"spend restriction {t!r}")


# =============================================================================== triggered

def compile_triggered(line, cx):
    trig, rest = tg.parse_trigger(line, cx)
    outer = getattr(cx, "trigger", None)  # a granted/emblem ability inside another ability
    cx.trigger = trig
    try:
        return _compile_triggered(trig, rest, line, cx)
    finally:
        cx.trigger = outer


def compile_modal_trigger(head, choose, bullets, line, cx):
    """"When ~ enters, choose one — • A • B": a triggered ability whose modes are chosen as it's
    put on the stack (CR 700.2b, 603.3c); each mode has its own targets (601.2c, 603.3d).
    "Choose one that hasn't been chosen this turn" (Monument to Endurance, The Vision, Lita):
    the modes this object's ability chose earlier this turn aren't available, and with none left
    the ability is removed from the stack (Modes.once_per_turn; casting.choose_modes). A mode's
    name before a dash ("Solar Beam — ...") is a flavor word (207.2d)."""
    trig, rest = tg.parse_trigger(head + ", X.", cx)
    if rest != "X.":
        raise Unsupported(f"modal trigger {line!r}")
    once = choose.endswith(" that hasn't been chosen this turn")
    if once:
        choose = choose[:-len(" that hasn't been chosen this turn")]
    outer = getattr(cx, "trigger", None)
    cx.trigger = trig
    try:
        modes = compile_modes(choose[0].upper() + choose[1:] + " —", bullets, cx)
    finally:
        cx.trigger = outer
    if once:
        if modes.min != 1 or modes.max != 1:
            raise Unsupported(f"modal trigger {line!r}")
        modes.once_per_turn = f"{cx.name}|{line}"
    return Triggered(trig, Noop(), [], text=line + " " + " ".join("• " + b for b in bullets),
                     zones=trig.zones, modes=modes)


def _compile_triggered(trig, rest, line, cx):
    condition = None
    m = re.match(r"^if (.+?), (.+)$", rest)
    if m and not rest.startswith("if you do"):
        cond = tg.parse_condition(m.group(1), cx)
        if cond is None:
            # CR 603.4: an intervening "if" must be checked as the ability triggers too, not
            # only on resolution.
            raise Unsupported(f"intervening-if condition {m.group(1)!r}")
        condition = cond
        rest = m.group(2)
        rest = rest[0].upper() + rest[1:]
    limit = None
    m = re.search(r"\s*This ability triggers only once each turn\.$", rest)
    if m:
        limit = 1
        rest = rest[:m.start()]
    optional = False
    eff, specs, modes = compile_effect(rest, cx, ability=True)
    zones = trig.zones
    if re.search(r"\b[Rr]eturn ~ from your graveyard\b", rest) and not (
            isinstance(trig, tg.Dies) and trig.self_):
        # CR 113.6m: an ability whose effect moves its object out of a zone functions only
        # in that zone.
        zones = ("graveyard",)
        if condition is not None:
            # CR 603.4: the intervening "if" is checked when the event occurs, in every zone
            # the ability functions in (the engine checks it there only for the battlefield).
            trig = tg.IfTrigger(trig, condition, zones)
    return Triggered(trig, eff, specs, optional=optional, condition=condition, text=line,
                     zones=zones, modes=modes, limit=limit)


# =============================================================================== spells

def compile_spell(spell_lines, cx):
    effects = []
    specs = []
    modes = None
    text = []
    statics = []
    tc = Targets()
    from .effects_text import SCtx
    sc = SCtx(cx, tc)  # pronouns refer across the lines of one spell ("that creature")
    for kind, payload in spell_lines:
        if kind == "modes":
            if modes is not None:
                raise Unsupported("two modal blocks")
            modes = payload
            continue
        eff, sp, md = compile_effect(payload, cx, tc=tc, sc=sc)
        if md is not None:
            raise Unsupported("nested modes")
        effects.append(eff)
        text.append(payload)
    specs = tc.specs
    if modes is not None and effects:
        # Text before/after the modes applies in addition (rare) - unsupported for clarity.
        raise Unsupported("modal spell with extra text")
    if modes is not None:
        return SpellAbility(Noop(), [], modes=modes, text="modal")
    return SpellAbility(Seq(*effects), specs, text=" ".join(text))


def compile_modes(header, bullets, cx):
    h = header.lower()
    spree = header.strip() == "Spree"
    # CR 702.183a: "Tiered" means "Choose one. As an additional cost to cast this spell, pay the
    # cost associated with that mode" (bullets "Name — {cost} — effect"; ref/cards/round2_c2.py
    # registers the header). Its mode costs work like spree's (casting: spree mode costs).
    tiered = header.strip() == "Tiered"
    if spree:
        mn, mx = 1, len(bullets)
    elif tiered:
        mn, mx = 1, 1
    elif "one or both" in h:
        mn, mx = 1, 2
    elif "one or more" in h:
        mn, mx = 1, len(bullets)
    elif "any number" in h:
        mn, mx = 0, len(bullets)
    elif "choose two" in h:
        mn, mx = 2, 2
    else:
        mn, mx = 1, 1
    modes = []
    for b in bullets:
        cost = []
        if spree:
            m = re.match(r"^\+ ((?:\{[^}]+\})+) — (.+)$", b)
            if not m:
                raise Unsupported(f"spree mode {b!r}")
            cost = parse_cost(m.group(1))
            b = m.group(2)
        elif tiered:
            m = re.match(r"^[A-Z][A-Za-z']*(?: [A-Za-z']+)* — ((?:\{[^}]+\})+) — (.+)$", b)
            if not m:
                raise Unsupported(f"tiered mode {b!r}")
            cost = [x for x in parse_cost(m.group(1)) if x != 0]
            b = m.group(2)
        eff, specs, md = compile_effect(b, cx)
        if md is not None:
            raise Unsupported("nested modes")
        modes.append(Mode(eff, specs, text=b, cost=cost))
    out = Modes(modes, mn, mx, spree=spree or tiered)
    out.tiered = tiered
    return out


# =============================================================================== effects

def compile_effect(text, cx, ability=False, tc=None, sc=None):
    """Effect text (one or more sentences) -> (Effect, [TargetSpec], Modes|None)."""
    from .effects_text import parse_sentences
    tc = tc or Targets()
    eff = parse_sentences(text, cx, tc, sc=sc)
    return eff, tc.specs, None


# =============================================================================== statics

def compile_static(line, cx):
    from .statics import parse_static
    return parse_static(line, cx)


class SacAnyNumber(fx.CostPart):
    """"As an additional cost to cast this spell, you may sacrifice any number of nonland
    permanents. This spell costs {1} less to cast for each permanent sacrificed this way."
    (CR 601.2b: the choice is announced; 601.2f: the total is reduced; 601.2h: they're
    sacrificed as the cost is paid, so they can't also make mana, 118.3). Only choices with
    which the total cost can still be paid are offered (one canonical order of picks)."""
    name = "sacrifice"
    reserves_many = True
    optional = True

    def __init__(self, filt, k):
        self.filt = filt
        self.k = k

    def _cands(self, g, p, oid):
        cs = [x for x in g.s.battlefield if g.ctrl(x) == p and x != oid
              and self.filt.match(g, x, oid, {"controller": p})]
        return sorted(cs, key=lambda x: (repr(g.obj_sig(x)), x))

    @staticmethod
    def sac_mana(g, x):
        """x has a mana ability that sacrifices it (a Treasure): chosen for this cost, it can't
        also make mana (CR 118.3: it can't be sacrificed twice). A chosen permanent with a
        {T} mana ability may still be tapped for mana: mana abilities are activated before the
        costs are paid (601.2g), the sacrifice is paid after (601.2h)."""
        return any(getattr(ab, "is_mana", False) and ab.cost.sac_self for ab in g.chars(x).abilities)

    def _payable(self, g, p, base, chosen, spend, reserved):
        return g.mana_payable(p, g.reduce_total(base, self.k * len(chosen)), 0, spend,
                              exclude={x for x in chosen if self.sac_mana(g, x)} | set(reserved))

    def _completable(self, g, p, oid, base, chosen, spend, reserved):
        """Can `chosen` be extended (with picks later in the canonical order) to a payable
        choice? The best extension adds every later candidate that keeps its mana (601.2g):
        sacrificing it only lowers the total. A Treasure-like one makes its mana instead (at
        least as good as {1} less)."""
        cands = self._cands(g, p, oid)
        last = max((cands.index(x) for x in chosen), default=-1)
        later = [x for i, x in enumerate(cands) if i > last]
        # (A Treasure that can't make mana for this spell now, e.g. reserved for this very
        # cost, is better sacrificed: as before.)
        srcs = {o for o, ab, n, prods in g.mana_sources(p)
                if prods and (ab.mana.restrict is None or ab.mana.restrict(g, spend))}
        best = set(chosen) | {x for x in later if not (self.sac_mana(g, x) and x in srcs)}
        return self._payable(g, p, base, best, spend, reserved)

    def castable(self, g, p, oid, cost, spend):
        return self._completable(g, p, oid, cost, set(), spend, set())

    def payable(self, g, p, oid):
        return True

    def pay(self, g, p, oid, ctx):
        return None

    def choose(self, g, p, oid, ctx, base, reserved):
        from ..engine.decisions import Decision, Opt, IllegalAction
        spend = {"kind": "spell", "chars": g.chars(oid), "obj": oid}
        chosen = []
        while True:
            cands = self._cands(g, p, oid)
            last = cands.index(chosen[-1]) if chosen else -1
            opts = []
            if self._payable(g, p, base, chosen, spend, reserved):
                opts.append(Opt("done", None))
            for i, x in enumerate(cands):
                if i <= last or x in chosen:
                    continue
                if self._completable(g, p, oid, base, chosen + [x], spend, reserved):
                    opts.append(Opt(f"choose:{g.ref(x)}", x, sig=("sacany", g.obj_sig(x))))
            if not opts:
                raise IllegalAction("the spell's total cost can't be paid")
            a = g.ask(Decision(p, "Sacrifice", opts, subject=g.ref(oid),
                               prompt="Choose a nonland permanent to sacrifice (or done)."))
            if a is None:
                break
            chosen.append(a)
        if not chosen:
            return None
        return _SacChosen(chosen, self.k * len(chosen), [x for x in chosen if self.sac_mana(g, x)])


class _SacChosen(fx.CostPart):
    name = "sacrifice"

    def __init__(self, oids, power, no_mana=()):
        self.oids = list(oids)
        self.power = power
        # The chosen permanents that can't also make mana (their mana ability sacrifices them);
        # the others may be tapped for mana first (CR 601.2g-h).
        self.no_mana = list(no_mana)

    def payable(self, g, p, oid):
        return all(x in g.s.objs and g.s.objs[x].zone == "battlefield" and g.ctrl(x) == p for x in self.oids)

    def pay(self, g, p, oid, ctx):
        if not self.payable(g, p, oid):
            from ..engine.decisions import IllegalAction
            raise IllegalAction("a permanent chosen to sacrifice is gone")
        for x in self.oids:
            ctx.setdefault("paid", {}).setdefault("sacrificed", []).append(g.chars(x))
        # Sacrificed together, as one cost (CR 701.21a): a "whenever you sacrifice an artifact"
        # ability of one of them looks back and sees the others leave (603.10a; Biotech
        # Specialist sacrificed with a Clue and a Lander for Rottenmouth Viper).
        g.sacrifice_batch(self.oids)


def parse_additional_cost(t, cx):
    """CR 118.8: additional costs of spells."""
    m = re.fullmatch(r"you may sacrifice any number of (.+?)\. ~ costs \{(\d+)\} less to cast for each permanent sacrificed this way", t)
    if m:
        return SacAnyNumber(parse_noun(m.group(1) + " you control"), int(m.group(2)))
    m = re.fullmatch(r"you may (.+)", t)
    if m:
        inner = parse_cost_alt(m.group(1), cx)
        name = inner.name
        return fx.ChoiceCost([inner], optional=True, label=lambda i, a, name=name: f"cost:{name}")
    alts = re.split(r" or (?=(?:pay|sacrifice|discard|exile|blight|tap)\b)", t)
    if len(alts) > 1:
        return fx.ChoiceCost([parse_cost_alt(a, cx) for a in alts], label=lambda i, a: f"cost:{a.name}")
    return parse_cost_alt(t, cx)


def parse_cost_alt(t, cx):
    t = t.strip()
    m = re.fullmatch(r"behold an? ([A-Z][a-z]+)", t)
    if m:
        from .recent import BeholdCost  # CR 701.4a
        return BeholdCost(m.group(1))
    m = re.fullmatch(r"collect evidence (\d+)", t)
    if m:
        from .recent import CollectEvidenceCost  # CR 701.59a
        return CollectEvidenceCost(int(m.group(1)))
    m = re.fullmatch(r"blight (\d+)", t)
    if m:
        return fx.BlightCost(int(m.group(1)))
    m = re.fullmatch(r"pay (\d+) life", t)
    if m:
        return fx.LifeCost(int(m.group(1)))
    m = re.fullmatch(r"pay ((?:\{[^}]+\})+)", t)
    if m:
        return fx.ManaCostPart(parse_cost(m.group(1)))
    m = re.fullmatch(r"discard (a|an|two) (.+)", t)
    if m:
        noun = m.group(2)
        f = None if noun == "card" else parse_noun(noun.replace(" card", ""))
        return fx.DiscardCost(f, num(m.group(1)))
    m = re.fullmatch(r"sacrifice (a|an|another) (.+)", t)
    if m:
        f = parse_noun(m.group(2) + " you control")
        if m.group(1) == "another":
            f.other = True
        return fx.SacCost(f, 1)
    raise Unsupported(f"additional cost {t!r}")


# Card modules register into the hooks above (KEYWORD_HOOKS ...); this order is the call order.
from . import recent  # noqa: E402,F401  (plot, warp, Sagas, ...)
from . import batch7  # noqa: E402,F401  (coverage batch 7: gift, paradigm, ...)
from . import batch8  # noqa: E402,F401  (coverage batch 8: stack-ability targets, web-slinging, Cases, ...)
from . import batch9a  # noqa: E402,F401  (coverage batch 9: firebending, airbend, split cards)
from . import batch9b  # noqa: E402,F401  (coverage batch 9: Garruk, High Noon, descend, offspring, ...)
from . import round2_a2  # noqa: E402,F401  (card-support round 2: conditions, life, counters, triggers)
from . import round2_b2  # noqa: E402,F401  (card-support round 2, list b: graveyard, library, exile, hand)
from . import round2_c2  # noqa: E402,F401  (card-support round 2: sneak, speed, station, craft, ...)
from . import round2_d2  # noqa: E402,F401  (card-support round 2: combat, Equipment, CDAs, ...)
from . import phase2_p3  # noqa: E402,F401  (EUT phase-2 scenarios: shield counters, each-target-player discards, ...)
