"""Effect sentences of Oracle text -> Effects.

Each sentence is matched by one pattern (first match wins). "target <noun>" allocates a target
word (CR 115.1) in reading order. Pronouns ("it", "that creature") refer to the most recent
object in the sentence context.
"""
import re

from ..engine import effects as fx
from ..engine.effects import Filter
from ..engine.ir import TargetSpec, Seq, Noop, Keyword
from ..engine.objects import ContEffect
from ..engine.mana import parse_cost
from .nouns import parse_noun, num, COLORS
from .model import token_def
from .. import Unsupported

KW = r"(flying|first strike|double strike|deathtouch|haste|hexproof|indestructible|lifelink|menace|reach|trample|vigilance)"


def split_sentences(text):
    out, cur, q = [], "", False
    for ch in text:
        cur += ch
        if ch == '"':
            q = not q
        if ch == "." and not q:
            out.append(cur.strip())
            cur = ""
    if cur.strip():
        out.append(cur.strip())
    return [s for s in out if s]


class SCtx:
    """Parse state across the sentences of one ability."""

    def __init__(self, cx, tc):
        self.cx = cx
        self.tc = tc
        self.last = None   # Sel of the most recently mentioned object(s) ("it")
        self.last_player = None
        # The triggering event's object, for "it" in a triggered ability with no earlier
        # antecedent (CR 603.10, 400.7: the ability can find the object it became).
        self.trigger = getattr(cx, "trigger", None)


MERGE_NEXT = (r"^If ~ was kicked, it deals \d+ damage instead\.$",
              r"^Otherwise, .+\.$",
              r"^It's still an? (land|artifact|enchantment)\.$",
              r"^If ~ was bargained, instead it deals \d+ damage to that (permanent|creature)( and you scry \d+)?\.$",
              r"^It's an? (enchantment|artifact)\.$",
              r"^If that spell is countered this way, exile it instead of putting it into its owner's graveyard\.$",
              r"^For each of those opponents who didn't discard a card with mana value \d+ or greater, draw a card\.$")
# A sentence that absorbs every later sentence of its ability (parse_sentences).
MERGE_REST = r"^(?:[Ll]ook at the top (?:\w+|X) cards? of your library\b|[Ll]ook at target player's hand\.)"
# (previous sentence, next sentence) regex pairs: the next sentence joins the previous one only
# after that sentence (ref/cards/batch7.py; sentences that form one instruction).
MERGE_PAIRS = [
    # "Target opponent reveals their hand. You choose a <card> from it. That player discards that
    # card. / Exile that card." is one instruction (later sentences, e.g. Cruelclaw's Heist's
    # "If the gift was promised, you may cast that card ...", are their own).
    (r"reveals their hand\.$", r"^You choose an? [a-z, -]+ card from it\.$"),
    (r"You choose an? [a-z, -]+ card from it\.$", r"^(?:Exile that card|That player discards that card)\.$"),
]


def parse_sentences(text, cx, tc, sc=None):
    sc = sc if sc is not None else SCtx(cx, tc)
    effs = []
    sents = split_sentences(text)
    merged = []
    for st in sents:
        if merged and (any(re.match(p, st) for p in MERGE_NEXT) or merged[-1].startswith("When you do, ")
                       or any(re.search(a, merged[-1]) and re.match(b, st) for a, b in MERGE_PAIRS)):
            # The sentences after "When you do, ..." belong to that reflexive ability (603.12).
            merged[-1] = merged[-1] + " " + st
        elif merged and re.match(MERGE_REST, merged[-1]):
            # "Look at the top N cards of your library. Put ... of them ...": the sentences after
            # it say what happens to the cards looked at (or to the revealed hand): one effect,
            # whose whole text must match a pattern (strict).
            merged[-1] = merged[-1] + " " + st
        else:
            merged.append(st)
    sents = merged
    i = 0
    while i < len(sents):
        s = sents[i]
        # "You may X. If you do, Y." -> May(X then Y)
        m = re.match(r"^[Yy]ou may (.+)\.$", s)
        if m and i + 1 < len(sents) and sents[i + 1].startswith("If you do, "):
            first = parse_one(m.group(1)[0].upper() + m.group(1)[1:] + ".", sc)
            then = parse_one(sents[i + 1][len("If you do, "):][0].upper() + sents[i + 1][len("If you do, ") + 1:], sc)
            effs.append(fx.MayThen(first, then, text=s))
            i += 2
            continue
        e = parse_one(s, sc)
        effs.append(e)
        i += 1
        if i < len(sents) and sents[i].startswith("If you do, "):
            # "X. If you do, Y." after an action that may not happen (CR 608.2c): only for
            # effects that record whether they happened.
            if not getattr(e, "reports_done", False):
                raise Unsupported(f"'If you do' after {s!r}")
            rest = sents[i][len("If you do, "):]
            effs.append(fx.IfYouDo(parse_one(rest[0].upper() + rest[1:], sc), text=sents[i]))
            i += 1
    return Seq(*effs) if len(effs) != 1 else effs[0]


def target_noun(np, sc, zone="battlefield"):
    """'target creature you control' etc. -> Sel (allocates a TargetSpec)."""
    np = np.strip()
    if np in ("any target",):
        return sc.tc.add(TargetSpec("any", text="any target"))
    if np in ("target player", "target opponent"):
        what = "player" if np == "target player" else "opponent"
        return sc.tc.add(TargetSpec(what, text=np))
    if np == "target player or planeswalker":
        return sc.tc.add(TargetSpec("player_or_pw", filt=Filter(types=["Planeswalker"]), text=np))
    m = re.match(r"^(up to (one|two|three) )?(other |another )?target (.+)$", np)
    if not m:
        raise Unsupported(f"target phrase {np!r}")
    upto = m.group(2)
    noun = m.group(4)
    if noun.endswith(" spell") or " spell " in noun or noun == "spell" or noun.startswith("spell "):
        f = parse_spell(noun)
        spec = TargetSpec("spell", filt=f, zone="stack", text=np)
    elif noun.endswith("card from your graveyard") or noun.endswith("card from a graveyard"):
        n2 = noun.replace(" from your graveyard", "").replace(" from a graveyard", "")
        f = parse_noun(n2.replace(" card", "")) if n2 != "card" else Filter(text="card")
        if noun.endswith("your graveyard"):
            f.owner = "you"
        spec = TargetSpec("card", filt=f, zone="graveyard", text=np)
    else:
        f = parse_noun(noun)
        spec = TargetSpec("object", filt=f, zone=zone, text=np)
        if "Creature" not in f.types and "Creature" not in f.any_types and not f.permanent and not f.types and not f.any_types and not f.subtypes:
            spec.what = "object"
    if m.group(3):
        f.other = True
    if upto:
        spec.n = (0, num(upto))
    return sc.tc.add(spec)


def parse_spell(noun):
    noun = noun.replace(" spell", "").strip()
    if noun.startswith("spell "):
        noun = noun[len("spell "):]
    if noun in ("", "spell"):
        return Filter(text="spell")
    if noun == "noncreature":
        return Filter(not_types=["Creature"], text=noun)
    if noun == "creature":
        return Filter(types=["Creature"], text=noun)
    if noun == "instant or sorcery":
        return Filter(any_types=["Instant", "Sorcery"], text=noun)
    m = re.fullmatch(r"with mana value (\d+)", noun)
    if m:
        return Filter(mv=("==", int(m.group(1))), text=noun)
    m = re.fullmatch(r"with mana value (\d+) or less", noun)
    if m:
        return Filter(mv=("<=", int(m.group(1))), text=noun)
    return parse_noun(noun)


def obj_ref(np, sc):
    """A noun phrase that names objects in an effect (not a new target)."""
    np = np.strip()
    if np in ("It", "That creature", "That permanent", "Them", "That card", "Those creatures"):
        np = np[0].lower() + np[1:]
    if np in ("~", "it", "this creature") and np == "~":
        return fx.self_permanent()
    if np in ("him", "her") and "Legendary" in sc.cx.face.supertypes:
        return fx.self_permanent()  # a legendary character's pronoun for itself (CR 201.5c)
    if np in ("it", "that creature", "that permanent", "them", "that card", "those creatures"):
        if sc.last is None:
            trig = trigger_obj(sc, np)
            if trig is not None:
                return trig
            raise Unsupported(f"pronoun {np!r} without antecedent")
        return sc.last
    if "target" in np.lower():
        sel = target_noun(np[0].lower() + np[1:], sc)
        sc.last = sel
        return sel
    if re.fullmatch(r"(?:other |attacking |blocking |tapped |untapped |nontoken )*creatures(?: you control| your opponents control)?", np.lower()):
        # "Creatures you control get ...": the affected set is locked as the effect begins
        # (CR 611.2c).
        return fx.all_matching(parse_noun(np[0].lower() + np[1:]))
    m = re.match(r"^(each|all) (.+)$", np)
    if m:
        return fx.all_matching(parse_noun(m.group(2)))
    raise Unsupported(f"object reference {np!r}")


def _tg():
    from . import triggers
    return triggers


def trigger_obj(sc, np="it"):
    """'it' in a triggered ability = the object of the triggering event (the new object it
    became, CR 400.7 exception for zone-change triggers), when the trigger names one."""
    tr = sc.trigger
    if tr is None or not getattr(tr, "has_obj", True):
        return None
    from ..cards import triggers as tg
    if isinstance(tr, (tg.Enters, tg.Dies, tg.Attacks, tg.EntersOrAttacks, tg.Blocks)):
        if np in ("it", "that creature", "that permanent", "that card"):
            return fx.event_obj("new")
    return None


def player_ref(w, sc):
    w = w.strip()
    if w in ("you", "You"):
        return fx.you()
    if w in ("each opponent", "Each opponent", "target opponent", "Target opponent", "target player", "Target player"):
        if w.lower().startswith("target"):
            sel = target_noun(w.lower(), sc)
            sc.last_player = sel
            return sel
        return fx.opponent()
    if w.lower() == "each player":
        return fx.each_player()
    if w in ("They", "they", "That player", "that player") and sc.last_player is None and sc.trigger is not None:
        return fx.trigger_player("player")  # the player of the triggering event
    if w.lower() in ("that player", "its controller"):
        if sc.last_player is not None:
            return sc.last_player
        if sc.last is not None:
            return fx.controller_of(sc.last)
    raise Unsupported(f"player reference {w!r}")


def amount_of(w, sc):
    w = w.strip()
    if w == "X":
        x = getattr(sc, "x_expr", None)
        if x is not None:
            sc.x_used = True
        return x if x is not None else "X"
    return num(w)


# AMOUNT_HOOKS fn(text, sc) -> Amount | None: card modules' amount expressions, tried first in
# parse_amount_expr (round2_c2; round2_d2: "the number of colors of mana spent to cast ~",
# "the number of lands you control", ...).
AMOUNT_HOOKS = []


def parse_amount_expr(t, sc):
    """'your life total', "~'s power", 'the number of <noun> you control' -> Amount."""
    t = t.strip()
    for h in AMOUNT_HOOKS:
        r = h(t, sc)
        if r is not None:
            return r
    if t == "your life total":
        return fx.Amount(lambda g, ctx: g.s.players[ctx["controller"]].life, t)
    if t == "its power" and (sc.last is not None or (sc.trigger is not None and getattr(sc.trigger, "self_", True) is False)):
        # "its" = the object "it" names: the last one mentioned, or the triggering event's object
        # when that isn't the source (Railway Brawler: "Whenever another creature you control
        # enters, put X +1/+1 counters on it, where X is its power", the entering creature's
        # power as the ability resolves, 608.2h). Gone from the battlefield: its last known
        # information.
        sel = sc.last if sc.last is not None else trigger_obj(sc, "it")
        if sel is not None:
            by_event = sc.last is None

            def pw_of(g, ctx, sel=sel, by_event=by_event):
                for o in sel.get(g, ctx) or []:
                    if isinstance(o, int) and o in g.s.objs and g.s.objs[o].zone == "battlefield":
                        return max(0, g.chars(o).power or 0)
                    lki = g.s.lki.get(o) if isinstance(o, int) else None
                    if lki is not None:
                        return max(0, lki.power or 0)
                if by_event:
                    c, _ = fx.event_obj_chars(g, ctx)  # it left the battlefield: as it last existed
                    return max(0, (c.power or 0)) if c is not None else 0
                return 0
            return fx.Amount(pw_of, t)
    if t in ("~'s power", "its power"):
        def pw(g, ctx):
            c = fx.self_chars(g, ctx)
            return max(0, c.power or 0) if c is not None else 0
        return fx.Amount(pw, t)
    if t == "the number of cards in your graveyard":
        return fx.Amount(lambda g, ctx: len(g.s.players[ctx["controller"]].graveyard), t)
    m = re.fullmatch(r"the number of (.+)", t)
    if m:
        return fx.count_amount(parse_noun(m.group(1)))
    raise Unsupported(f"amount {t!r}")


# =========================================================================== tokens

PREDEF = {
    "Treasure": dict(types=("Artifact",), subtypes=("Treasure",),
                     text="{T}, Sacrifice this token: Add one mana of any color."),
    "Food": dict(types=("Artifact",), subtypes=("Food",),
                 text="{2}, {T}, Sacrifice this token: You gain 3 life."),
    "Clue": dict(types=("Artifact",), subtypes=("Clue",),
                 text="{2}, Sacrifice this token: Draw a card."),
    "Map": dict(types=("Artifact",), subtypes=("Map",),
                text="{1}, {T}, Sacrifice this token: Target creature you control explores. Activate only as a sorcery."),
    "Blood": dict(types=("Artifact",), subtypes=("Blood",),
                  text="{1}, {T}, Discard a card, Sacrifice this token: Draw a card."),
    # CR 111.10u
    "Lander": dict(types=("Artifact",), subtypes=("Lander",),
                   text="{2}, {T}, Sacrifice this token: Search your library for a basic land card, put it onto the battlefield tapped, then shuffle."),
    # CR 111.10v
    "Mutagen": dict(types=("Artifact",), subtypes=("Mutagen",),
                    text="{1}, {T}, Sacrifice this token: Put a +1/+1 counter on target creature. Activate only as a sorcery."),
}


def predefined_token(name):
    """CR 111.10: predefined tokens."""
    d = PREDEF[name]
    return lambda d=d, name=name: token_def(name, d["types"], d["subtypes"], text=d["text"])


# TOKEN_HOOKS fn(desc) -> carddef factory | None: tried first in parse_token, with the leading
# article stripped (ref/cards/round2_c2.py: quoted abilities, named tokens, Heartwood, ...).
TOKEN_HOOKS = []


def parse_token(desc):
    """'2/2 black Zombie creature token with flying' -> carddef factory."""
    d = desc.strip()
    d = re.sub(r"^(a|an) ", "", d)
    for h in TOKEN_HOOKS:
        r = h(d)
        if r is not None:
            return r
    m = re.fullmatch(r"(Treasure|Food|Clue|Map|Blood|Lander|Mutagen) tokens?", d)
    if m:
        return predefined_token(m.group(1))
    m = re.fullmatch(r"(\d+)/(\d+) ((?:(?:white|blue|black|red|green|colorless)(?:,? and |, | )?)+)((?:[A-Z][a-z]+ )+)((?:artifact )?creature) tokens?(?: with (.+))?", d)
    if not m:
        raise Unsupported(f"token {desc!r}")
    p, t = int(m.group(1)), int(m.group(2))
    cols = [COLORS[c] for c in re.findall(r"white|blue|black|red|green", m.group(3))]
    subs = tuple(m.group(4).split())
    types = ("Artifact", "Creature") if m.group(5).startswith("artifact") else ("Creature",)
    text = ""
    if m.group(6):
        kws = m.group(6)
        if re.fullmatch(r"(flying|haste|vigilance|reach|trample|deathtouch|lifelink|menace|defender)(( and |, )(flying|haste|vigilance|reach|trample|deathtouch|lifelink|menace|defender))*", kws):
            text = ", ".join(k.capitalize() if i == 0 else k for i, k in enumerate(re.split(r" and |, ", kws)))
        else:
            raise Unsupported(f"token abilities {kws!r}")
    name = " ".join(subs)
    return lambda: token_def(name, types, subs, cols, p, t, text)


# =========================================================================== sentences

def _snap(sc):
    return (len(sc.tc.specs), sc.last, sc.last_player)


def _restore(sc, snap):
    del sc.tc.specs[snap[0]:]
    sc.last, sc.last_player = snap[1], snap[2]


# A second clause joined by "and" that is a sentence of its own ("Scry 1 and create a Treasure
# token", "tap target creature and put a stun counter on it").
CLAUSE_START = r"(you |draw |investigate|create |put |scry |surveil |it |untap |tap |that player |each opponent |target |return |exile |sacrifice |mill |discard )"


def parse_one(s, sc):
    s = s.strip()
    if s.startswith("Then "):
        s = s[len("Then "):]  # sequencing only: the sentences already happen in order
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    err = None
    for pat, fn in PATTERNS:
        m = re.fullmatch(pat, s)
        if m:
            snap = _snap(sc)
            try:
                return fn(m, sc)
            except Unsupported as e:
                _restore(sc, snap)
                err = e
                break
    conj = _parse_conjunction(s, sc)
    if conj is not None:
        return conj
    if err is not None:
        raise err
    raise Unsupported(f"effect: {s!r}")


def _parse_conjunction(s, sc):
    """"<sentence> and <sentence>." -> both, in order (each must parse on its own)."""
    if not s.endswith("."):
        return None
    for m in re.finditer(r",? and (?=" + CLAUSE_START + ")", s, re.I):
        left, right = s[:m.start()], s[m.end():]
        if not left or '"' in left:
            continue
        snap = _snap(sc)
        try:
            a = parse_one(left + ".", sc)
            b = parse_one(right, sc)
        except Unsupported:
            _restore(sc, snap)
            continue
        return Seq(a, b)
    return None


def _dmg(m, sc):
    n = amount_of(m.group(1), sc)
    to = m.group(2)
    if to == "each opponent":
        sel = fx.opponent()
    elif to == "each player":
        sel = fx.each_player()
    elif to in ("them", "that player") and sc.trigger is not None:
        sel = fx.trigger_player("player")
    elif to.startswith("each "):
        sel = fx.all_matching(parse_noun(to[5:]))
    elif to in ("you",):
        sel = fx.you()
    else:
        sel = target_noun(to, sc)
        sc.last = sel
    return fx.Damage(n, sel, text=m.group(0))


def _dmg_kicked(m, sc):
    base = amount_of(m.group(1), sc)
    sel = target_noun(m.group(2), sc)
    kicked = amount_of(m.group(3), sc)
    return fx.If(lambda g, ctx: ctx.get("kicked"), fx.Damage(kicked, sel), fx.Damage(base, sel), text=m.group(0))


def _draw(m, sc):
    return fx.Draw(amount_of(m.group(1), sc), text=m.group(0))


def _player_draws(m, sc):
    who = player_ref(m.group(1), sc)
    return fx.Draw(amount_of(m.group(2), sc), who=who, text=m.group(0))


def _gain(m, sc):
    return fx.GainLife(amount_of(m.group(1), sc), text=m.group(0))


def _loses(m, sc):
    who = player_ref(m.group(1), sc)
    return fx.LoseLife(amount_of(m.group(2), sc), who, text=m.group(0))


def _drain(m, sc):
    n1 = amount_of(m.group(1), sc)
    n2 = amount_of(m.group(2), sc)
    return Seq(fx.LoseLife(n1, fx.opponent()), fx.GainLife(n2))


def _destroy(m, sc):
    sel = obj_ref(m.group(1), sc)
    sc.last = sel
    return fx.Destroy(sel, text=m.group(0))


def _exile(m, sc):
    sel = obj_ref(m.group(1), sc)
    sc.last = sel
    link = "exiled" if getattr(sc.cx, "links_exile", False) else None
    return fx.MoveTo(sel, "exile", text=m.group(0), link=link)


def _bounce(m, sc):
    sel = obj_ref(m.group(1), sc)
    sc.last = sel
    return fx.MoveTo(sel, "hand", text=m.group(0))


def _counter(m, sc):
    sel = target_noun("target " + m.group(1), sc)
    return fx.CounterSpell(sel, text=m.group(0))


def _counter_unless(m, sc):
    sel = target_noun("target " + m.group(1), sc)
    cost = parse_cost(m.group(2))
    if "X" in cost:
        return fx.CounterUnlessPaysX(cost, 0, sel=sel)
    return fx.CounterUnlessPays(cost, 0, sel=sel)


def _pump(m, sc):
    sel = obj_ref(m.group(1), sc)
    sc.last = sel
    p, t = int(m.group(2)), int(m.group(3))
    kws = []
    if m.group(4):
        kws = [k.strip() for k in re.split(r",? and |, ", m.group(4))]
    return fx.Pump(sel, p, t, keywords=kws, text=m.group(0))


def _gains(m, sc):
    sel = obj_ref(m.group(1), sc)
    sc.last = sel
    kws = [k.strip() for k in re.split(r",? and |, ", m.group(2))]
    return fx.Pump(sel, None, None, keywords=kws, text=m.group(0))


def _counters(m, sc):
    n = amount_of(m.group(1), sc)
    kind = m.group(2)
    sel = obj_ref(m.group(3), sc)
    sc.last = sel
    return fx.PutCounters(sel, kind, n, text=m.group(0))


def _token(m, sc):
    n = 1 if m.group(1) in ("a", "an") else amount_of(m.group(1), sc)
    sc.last = fx.var("token")
    return fx.CreateToken(parse_token(m.group(2)), n, text=m.group(0), remember="token")


def _scry(m, sc):
    return fx.Scry(amount_of(m.group(1), sc), text=m.group(0))


def _surveil(m, sc):
    return fx.Scry(amount_of(m.group(1), sc), surveil=True, text=m.group(0))


def _mill(m, sc):
    who = fx.you() if not m.group(1) else player_ref(m.group(1), sc)
    n = 1 if m.group(2) == "a card" else amount_of(m.group(2).split()[0], sc)
    return fx.Mill(n, who, text=m.group(0))


def _tap(m, sc):
    sel = obj_ref(m.group(2), sc)
    sc.last = sel
    return fx.Tap(sel, untap=m.group(1) == "Untap", text=m.group(0))


def _search_basic_bf(m, sc):
    tapped = bool(m.group(2))
    f = parse_noun(m.group(1))
    return fx.Search(f, 1, "battlefield", tapped=tapped, text=m.group(0))


def _search_hand(m, sc):
    f = parse_noun(m.group(1).replace(" card", "")) if m.group(1) != "card" else Filter(text="card")
    return fx.Search(f, 1, "hand", text=m.group(0))


def _discard(m, sc):
    who = player_ref(m.group(1), sc) if m.group(1) else fx.you()
    n = amount_of(m.group(2), sc)
    return fx.Discard(n, who, chooser="self", text=m.group(0))


def _sacrifice(m, sc):
    who = player_ref(m.group(1), sc)
    f = parse_noun(m.group(3))
    return fx.Sacrifice(who, f, amount_of(m.group(2), sc), text=m.group(0))


def _fight(m, sc):
    a = obj_ref(m.group(1), sc)
    b = target_noun(m.group(2), sc)
    return fx.Fight(a, b, text=m.group(0))


def _cant_block(m, sc):
    sel = obj_ref(m.group(1), sc)
    sc.last = sel
    return fx.RuleEffect(sel, "cant_block", text=m.group(0))


def _cant_be_blocked(m, sc):
    sel = obj_ref(m.group(1), sc)
    sc.last = sel
    return fx.RuleEffect(sel, "unblockable", text=m.group(0))


def _return_from_gy(m, sc):
    sel = target_noun(m.group(1), sc)
    dest = "hand" if m.group(2) == "hand" else "battlefield"
    sc.last = sel
    return fx.MoveTo(sel, dest, text=m.group(0), controller="you")


def _exile_until(m, sc):
    sel = target_noun(m.group(1), sc)
    return fx.ExileUntilLeaves(sel, text=m.group(0))


def _add_mana(m, sc):
    syms = re.findall(r"\{([WUBRGC])\}", m.group(1))
    return fx.AddMana(syms, text=m.group(0))


def _look_top_hand(m, sc):
    n = num(m.group(1))
    k = num(m.group(2))
    rest = m.group(3)
    order = "any order" in (rest or "") or True
    return fx.Dig(n, k, rest_to="bottom", text=m.group(0))


PATTERNS = [
    (r"~ deals (\d+|X) damage to (any target|target .+?|up to (?:one|two|three) targets? .+?|each opponent|each .+?|them|that player)\.", _dmg),
    (r"~ deals (\d+) damage to (any target|target .+?)\. If ~ was kicked, it deals (\d+) damage instead\.", _dmg_kicked),
    (r"Draw (a|two|three|four|X) cards?\.", _draw),
    (r"You draw (a|two|three) cards?\.", _draw),
    (r"(Target player|Each player) draws (a|two|three) cards?\.", _player_draws),
    (r"You gain (\d+|X) life\.", _gain),
    (r"(Each opponent|Target opponent|Target player|You|They|That player) loses? (\d+|X) life\.", _loses),
    (r"Each opponent loses (\d+) life and you gain (\d+) life\.", _drain),
    (r"[Ee]xile (target .+?) until ~ leaves the battlefield\.", _exile_until),
    (r"[Ee]xile all (.+?) until ~ leaves the battlefield\.", lambda m, sc: fx.ExileUntilLeaves(fx.all_matching(parse_noun(m.group(1))), text=m.group(0))),
    (r"Destroy (target .+?|up to (?:one|two) target .+?|all .+?|each .+?)\.", _destroy),
    (r"Exile ((?:another |other )?target .+?|up to (?:one|two) (?:other )?target .+?)\.", _exile),
    (r"Return (target .+?|up to one other target .+?|up to one target .+?) to (?:its|their) owners?'s? hands?\.", _bounce),
    (r"Counter target (.+?) unless its controller pays ((?:\{[^}]+\})+)\.", _counter_unless),
    (r"Counter target (.+?)\.", _counter),
    (r"(Target .+?|~|Creatures you control|Other creatures you control|It) gets? \+(\d+)/\+(\d+)(?: and gains? (.+?))? until end of turn\.", _pump),
    (r"(Target .+?|~|Creatures you control|It) gets? (-\d+)/(-\d+) until end of turn\.", lambda m, sc: _pump_neg(m, sc)),
    (r"(Target .+?|~|Creatures you control|It|That creature) gains? ((?:flying|first strike|double strike|deathtouch|haste|hexproof|indestructible|lifelink|menace|reach|trample|vigilance)(?:(?:,? and |, )(?:flying|first strike|double strike|deathtouch|haste|hexproof|indestructible|lifelink|menace|reach|trample|vigilance))*) until end of turn\.", _gains),
    (r"Put (a|an|one|two|three|X) ([+\-]1/[+\-]1|stun|quest|oil|lore|shield) counters? on (target .+?|~|it|each creature you control)\.", _counters),
    (r"Create (a|an|one|two|three|X) (.+? tokens?)\.", _token),
    (r"(?:You )?[Ss]cry (\d+)\.", _scry),
    (r"Surveil (\d+)\.", _surveil),
    (r"(?:(Target player|Each opponent|Target opponent) )?[Mm]ills? (a card|\w+ cards)\.", _mill),
    (r"(Tap|Untap) (target .+?|up to (?:one|two|three) target .+?|~|it)\.", _tap),
    (r"Search your library for an? (basic land|land|basic Forest|Forest) card, put it onto the battlefield( tapped)?, then shuffle\.", _search_basic_bf),
    (r"Search your library for an? (.+?), reveal it, put it into your hand, then shuffle\.", _search_hand),
    (r"(?:(Target opponent|Each opponent|Target player|Each player) )?[Dd]iscards? (a|two) cards?\.", _discard),
    (r"(Each opponent|Target opponent|Target player|Each player) sacrifices? (a|an|one|two) (.+?)\.", _sacrifice),
    (r"(~|Target creature you control|It) fights (target .+?|up to one target .+?)\.", _fight),
    (r"(Target .+?|~) can't block this turn\.", _cant_block),
    (r"(Target .+?|~) can't be blocked this turn\.", _cant_be_blocked),
    (r"Return (target .+? card from your graveyard) to (?:your )?(hand|the battlefield)\.", _return_from_gy),
    (r"[Ee]xile (target .+?) until ~ leaves the battlefield\.", _exile_until),
    (r"Add ((?:\{[WUBRGC]\})+)\.", _add_mana),
]


def _pump_neg(m, sc):
    sel = obj_ref(m.group(1), sc)
    sc.last = sel
    return fx.Pump(sel, int(m.group(2)), int(m.group(3)), text=m.group(0))


# =========================================================================== more patterns

def _loot(m, sc):
    return Seq(fx.Draw(amount_of(m.group(1), sc)), fx.Discard(amount_of(m.group(2), sc), fx.you()))


def _draw_lose(m, sc):
    return Seq(fx.Draw(amount_of(m.group(1), sc)), fx.LoseLife(amount_of(m.group(2), sc), fx.you()))


def _pump_x(m, sc):
    sel = obj_ref(m.group(1), sc)
    sc.last = sel
    x = amount_of("X", sc)  # "where X is ..." binds X (otherwise the spell's X)
    return fx.Pump(sel, x, x, text=m.group(0))


def _counter_on(m, sc):
    n = amount_of(m.group(1), sc)
    kind = m.group(2)
    sel = obj_ref(m.group(3), sc)
    sc.last = sel
    # Hone counters (CR 122.1j) are applied in layer 7c (ref/engine/layers.py _pt_counters).
    return fx.PutCounters(sel, kind, n, text=m.group(0))


def _token2(m, sc):
    n = 1 if m.group(1) in ("a", "an") else amount_of(m.group(1), sc)
    sc.last = fx.var("token")
    desc = m.group(2)
    tapped = desc.startswith("tapped ")  # "create a tapped Treasure token" (CR 111.2)
    if tapped:
        desc = desc[len("tapped "):]
    return fx.CreateToken(parse_token(desc), n, tapped=tapped, text=m.group(0), remember="token")


def _copy_token(m, sc):
    if m.group(1) == "it" and sc.last is None and sc.trigger is not None and isinstance(sc.trigger, _tg().Dies):
        sel = fx.event_lki()  # copiable values as it last existed (CR 707.2, 608.2h)
    elif m.group(1) == "~":
        # The source's copiable values as it last existed if it left (113.7a, 608.2h; Improvised
        # Arsenal ruling 2026-01-27).
        sel = fx.self_lki()
    else:
        sel = obj_ref(m.group(1), sc)
    sc.last = fx.var("token")  # "it" now refers to the created token
    return fx.CopyToken(sel, text=m.group(0))


def _exile_x(m, sc):
    f = parse_noun(m.group(1))
    spec = TargetSpec("object", filt=f, zone="battlefield", n=(0, lambda g, ctx: ctx.get("x", 0)), text=m.group(0))
    spec.n = ("X", "X")
    sel = sc.tc.add(spec)
    return fx.MoveTo(sel, "exile", text=m.group(0))


def _prevent_next(m, sc):
    sel = target_noun(m.group(2), sc)
    return fx.PreventNext(sel, int(m.group(1)), text=m.group(0))


def _die_exile_instead(m, sc):
    if sc.last is None:
        raise Unsupported("die-exile-instead without antecedent")
    return fx.DieExileInstead(sc.last, text=m.group(0))


def _switch(m, sc):
    sel = target_noun("target " + m.group(1), sc)
    return fx.SwitchPT(sel, text=m.group(0))


def _loses_abilities(m, sc):
    sel = obj_ref(m.group(1), sc)
    sc.last = sel
    return fx.LoseAbilities(sel, duration="eot", text=m.group(0))


def _it_deals(m, sc):
    n = amount_of(m.group(1), sc)
    to = target_noun(m.group(2), sc)
    return fx.Damage(n, to, text=m.group(0))


def _drain_target(m, sc):
    who = player_ref(m.group(1), sc)
    return Seq(fx.LoseLife(amount_of(m.group(2), sc), who), fx.GainLife(amount_of(m.group(3), sc)))


EXTRA = [
    (r"Draw (a|two|three) cards?, then discard (a|two) cards?\.", _loot),
    (r"You draw (a|two) cards? and (?:you )?lose (\d+) life\.", _draw_lose),
    (r"Draw (a|two) cards? and lose (\d+) life\.", _draw_lose),
    (r"(Target .+?|~) gets \+X/\+X until end of turn\.", _pump_x),
    (r"Put (a|an|one|two|three|X) ([+\-]1/[+\-]1|stun|quest|oil|lore|shield|hone|page|conqueror|trample|flying|first strike|deathtouch|lifelink|vigilance|menace|reach|hexproof|indestructible|haste) counters? on (.+?)\.", _counter_on),
    (r"Create (a|an|one|two|three|four|X) (.+? tokens?(?: with .+?)?)\.", _token2),
    (r"Create a token that's a copy of (target .+?|~|it)\.", _copy_token),
    (r"Exile X target (.+?)\.", _exile_x),
    (r"Prevent the next (\d+) damage that would be dealt to (any target|target .+?) this turn\.", _prevent_next),
    (r"If (?:that creature|that creature or planeswalker|it) would die this turn, exile it instead\.", _die_exile_instead),
    (r"Switch (target .+?)'s power and toughness until end of turn\.", _switch),
    (r"(Target .+?|Up to one other target .+?|Up to one target .+?|It) loses all abilities until end of turn\.", _loses_abilities),
    (r"It deals (\d+|X) damage to (target .+?|any target)\.", _it_deals),
    (r"(Target opponent|Target player|Each opponent) loses (\d+) life and you gain (\d+) life\.", _drain_target),
]
PATTERNS[0:0] = EXTRA


def _if_additional_paid(m, sc):
    then = parse_one(m.group(1)[0].upper() + m.group(1)[1:] + ".", sc)
    return fx.If(lambda g, ctx: bool(ctx.get("paid_additional")), then, text=m.group(0))


def _counter_exile(m, sc):
    sel = target_noun("target " + m.group(1), sc)
    return fx.CounterSpell(sel, exile=True, text=m.group(0))


def _counter_unless_exile(m, sc):
    sel = target_noun("target " + m.group(1), sc)
    cost = parse_cost(m.group(2))
    if "X" in cost:
        return fx.CounterUnlessPaysX(cost, 0, sel=sel, exile=True)
    return fx.CounterUnlessPays(cost, 0, sel=sel, exile=True)


PATTERNS[0:0] = [
    (r"If ~'s additional cost was paid, (.+)\.", _if_additional_paid),
    (r"Counter target (.+?) unless its controller pays ((?:\{[^}]+\})+)\. If that spell is countered this way, exile it instead of putting it into its owner's graveyard\.", _counter_unless_exile),
    (r"Counter target (.+?)\. If that spell is countered this way, exile it instead of putting it into its owner's graveyard\.", _counter_exile),
]


# ---------------------------------------------------------------- delayed triggers (603.7)

def _at_next_end_step(eff_fn):
    def make(m, sc):
        inner = eff_fn(m, sc)
        return fx.DelayedAtNextEndStep(inner, text=m.group(0))
    return make


def _sac_it(m, sc):
    if sc.last is None:
        sel = fx.var("token")
    else:
        sel = sc.last
    return fx.CaptureThen(sel, lambda captured: fx.SacrificeObjs(captured))


def _return_exiled_cards(m, sc):
    return fx.CaptureThen(fx.var("exiled"), lambda captured: fx.ReturnObjs(captured, "battlefield"))


def _exile_that_token(m, sc):
    return fx.CaptureThen(fx.var("token"), lambda captured: fx.ExileObjs(captured))


def _treasures_equal_power(m, sc):
    if sc.last is None:
        src = fx.self_permanent()
    else:
        src = sc.last
    def power(g, ctx, src=src):
        objs = src.get(g, ctx)
        if objs:
            return max(0, g.chars(objs[0]).power or 0)
        lki = ctx.get("source_chars")  # 608.2h: last known information
        return max(0, (lki.power or 0)) if lki is not None else 0
    n = fx.Amount(power, "its power")
    return fx.CreateToken(predefined_token("Treasure"), n, tapped=bool(m.group(1)), text=m.group(0))


def _each_player_token(m, sc):
    return fx.CreateToken(parse_token(m.group(1)), 1, who=fx.each_player(), text=m.group(0))


def _earthbend(m, sc):
    spec = TargetSpec("object", filt=Filter(types=["Land"], controller="you", text="land you control"), text="target land you control")
    sel = sc.tc.add(spec)
    return fx.Earthbend(sel, int(m.group(1)), text=m.group(0))


def _becomes_type(m, sc):
    sel = obj_ref(m.group(1), sc)
    sub = m.group(2)
    return fx.AddEffect(sel, {4: (lambda g, oid, c, ce, sub=sub: c.subtypes.add(sub))}, duration="forever", text=m.group(0))


def _look_hand(m, sc):
    sel = target_noun("target opponent", sc)
    sc.last_player = sel
    return fx.Noop() if False else fx.LookAtHand(sel)


def _lose_half(m, sc):
    # "They": the player of the trigger (e.g. the player dealt combat damage); the damage event
    # itself has no 'player' field, so the trigger data is read first.
    who = player_ref(m.group(1), sc) if m.group(1) != "They" else fx.trigger_player("player")
    return fx.LoseHalfLife(who)


def _when_you_do(m, sc):
    # Reflexive trigger (603.12): created only if the preceding optional action happened.
    inner_tc = sc.tc
    body = m.group(1)
    cond = None
    mm = re.fullmatch(r"if (.+?), (.+)", body)
    if mm:
        from . import triggers as tg
        cond = tg.parse_condition(mm.group(1), sc.cx)
        if cond is None:
            raise Unsupported(f"reflexive condition {mm.group(1)!r}")
        body = mm.group(2)
    rsc = SCtxReflexive(sc)
    eff = parse_sentences(body[0].upper() + body[1:] + ".", sc.cx, rsc.tc, sc=rsc)
    return fx.Reflexive(eff, sc.reflexive_specs, text=m.group(0), condition=cond)


class SCtxReflexive(SCtx):
    def __init__(self, parent):
        from .compiler import Targets
        super().__init__(parent.cx, Targets())
        parent.reflexive_specs = self.tc.specs
        self.last = parent.last


def _search_basic_may(m, sc):
    return fx.May(fx.Search(parse_noun("basic land"), 1, "battlefield", tapped=True), text=m.group(0))


def _its_controller_may_search(m, sc):
    if sc.last is None:
        raise Unsupported("its controller without antecedent")
    return fx.ControllerMaySearchBasic(sc.last, text=m.group(0))


PATTERNS[0:0] = [
    (r"Sacrifice (?:it|that creature|that token|them) at the beginning of the next end step\.", _at_next_end_step(_sac_it)),
    (r"Return the exiled cards? to the battlefield under (?:their|its) owners?'s? control at the beginning of the next end step\.", _at_next_end_step(_return_exiled_cards)),
    (r"Exile (?:that token|them|it) at the beginning of the next end step\.", _at_next_end_step(_exile_that_token)),
    (r"Create a number of( tapped)? Treasure tokens equal to its power\.", _treasures_equal_power),
    (r"Each player creates (a .+? token)\.", _each_player_token),
    (r"Earthbend (\d+)\.", _earthbend),
    (r"(It|Target .+?) becomes an? ([A-Z][a-z]+) in addition to its other types\.", _becomes_type),
    (r"Look at target opponent's hand\.", _look_hand),
    (r"(They|Target opponent|Each opponent) loses? half (?:their|his or her) life, rounded up\.", _lose_half),
    (r"When you do, (.+)\.", _when_you_do),
    (r"You may search your library for a basic land card, put (?:it|that card) onto the battlefield tapped, then shuffle\.", _search_basic_may),
    (r"Its controller may search their library for a basic land card, put it onto the battlefield tapped, then shuffle\.", _its_controller_may_search),
]


def _explore(m, sc):
    who = m.group(1)
    if who == "~":
        sel = fx.self_permanent()
    else:
        sel = obj_ref(who, sc)
    return fx.Explore(sel, text=m.group(0))


PATTERNS[0:0] = [
    (r"(~|Target creature you control|It) explores\.", _explore),
]


# =========================================================================== keyword actions, costs

def _blight(m, sc):
    return fx.Blight(amount_of(m.group(1), sc), text=m.group(0))


def _pay_mana(m, sc):
    return fx.PayMana(parse_cost(m.group(1)), text=m.group(0))


PATTERNS[0:0] = [
    (r"Blight (\d+)\.", _blight),
    (r"Pay ((?:\{[^}]+\})+)\.", _pay_mana),
]


def _cant_be_blocked_except(m, sc):
    """"~ can't be blocked this turn except by <creatures>" (CR 509.1b restriction)."""
    sel = obj_ref(m.group(1), sc)
    sc.last = sel
    f = parse_noun(m.group(2))
    return fx.RuleEffect(sel, "cant_be_blocked_by", text=m.group(0),
                         filter=lambda g, b, a, f=f: not f.match(g, b, a))


PATTERNS[0:0] = [
    (r"(Target .+?|~|It) can't be blocked this turn except by (.+?)\.", _cant_be_blocked_except),
]


def _flicker(m, sc):
    sel = obj_ref(m.group(1), sc)
    counters = {}
    if m.group(3):
        counters[m.group(4)] = num(m.group(3))
    sc.last = fx.var("returned")
    return fx.Flicker(sel, controller="you" if m.group(2) == "your" else "owner", counters=counters,
                      text=m.group(0))


PATTERNS[0:0] = [
    (r"Exile (target .+?|~), then return (?:that card|it) to the battlefield under (your|its owner's) control(?: with (a|an|one|two) ([+\-]1/[+\-]1) counters? on it)?\.", _flicker),
]


# =========================================================================== conditionals

def parse_effect_condition(t, sc):
    """A condition checked as the effect happens (CR 608.2c): predicate(g, ctx)."""
    from . import triggers as tg
    m = re.fullmatch(r"(?:it|that card) was an? (.+?) card", t)
    if m and sc.last is not None:
        f = parse_noun(m.group(1))
        sel = sc.last
        # The object as it last existed in its previous zone (608.2h).
        return lambda g, ctx, f=f, sel=sel: any(
            f.match_chars(g, g.chars(x), g.s.lki_obj.get(x) or g.s.objs.get(x), ctx.get("source"), ctx)
            for x in objs_of_any(sel, g, ctx))
    m = re.fullmatch(r"~ (is|isn't) an? (creature|land|artifact|enchantment)", t)
    if m:
        want = m.group(1) == "is"
        ty = m.group(2).capitalize()
        return lambda g, ctx, want=want, ty=ty: (ctx.get("source") in g.s.objs and g.s.objs[ctx["source"]].zone == "battlefield"
                                                 and (ty in g.chars(ctx["source"]).types) == want)
    c = tg.parse_condition(t, sc.cx)
    if c is not None:
        return c
    raise Unsupported(f"condition {t!r}")


def objs_of_any(sel, g, ctx):
    return [r for r in sel.get(g, ctx) if not isinstance(r, tuple) and (r in g.s.objs or r in g.s.lki)]


def _if_then(m, sc):
    cond = parse_effect_condition(m.group(1), sc)
    rest = m.group(2)
    if m.group(1).startswith("~ ") and sc.last is None:
        sc.last = fx.self_permanent()  # "If ~ isn't a creature, it becomes ..."
    then = parse_one(rest[0].upper() + rest[1:] + ".", sc)
    return fx.If(cond, then, text=m.group(0))


PATTERNS.append((r"If (.+?), (.+)\.", _if_then))


def _return_to_bf(m, sc):
    who = m.group(1)
    if who == "~":
        sel = fx.self_sel()
    else:
        sel = obj_ref(who, sc)
    ctrl = {"your": "you", "its owner's": "owner", None: "you"}[m.group(4)]
    counters = {}
    if m.group(5):
        counters[m.group(6)] = num(m.group(5))
    sc.last = fx.var("returned")
    return fx.PutOntoBattlefield(sel, ("graveyard",), controller=ctrl, tapped=bool(m.group(2)),
                                 counters=counters, text=m.group(0), transformed=bool(m.group(3)))


def _until_eot_prefix(m, sc):
    return parse_one(m.group(1)[0].upper() + m.group(1)[1:] + " until end of turn.", sc)


def _investigate(m, sc):
    """CR 701.16a: create a Clue token."""
    return fx.CreateToken(predefined_token("Clue"), 1, text=m.group(0))


def _loses_that_much(m, sc):
    who = player_ref(m.group(1), sc)
    return fx.LoseLife(fx.event_amount(), who, text=m.group(0))


def _shuffle_into_library(m, sc):
    a = obj_ref(m.group(1), sc)
    b = obj_ref(m.group(2), sc)
    return fx.ShuffleIntoLibrary([a, b], text=m.group(0))


PATTERNS[0:0] = [
    (r"Return (it|~|that card) (?:from your graveyard )?to the battlefield( tapped)?( and transformed| transformed)?(?: under (your|its owner's) control)?(?: with (a|an|one|two|three) ([+\-]1/[+\-]1|stun|shield) counters? on it)?\.", _return_to_bf),
    (r"Until end of turn, (.+)\.", _until_eot_prefix),
    (r"Investigate\.", _investigate),
    (r"(Target opponent|Each opponent|Target player|That player) loses that much life\.", _loses_that_much),
    (r"Shuffle (~) and (target .+?) into their owners' libraries\.", _shuffle_into_library),
]


def _return_its_a(m, sc):
    eff = parse_one(m.group(1), sc)
    if not isinstance(eff, fx.PutOntoBattlefield):
        raise Unsupported(f"'It's a ...' after {m.group(1)!r}")
    t = {"enchantment": "Enchantment", "artifact": "Artifact"}[m.group(2)]

    def typ(g, oid, c, ce, t=t):
        fx.set_card_types(c, {t})  # CR 205.1a
    eff.enters_as = {4: typ}
    return eff


PATTERNS[0:0] = [
    (r"(Return .+?\.) It's an? (enchantment|artifact)\.", _return_its_a),
]


# =========================================================================== prevention (615)

def _fog(m, sc):
    """"Prevent all [combat] damage that would be dealt this turn" (CR 615.1a): a prevention
    shield for the rest of the turn."""
    kind = (m.group(1) or "").strip() or None
    from .statics import prevention_part
    part = prevention_part(kind, lambda g, tgt, src: True, m.group(0))
    return fx.AddEffect(None, {}, duration="eot", kind="replacement", text=m.group(0),
                        data={"event": "damage", "part": part})


def _cant_be_prevented_this_turn(m, sc):
    """CR 615.12: "Damage can't be prevented this turn" (a rule effect for the turn)."""
    from ..engine.ir import RulePart
    f = (lambda g, ev: bool(ev.get("combat"))) if m.group(1) else None
    return fx.AddEffect(None, {}, duration="eot", kind="rule", text=m.group(0),
                        data={"rule": "damage_cant_be_prevented",
                              "part": RulePart("damage_cant_be_prevented", None, filter=f)})


PATTERNS[0:0] = [
    (r"Prevent all (combat )?damage that would be dealt this turn\.", _fog),
    (r"(Combat )?[Dd]amage can't be prevented this turn\.", _cant_be_prevented_this_turn),
]


def _remove_counters_upto(m, sc):
    sel = obj_ref(m.group(2), sc)
    sc.last = sel
    return fx.RemoveCountersUpTo(sel, num(m.group(1)), text=m.group(0))


PATTERNS[0:0] = [
    (r"Remove up to (one|two|three|four|five) counters from (target .+?|~|it)\.", _remove_counters_upto),
]


def _where_x(m, sc):
    expr = parse_amount_expr(m.group(2), sc)
    old = getattr(sc, "x_expr", None)
    old_used = getattr(sc, "x_used", False)
    sc.x_expr = expr
    sc.x_used = False
    try:
        eff = parse_one(m.group(1) + ".", sc)
        if not sc.x_used:
            raise Unsupported(f"'where X is' not bound in {m.group(1)!r}")
        return eff
    finally:
        sc.x_expr = old
        sc.x_used = old_used


PATTERNS[0:0] = [
    (r"(.+?), where X is (.+)\.", _where_x),
]


# =========================================================================== base P/T (613.4b)

def _base_pt(m, sc):
    sel = obj_ref(m.group(1), sc)
    sc.last = sel
    return fx.Pump(sel, None, None, set_pt=(int(m.group(2)), int(m.group(3))), text=m.group(0))


def _bargained_dmg(m, sc):
    """Torch the Tower: "~ deals N damage to <target>. If this spell was bargained, instead it
    deals M damage to that permanent and you scry 1." The bargain clause is a self-replacement
    (CR 614.15), applied before other replacement effects: deal M instead of N."""
    sel = target_noun(m.group(2), sc)
    sc.last = sel
    base = fx.DamageRecord(int(m.group(1)), sel)
    bar = fx.DamageRecord(int(m.group(3)), sel)
    extra = parse_one(m.group(4)[0].upper() + m.group(4)[1:] + ".", sc) if m.group(4) else None
    then = Seq(bar, extra) if extra is not None else bar
    return fx.If(lambda g, ctx: "bargain" in (ctx.get("paid_additional") or []), then, base, text=m.group(0))


def _dealt_damage_exile(m, sc):
    return fx.DieExileInstead(fx.var("damaged"), text=m.group(0))


PATTERNS[0:0] = [
    (r"(Target .+?|~|It) has base power and toughness (\d+)/(\d+) until end of turn\.", _base_pt),
    (r"~ deals (\d+) damage to (target .+?)\. If ~ was bargained, instead it deals (\d+) damage to that (?:permanent|creature)(?: and (you scry \d+))?\.", _bargained_dmg),
    (r"If a permanent dealt damage by ~ would die this turn, exile it instead\.", _dealt_damage_exile),
]


# =========================================================================== ability loss (613.1f)

def _loses_all(m, sc):
    who = m.group(1)
    dur = {"until end of turn": "eot", "for as long as ~ remains on the battlefield": "while_host",
           None: "forever"}[m.group(2)]
    if re.match(r"(?i)^(each|all) ", who):
        f = parse_noun(re.sub(r"(?i)^(each|all) ", "", who))
        sel = fx.var("those")
        sc.last = sel
        return Seq(fx.Remember(fx.all_matching(f), "those"), fx.LoseAllAbilities(sel, dur, text=m.group(0)))
    sel = obj_ref(who[0].lower() + who[1:] if who.lower().startswith(("up to", "target", "other", "another")) else who, sc)
    sc.last = sel
    return fx.LoseAllAbilities(sel, dur, text=m.group(0))


def _put_kw_counters(m, sc):
    kinds = re.findall(r"an? ([a-z +\-/1]+?) counter", m.group(1))
    sel = obj_ref(m.group(2), sc)
    sc.last = sel
    return fx.PutCountersMulti(sel, [(k, 1) for k in kinds], text=m.group(0))


PATTERNS[0:0] = [
    (r"(Target .+?|Up to one (?:other )?target .+?|It|Each .+?|All .+?) loses? all abilities(?: (until end of turn|for as long as ~ remains on the battlefield))?\.", _loses_all),
    (r"Put ((?:an? [a-z ]+? counter, )+and an? [a-z ]+? counter) on (it|that creature|target .+?|~)\.", _put_kw_counters),
    (r"Destroy (those creatures|them|it|that creature)\.", _destroy),
]


# =========================================================================== linked exile (607)

def _exiled_with_to_gy_tokens(m, sc):
    move = fx.MoveCountTo(fx.exiled_with_source(), "graveyard", "put_this_way")
    n = fx.Amount(lambda g, ctx: len((ctx.get("vars") or {}).get("put_this_way") or []), "cards put this way")
    tok = fx.CreateToken(parse_token(m.group(1)), n, text=m.group(0), remember="token")
    return Seq(move, tok)


PATTERNS[0:0] = [
    (r"Put each card exiled with ~ into its owner's graveyard, then create an? (.+? token) for each card put into a graveyard this way\.", _exiled_with_to_gy_tokens),
    (r"Sacrifice ~\.", lambda m, sc: fx.SacrificeSelf(text=m.group(0))),
    (r"Exile ~\.", lambda m, sc: fx.ExileSelf(text=m.group(0))),
]


# =========================================================================== "becomes" (613.1d-f)

def parse_becomes(desc, sc):
    """'a 3/3 creature with vigilance and all creature types', 'a 2/4 Wizard creature with
    "<ability>"' -> layer parts {layer: fn(g, oid, c, ce)} (the object keeps its other types:
    callers require "It's still a ..." / "in addition to its other types", CR 205.1b)."""
    m = re.fullmatch(r"an? (\d+)/(\d+) ((?:[A-Z][a-z]+ )*)(artifact )?creature(?: with (.+))?", desc)
    if not m:
        raise Unsupported(f"becomes {desc!r}")
    p, t = int(m.group(1)), int(m.group(2))
    subs = m.group(3).split()
    types = {"Creature"} | ({"Artifact"} if m.group(4) else set())
    kws, granted, all_types = [], [], False
    rest = m.group(5)
    if rest:
        q = re.fullmatch(r'"(.+)"', rest)
        if q:
            from .compiler import compile_line
            inner = q.group(1)
            res = compile_line(inner, sc.cx)
            if res == "spell" or not res:
                raise Unsupported(f"granted ability {inner!r}")
            granted = list(res)
        else:
            parts = [x.strip() for x in re.split(r",? and |, ", rest)]
            for x in parts:
                if x == "all creature types":
                    all_types = True
                elif re.fullmatch(KW, x):
                    kws.append(x)
                else:
                    raise Unsupported(f"becomes with {x!r}")
    from .compiler import _CREATURE_TYPES

    def typ(g, oid, c, ce, types=types, subs=subs, all_types=all_types):
        c.types |= types
        c.subtypes |= set(subs)
        if all_types:
            c.subtypes |= set(_CREATURE_TYPES)

    def pt(g, oid, c, ce, p=p, t=t):
        c.power, c.toughness = p, t
    parts = {4: typ, "7b": pt}
    abil = [Keyword(k) for k in kws] + granted
    if abil:
        def grant(g, oid, c, ce, abil=tuple(abil)):
            c.abilities.extend(abil)
        parts[6] = grant
    return parts


def _becomes_still(m, sc):
    who = m.group(1)
    sel = fx.self_permanent() if who == "~" else obj_ref(who[0].lower() + who[1:] if who in ("It",) else who, sc)
    sc.last = sel
    parts = parse_becomes(m.group(2), sc)
    dur = "eot" if m.group(3) else "forever"
    return fx.AddEffect(sel, parts, duration=dur, text=m.group(0))


PATTERNS[0:0] = [
    (r"(~|It|Target .+?) becomes (an? \d+/\d+ .+?)( until end of turn)?(?:\.|(?<=\"))\s*It's still an? (?:land|artifact|enchantment)\.", _becomes_still),
]


def _exile_all_graveyards(m, sc):
    """Exile every card in every graveyard (one simultaneous event)."""
    def fn(g, ctx):
        return [o for p in (0, 1) for o in g.s.players[p].graveyard]
    return fx.MoveTo(fx.Sel(fn, "all graveyards", single=False), "exile", text=m.group(0))


PATTERNS[0:0] = [
    (r"Exile all graveyards\.", _exile_all_graveyards),
]



PLURAL_CREATURES = r"(?:Other |Attacking |Blocking )?(?:attacking |blocking )?[Cc]reatures(?: you control| your opponents control)?"


def _pump_loses_types(m, sc):
    sel = obj_ref(m.group(1), sc)
    sc.last = sel
    from .compiler import subtypes_of

    def lose(g, oid, c, ce):
        c.subtypes -= subtypes_of("Creature")
    return Seq(fx.Pump(sel, int(m.group(2)), int(m.group(3)), text=m.group(0)),
               fx.AddEffect(sel, {4: lose}, duration="eot", text=m.group(0)))


def _may_simple(m, sc):
    inner = parse_one(m.group(1)[0].upper() + m.group(1)[1:] + ".", sc)
    return fx.MayThen(inner, None, text=m.group(0))


def _gains_plural(m, sc):
    sel = obj_ref(m.group(1), sc)
    kws = [k.strip() for k in re.split(r",? and |, ", m.group(2))]
    return fx.Pump(sel, None, None, keywords=kws, text=m.group(0))


def _pump_plural(m, sc):
    sel = obj_ref(m.group(1), sc)
    kws = [k.strip() for k in re.split(r",? and |, ", m.group(4))] if m.group(4) else []
    return fx.Pump(sel, int(m.group(2)), int(m.group(3)), keywords=kws, text=m.group(0))


def _same_name(m, sc):
    sel = target_noun(m.group(1), sc)
    sc.last = sel
    return fx.DestroyWithSameName(sel, text=m.group(0))


def _gain_control(m, sc):
    who = player_ref(m.group(1), sc)
    sel = target_noun(m.group(2), sc)
    sc.last = sel
    return fx.GainControl(sel, who, text=m.group(0))


def _dmg_each_player_count(m, sc):
    return fx.DamageEachPlayerCount(parse_noun(m.group(1)), text=m.group(0))


PATTERNS[0:0] = [
    (r"(Target .+?) gets ([+\-]\d+)/([+\-]\d+) and loses all creature types until end of turn\.", _pump_loses_types),
    (r"(" + PLURAL_CREATURES + r") gain ((?:flying|first strike|double strike|deathtouch|haste|hexproof|indestructible|lifelink|menace|reach|trample|vigilance)(?:(?:,? and |, )(?:flying|first strike|double strike|deathtouch|haste|hexproof|indestructible|lifelink|menace|reach|trample|vigilance))*) until end of turn\.", _gains_plural),
    (r"(" + PLURAL_CREATURES + r") get \+(\d+)/\+(\d+)(?: and gain (.+?))? until end of turn\.", _pump_plural),
    (r"Destroy (target .+?) and all other permanents with the same name as that permanent\.", _same_name),
    (r"(Target opponent|Target player) gains control of (target .+?)\.", _gain_control),
    (r"(?:~|It) deals damage to each player equal to the number of (.+?) that player controls\.", _dmg_each_player_count),
]
PATTERNS.append((r"[Yy]ou may (.+)\.", _may_simple))



# =========================================================================== face-down (708)

def _manifest_dread(m, sc):
    return fx.Manifest(dread=True, text=m.group(0))


def _if_controlled_return(m, sc):
    """"If you controlled it, return it to the battlefield tapped. Otherwise, its controller
    creates <token>." after exiling a target: 'it' is the exiled object (LKI for who controlled
    it, CR 608.2h; the card in exile for the return, 400.7)."""
    tgt = sc.last
    if tgt is None:
        raise Unsupported("'If you controlled it' without antecedent")

    def lki_ctrl(g, ctx, tgt=tgt):
        for x in tgt.get(g, ctx):
            if isinstance(x, tuple):
                continue
            c = g.s.lki.get(x) if x not in g.s.objs else g.chars(x)
            if c is not None:
                return c.controller
        return None
    back = fx.PutOntoBattlefield(fx.var("exiled"), ("exile",), controller="you", tapped=bool(m.group(1)))
    tok = parse_token(m.group(2))
    who = fx.Sel(lambda g, ctx: ([("player", lki_ctrl(g, ctx))] if lki_ctrl(g, ctx) is not None else []), "its controller")
    other = fx.CreateToken(tok, 1, who=who, text=m.group(0))
    return fx.If(lambda g, ctx: lki_ctrl(g, ctx) == ctx["controller"], back, other, text=m.group(0))


PATTERNS[0:0] = [
    (r"Manifest dread\.", _manifest_dread),
    (r"If you controlled it, return it to the battlefield( tapped)?\. Otherwise, its controller creates (an? .+? token)\.", _if_controlled_return),
]


PATTERNS[0:0] = [
    (r"Transform ~\.", lambda m, sc: fx.Transform(fx.self_permanent(), text=m.group(0))),
]



def _emblem(m, sc):
    from .compiler import compile_line
    inner = m.group(1)
    res = compile_line(inner, sc.cx)
    if res == "spell" or not res:
        raise Unsupported(f"emblem ability {inner!r}")
    return fx.Emblem(res, text=m.group(0))


def _exile_from_hand_until(m, sc):
    who = sc.last_player
    if who is None:
        raise Unsupported("exile a card from 'it' without a player")
    noun = m.group(1)
    f = Filter(text="card") if noun == "card" else parse_noun(noun.replace(" card", ""))
    return fx.ExileFromHandUntil(who, f, optional=True, text=m.group(0))


PATTERNS[0:0] = [
    (r'You get an emblem with "(.+)"\.?', _emblem),
    (r"You may exile an? (.+?card) from it until ~ leaves the battlefield\.", _exile_from_hand_until),
]



def _token_then_counters(m, sc):
    tok = fx.CreateToken(parse_token(m.group(1)), 1, text=m.group(0), remember="token")
    sc.last = fx.var("token")
    return Seq(tok, fx.PutLKICountersOn(fx.var("token"), text=m.group(0)))


PATTERNS[0:0] = [
    (r"Create (an? .+? token), then put ~'s counters on that token\.", _token_then_counters),
]



def _discard_draw_per(m, sc):
    who = sc.tc.add(TargetSpec("opponent", n=(0, 99), text="any number of target opponents"))
    return fx.DiscardThenDrawPer(who, int(m.group(1)), text=m.group(0))


PATTERNS.insert(0, (r"Any number of target opponents each discard a card\. For each of those opponents who didn't discard a card with mana value (\d+) or greater, draw a card\.", _discard_draw_per))


# =========================================================================== libraries, hands (card push s)
# "Look at the top N cards of your library" (CR 701.20e: looking follows the rules for revealing,
# shown only to that player; 401.2: the library stays hidden to everyone else). The cards stay in
# the library while looked at; the rest go to the bottom "in any order" (the player orders them,
# CR 401.4) or "in a random order".

def _dig_rest(m_order):
    return "random" if m_order == "a random" else "any"


def _dig(m, sc):
    """"Look at the top N cards of your library. Put K of them into your hand and the rest on the
    bottom of your library in any order." (Stock Up)"""
    return fx.Dig(num(m.group(1)), num(m.group(2)), order=_dig_rest(m.group(3)), text=m.group(0))


def _dig_other(m, sc):
    """Sleight of Hand: "Look at the top two cards ... Put one of them into your hand and the other
    on the bottom of your library." (one card left: no order to choose)."""
    return fx.Dig(2, 1, order="any", text=m.group(0))


def _dig_instead(m, sc):
    """Flow State: "... Put one of them into your hand and the rest on the bottom ... If there is
    an instant card and a sorcery card in your graveyard, instead put two of them into your hand
    and the rest on the bottom ..." The condition is checked as the spell resolves (CR 608.2c,
    608.2h); the spell itself is on the stack then, not in the graveyard (608.2n)."""
    n, k1, k2 = num(m.group(1)), num(m.group(2)), num(m.group(3))

    def k(g, ctx, k1=k1, k2=k2):
        gy = g.s.players[ctx["controller"]].graveyard
        inst = any("Instant" in g.chars(o).types for o in gy)
        sorc = any("Sorcery" in g.chars(o).types for o in gy)
        return k2 if inst and sorc else k1
    return fx.Dig(n, fx.Amount(k, "one, or two with an instant and a sorcery card in your graveyard"),
                  order="any", text=m.group(0))


def _dig_x_kicked(m, sc):
    """Consult the Star Charts: "Look at the top X cards of your library, where X is <amount>. Put
    one of those cards into your hand. If this spell was kicked, put two of those cards into your
    hand instead. Put the rest on the bottom of your library in a random order." (kicker: CR
    702.33d, "if this spell was kicked")."""
    x = parse_amount_expr(m.group(1), sc)
    k = fx.Amount(lambda g, ctx: 2 if ctx.get("kicked") else 1, "1, or 2 if kicked")
    return fx.Dig(x, k, order="random", text=m.group(0))


def _break_out(m, sc):
    return fx.Dig(num(m.group(1)), 0, order="random", reveal_creature=fx.break_out_reveal, text=m.group(0))


def _reveal_choose(m, sc):
    """"Target opponent reveals their hand. You choose a(n) <card> from it. That player discards
    that card. / Exile that card." (Duress, Intimidation Tactics, Null Summoner, Cruelclaw's
    Heist; CR 701.20a, 701.9b). A permanent's exile is linked to it (607.2a: Null Summoner's
    "the exiled card"); "that card" later in the text is the exiled card (var exiled)."""
    who = target_noun("target opponent", sc)
    sc.last_player = who
    f = _hand_card_filter(m.group(1))
    if m.group(2) == "That player discards that card":
        return fx.ChooseFromHand(who, f, "discard", kind="Discard", text=m.group(0))
    sc.last = fx.var("exiled")
    return fx.ChooseFromHand(who, f, "exile", kind="Search", link=sc.cx.is_permanent, text=m.group(0))


def _hand_card_filter(noun):
    noun = noun.strip()
    if noun in ("card",):
        return Filter(text="card")
    if not noun.endswith(" card"):
        raise Unsupported(f"hand card {noun!r}")
    return parse_noun(noun[:-len(" card")].replace(", ", " "))


def _look_choose_discard_draw(m, sc):
    """Oildeep Gearhulk: "Look at target player's hand. You may choose a card from it. If you do,
    that player discards that card, then draws a card." (CR 701.20e look; "if you do" checks the
    choice, 608.2c)."""
    who = target_noun("target player", sc)
    sc.last_player = who
    return fx.ChooseFromHand(who, None, "discard", kind="Discard", optional=True, then_draw=1, text=m.group(0))


def _discard_unless(m, sc):
    noun = m.group(2)
    if noun == "an instant or sorcery card or a creature card with flying":
        inst = Filter(any_types=["Instant", "Sorcery"], text="instant or sorcery")
        fly = Filter(types=["Creature"], keywords=["flying"], text="creature with flying")
        f = Filter(custom=lambda g, c, o, src, ctx: inst.match_chars(g, c, o, src, ctx) or fly.match_chars(g, c, o, src, ctx),
                   text="instant or sorcery card or creature card with flying")
    else:
        f = _hand_card_filter(re.sub(r"^an? ", "", noun))
    return fx.DiscardUnless(num(m.group(1)), f, text=m.group(0))


def _exile_creature_and_gy(m, sc):
    who = target_noun("target opponent", sc)
    sc.last_player = who
    return fx.ExileCreatureAndGraveyard(who, parse_noun("creature"), text=m.group(0))


def _then_seq(m, sc):
    """"<action>, then <action>." (Theorix Charm "Mill three cards, then draw a card", Prismari
    Charm "Surveil 2, then draw a card"): both, in order (CR 608.2c)."""
    a = parse_one(m.group(1) + ".", sc)
    b = parse_one(m.group(2)[0].upper() + m.group(2)[1:] + ".", sc)
    return Seq(a, b)


PATTERNS[0:0] = [
    (r"Look at the top (\w+) cards of your library\. Put (\w+) of them into your hand and the rest on the bottom of your library in (any|a random) order\.", _dig),
    (r"Look at the top two cards of your library\. Put one of them into your hand and the other on the bottom of your library\.", _dig_other),
    (r"Look at the top (\w+) cards of your library\. Put (\w+) of them into your hand and the rest on the bottom of your library in any order\. If there is an instant card and a sorcery card in your graveyard, instead put (\w+) of them into your hand and the rest on the bottom of your library in any order\.", _dig_instead),
    (r"Look at the top X cards of your library, where X is (.+?)\. Put one of those cards into your hand\. If (?:~|this spell) was kicked, put two of those cards into your hand instead\. Put the rest on the bottom of your library in a random order\.", _dig_x_kicked),
    (r"Look at the top (\w+) cards of your library\. You may reveal a creature card from among them\. If that card has mana value 2 or less, you may put it onto the battlefield and it gains haste until end of turn\. If you didn't put the revealed card onto the battlefield this way, put it into your hand\. Put the rest on the bottom of your library in a random order\.", _break_out),
    (r"Target opponent reveals their hand\. You choose an? (.+?) from it\. (That player discards that card|Exile that card)\.", _reveal_choose),
    (r"Look at target player's hand\. You may choose a card from it\. If you do, that player discards that card, then draws a card\.", _look_choose_discard_draw),
    (r"Discard (two) cards unless you discard (an? .+?)\.", _discard_unless),
    (r"Target opponent exiles a creature they control and their graveyard\.", _exile_creature_and_gy),
    (r"(Mill (?:a card|\w+ cards)|Surveil \d+|Scry \d+), then (draw (?:a card|\w+ cards))\.", _then_seq),
]


def _dmg_each_of_targets(m, sc):
    """"~ deals N damage to each of one or two targets." One target word with one or two targets
    (CR 115.1d, 601.2c: the same object or player can't be chosen twice for it); the damage is
    dealt to each of them at once."""
    mx = {"one or two": 2, "one, two, or three": 3}[m.group(2)]
    sel = sc.tc.add(TargetSpec("any", n=(1, mx), text="each of " + m.group(2) + " targets"))
    return fx.Damage(amount_of(m.group(1), sc), sel, text=m.group(0))


PATTERNS[0:0] = [
    (r"~ deals (\d+) damage to each of (one or two|one, two, or three) targets\.", _dmg_each_of_targets),
]



# --------------------------------------------------------------------------- batch 2 (card push s)

def _return_one_or_two_gy(m, sc):
    """"Return one or two target creature and/or planeswalker cards from your graveyard to your
    hand." (Archenemy's Charm): one target word with one or two targets (CR 115.1a, 601.2c)."""
    f = parse_noun(m.group(1).replace(" and/or ", " or "))
    f.owner = "you"
    sel = sc.tc.add(TargetSpec("card", filt=f, zone="graveyard", n=(1, 2), text=m.group(0)))
    sc.last = sel
    return fx.MoveTo(sel, "hand", text=m.group(0))


def _return_one_or_two_bf(m, sc):
    """"Return one or two target creatures and/or enchantments you own to your hand." (Get Out)."""
    f = parse_noun(m.group(1).replace(" and/or ", " or "))
    f.owner = "you"
    sel = sc.tc.add(TargetSpec("object", filt=f, zone="battlefield", n=(1, 2), text=m.group(0)))
    sc.last = sel
    return fx.MoveTo(sel, "hand", text=m.group(0))


def _its_controller_tokens(m, sc):
    """"Destroy target ... Its controller creates two Map tokens." (Get Lost): the controller as
    the permanent last existed (CR 608.2h), whether or not it was destroyed."""
    if sc.last is None:
        raise Unsupported("'Its controller' without antecedent")
    n = 1 if m.group(1) in ("a", "an") else num(m.group(1))
    return fx.CreateToken(parse_token(m.group(2)), n, who=fx.lki_controllers(sc.last), text=m.group(0))


def _it_deals_you(m, sc):
    """Callous Inspector: "When this creature dies, it deals 1 damage to you." "It" is the
    creature, as it last existed (CR 113.7a, 603.10a): the damage source is its last known
    information."""
    from . import triggers as tg
    tr = sc.trigger
    if sc.last is not None or not (isinstance(tr, tg.Dies) and tr.self_):
        raise Unsupported(f"'It deals damage to you' outside a self dies trigger: {m.group(0)!r}")
    return fx.Damage(amount_of(m.group(1), sc), fx.you(), text=m.group(0))


def _he_fights(m, sc):
    """"When Wolverine enters, he fights up to one other target creature." ("he" = ~)."""
    a = fx.self_permanent()
    b = target_noun(m.group(1), sc)
    return fx.Fight(a, b, text=m.group(0))


def _land_controller_may_search(m, sc):
    """"That land's controller may search their library for a basic land card, put it onto the
    battlefield[ tapped], then shuffle." (Demolition Field)."""
    if sc.last is None:
        raise Unsupported("'That land's controller' without antecedent")
    return fx.PlayerSearch(fx.lki_controllers(sc.last), parse_noun("basic land"), 1, "battlefield",
                           tapped=bool(m.group(1)), optional=True, text=m.group(0))


def _you_may_search_basic(m, sc):
    return fx.PlayerSearch(fx.you(), parse_noun("basic land"), 1, "battlefield", tapped=bool(m.group(1)),
                           optional=True, text=m.group(0))


def _its_controller_searches_stun(m, sc):
    """Magmatic Hellkite: "Its controller searches their library for a basic land card, puts it
    onto the battlefield tapped with a stun counter on it, then shuffles." (CR 701.23b: a search
    for a card with a stated quality may find nothing; 122.1d stun counters)."""
    if sc.last is None:
        raise Unsupported("'Its controller' without antecedent")
    return fx.PlayerSearch(fx.lki_controllers(sc.last), parse_noun("basic land"), 1, "battlefield",
                           tapped=True, counters={"stun": 1}, text=m.group(0))


def _settle(m, sc):
    """Settle the Wreckage: "Exile all attacking creatures target player controls. That player may
    search their library for that many basic land cards, put those cards onto the battlefield
    tapped, then shuffle." ("that many": the creatures exiled this way, 608.2c)."""
    who = target_noun("target player", sc)
    sc.last_player = who
    att = Filter(types=["Creature"], attacking=True, text="attacking creature")

    def fn(g, ctx, who=who, att=att):
        ps = fx.players_of(who, g, ctx)
        return [o for o in list(g.s.battlefield) if g.ctrl(o) in ps and att.match(g, o, ctx.get("source"), ctx)]
    # "That many": the attacking creatures the spell exiled, tokens included, even one that didn't
    # end up in exile (Settle the Wreckage ruling 2017-09-29): those it tried to exile.
    pick = fx.Remember(fx.Sel(fn, "attacking creatures target player controls", single=False), "settled")
    exile = fx.MoveTo(fx.var("settled"), "exile", text=m.group(0))
    n = fx.Amount(lambda g, ctx: len((ctx.get("vars") or {}).get("settled") or []), "that many")
    search = fx.PlayerSearch(who, parse_noun("basic land"), n, "battlefield", tapped=True, optional=True, text=m.group(0))
    return Seq(pick, exile, search)


def _mill_targets(m, sc):
    """"Any number of target players each mill two cards." (CR 115.1, 701.17a)."""
    who = sc.tc.add(TargetSpec("player", n=(0, 2), text="any number of target players"))
    return fx.Mill(num(m.group(1)), who, text=m.group(0))


def _mill_targets_gy(m, sc):
    who = sc.tc.add(TargetSpec("player", n=(0, 2), text="any number of target players"))
    return fx.MillEach(who, lambda g, ctx, p: len(g.s.players[p].graveyard), text=m.group(0))


def _counter_each_creature_target_player(m, sc):
    """Practiced Offense: "Put a +1/+1 counter on each creature target player controls."."""
    who = target_noun("target player", sc)
    sc.last_player = who
    cre = Filter(types=["Creature"], text="creature")

    def fn(g, ctx, who=who, cre=cre):
        ps = fx.players_of(who, g, ctx)
        return [o for o in list(g.s.battlefield) if g.ctrl(o) in ps and cre.match(g, o, ctx.get("source"), ctx)]
    return fx.PutCounters(fx.Sel(fn, "each creature target player controls", single=False), m.group(1), 1, text=m.group(0))


def _gains_choice(m, sc):
    sel = obj_ref(m.group(1), sc)
    sc.last = sel
    return fx.ChooseKeyword(sel, [m.group(2), m.group(3)], text=m.group(0))


def _search_any_to_gy(m, sc):
    """Lively Dirge: "Search your library for a card, put it into your graveyard, then shuffle."
    (701.23d: a search for a quantity of cards must find that many if possible)."""
    return fx.PlayerSearch(fx.you(), None, 1, "graveyard", must_find=True, text=m.group(0))


def _return_total_mv(m, sc):
    f = parse_noun(m.group(2))
    return fx.ReturnTotalMV(num(m.group(1)), int(m.group(3)), f, text=m.group(0))


def _cryomancer(m, sc):
    """Seasoned Cryomancer: "draw two cards, then discard two cards. When you discard one or more
    nonland cards this way, tap up to that many target creatures and put a stun counter on each
    of them." (CR 603.12 reflexive trigger; its target count is fixed as it triggers)."""
    loot = Seq(fx.Draw(num(m.group(1))), fx.Discard(num(m.group(2)), fx.you()))
    from .compiler import Targets
    rtc = Targets()
    sel = rtc.add(TargetSpec("creature", filt=Filter(types=["Creature"], text="creature"),
                             n=(0, lambda g, ctx: (ctx.get("data") or {}).get("that_many", 0)),
                             text="up to that many target creatures"))
    eff = Seq(fx.Tap(sel), fx.PutCounters(sel, "stun", 1))

    def count(g, ctx):
        ids = (ctx.get("vars") or {}).get("discarded") or []
        n = 0
        for x in ids:
            if x is None:
                continue
            c = g.chars(x) if x in g.s.objs else g.s.lki.get(x)
            if c is not None and "Land" not in c.types:
                n += 1
        return n
    return Seq(loot, fx.ReflexiveThatMany(eff, rtc.specs, count, text=m.group(0)))


PATTERNS[0:0] = [
    (r"Return one or two target (creature and/or planeswalker) cards from your graveyard to your hand\.", _return_one_or_two_gy),
    (r"Return one or two target (creatures and/or enchantments) you own to your hand\.", _return_one_or_two_bf),
    (r"Its controller creates (a|an|two|three) (.+? tokens?)\.", _its_controller_tokens),
    (r"(?:It|~) deals (\d+) damage to you\.", _it_deals_you),
    (r"(?:He|She) fights (up to one other target creature)\.", _he_fights),
    (r"That land's controller may search their library for a basic land card, put it onto the battlefield( tapped)?, then shuffle\.", _land_controller_may_search),
    (r"You may search your library for a basic land card, put it onto the battlefield( tapped)?, then shuffle\.", _you_may_search_basic),
    (r"Its controller searches their library for a basic land card, puts it onto the battlefield tapped with a stun counter on it, then shuffles\.", _its_controller_searches_stun),
    (r"Exile all attacking creatures target player controls\. That player may search their library for that many basic land cards, put those cards onto the battlefield tapped, then shuffle\.", _settle),
    (r"Any number of target players each mill (\w+) cards\.", _mill_targets),
    (r"Any number of target players each mill cards equal to the number of cards in their graveyard\.", _mill_targets_gy),
    (r"Put a ([+\-]1/[+\-]1) counter on each creature target player controls\.", _counter_each_creature_target_player),
    (r"(Target creature) gains your choice of (double strike|first strike|lifelink|vigilance|deathtouch|trample|haste|flying) or (double strike|first strike|lifelink|vigilance|deathtouch|trample|haste|flying) until end of turn\.", _gains_choice),
    (r"Search your library for a card, put it into your graveyard, then shuffle\.", _search_any_to_gy),
    (r"Return up to (two|three) (creature cards) with total mana value (\d+) or less from your graveyard to the battlefield\.", _return_total_mv),
    (r"Draw (two|three) cards, then discard (two|three) cards\. When you discard one or more nonland cards this way, tap up to that many target creatures and put a stun counter on each of them\.", _cryomancer),
]


def _if_exiled_creature(m, sc):
    """Raven Eagle: "If a creature card is exiled this way, create a Clue token." (the card in
    exile, CR 400.7, 608.2c)."""
    then = parse_one(m.group(1)[0].upper() + m.group(1)[1:] + ".", sc)

    def cond(g, ctx):
        for x in (ctx.get("vars") or {}).get("exiled") or []:
            if x in g.s.objs and "Creature" in g.chars(x).types:
                return True
        return False
    return fx.If(cond, then, text=m.group(0))


def _if_returned(m, sc):
    """Nurturing Pixie: "If a permanent was returned this way, put a +1/+1 counter on ~." (the
    return of an "up to one" target may not happen, 608.2c)."""
    then = parse_one(m.group(1)[0].upper() + m.group(1)[1:] + ".", sc)
    return fx.If(lambda g, ctx: bool((ctx.get("vars") or {}).get("last_done")), then, text=m.group(0))


PATTERNS[0:0] = [
    (r"If a creature card is exiled this way, (.+)\.", _if_exiled_creature),
    (r"If a permanent was returned this way, (.+)\.", _if_returned),
]
MERGE_NEXT = MERGE_NEXT + (
    r"^When you discard one or more nonland cards this way, .+\.$",
    r"^That player may search their library for that many basic land cards, .+\.$",
)



# --------------------------------------------------------------------------- batch 3 (card push s): exile the top cards, play them

def _bonehoard(m, sc):
    """Bonehoard Dracosaur: "Exile the top two cards of your library. You may play them this turn.
    If you exiled a land card this way, create a 3/1 red Dinosaur creature token. If you exiled a
    nonland card this way, create a Treasure token." (608.2c: the conditions look at the exiled
    cards)."""
    ex = fx.ExileTopMayPlay(num(m.group(1)), "this turn", text=m.group(0))

    def has(g, ctx, land):
        for x in (ctx.get("vars") or {}).get("exiled") or []:
            if x in g.s.objs and (("Land" in g.chars(x).types) == land):
                return True
        return False
    t1 = fx.CreateToken(parse_token(m.group(2)), 1, text=m.group(0))
    t2 = fx.CreateToken(parse_token(m.group(3)), 1, text=m.group(0))
    return Seq(ex, fx.If(lambda g, ctx: has(g, ctx, True), t1), fx.If(lambda g, ctx: has(g, ctx, False), t2))


def _cori(m, sc):
    return fx.ExileTopMayPlay(1, "end of your next turn", text=m.group(0))


def _dragonhawk(m, sc):
    """Dragonhawk: "exile the top X cards of your library, where X is <amount>. You may play those
    cards until your next end step. At the beginning of your next end step, ~ deals 2 damage to
    each opponent for each of those cards that are still exiled.\""""
    x = parse_amount_expr(m.group(1), sc)
    return Seq(fx.ExileTopMayPlay(x, "your next end step", text=m.group(0)),
               fx.DelayedYourNextEndStep(int(m.group(2)), text=m.group(0)))


PATTERNS[0:0] = [
    (r"Exile the top (\w+) cards of your library\. You may play them this turn\. If you exiled a land card this way, create (an? .+? token)\. If you exiled a nonland card this way, create (an? .+? token)\.", _bonehoard),
    (r"Exile the top card of your library\. Until the end of your next turn, you may play that card\.", _cori),
    (r"Exile the top X cards of your library, where X is (.+?)\. You may play those cards until your next end step\. At the beginning of your next end step, ~ deals (\d+) damage to each opponent for each of those cards that are still exiled\.", _dragonhawk),
]
MERGE_REST = MERGE_REST[:-1] + r"|[Ee]xile the top (?:\w+ cards|card|X cards) of your library\b)"



# --------------------------------------------------------------------------- batch 4 (card push s)

PATTERNS[0:0] = [
    (r"Choose a land card name\.", lambda m, sc: fx.ChooseCardName("land", text=m.group(0))),
]


# =========================================================================== card support

def _pump_plural_signed(m, sc):
    """"Other creatures get -2/-2 until end of turn" (CR 611.2c: the affected set is locked as
    the effect begins; Harvester of Misery ruling 2024-04-12)."""
    sel = obj_ref(m.group(1), sc)
    return fx.Pump(sel, int(m.group(2)), int(m.group(3)), text=m.group(0))


def _dmg_kicked_that(m, sc):
    """"~ deals N damage to target X. If this spell was kicked, it deals M damage to that X
    instead." The kicked amount replaces the base one (a self-replacement effect, CR 614.15, so
    it applies before any other replacement effect): deal M instead of N."""
    sel = target_noun(m.group(2), sc)
    sc.last = sel
    base = amount_of(m.group(1), sc)
    kicked = amount_of(m.group(3), sc)
    return fx.If(lambda g, ctx: bool(ctx.get("kicked")), fx.Damage(kicked, sel), fx.Damage(base, sel), text=m.group(0))


def cast_from_zone(zone):
    """"If this spell was cast from a graveyard" (CR 601.2a: the zone it was cast from)."""
    def cond(g, ctx, zone=zone):
        o = g.s.objs.get(ctx.get("source"))
        return o is not None and o.cast is not None and o.cast.get("from") == zone
    return cond


def _draw_instead_if_cast_from(m, sc):
    """"Draw a card. If this spell was cast from a graveyard, draw two cards instead." The
    second sentence replaces the first draw (CR 614.15 self-replacement): N cards in all, not
    1 + N."""
    return fx.If(cast_from_zone("graveyard"), fx.Draw(num(m.group(1))), fx.Draw(1), text=m.group(0))


def _dmg_equal_count(m, sc):
    """"~ deals damage equal to the number of <permanents you control> to any target" (he/she
    is the card itself, CR 201.5c)."""
    n = fx.count_amount(parse_noun(m.group(1)))
    to = m.group(2)
    sel = target_noun(to, sc)
    sc.last = sel
    return fx.Damage(n, sel, text=m.group(0))


MERGE_NEXT = MERGE_NEXT + (
    r"^If ~ was kicked, it deals \d+ damage to that (?:creature|permanent|player|planeswalker) instead\.$",
    r"^If ~ was cast from a graveyard, draw (?:two|three) cards instead\.$",
)

PATTERNS[0:0] = [
    (r"(" + PLURAL_CREATURES + r") get ([+\-]\d+)/([+\-]\d+) until end of turn\.", _pump_plural_signed),
    (r"~ deals (\d+) damage to (target .+?)\. If ~ was kicked, it deals (\d+) damage to that (?:creature|permanent|player|planeswalker) instead\.", _dmg_kicked_that),
    (r"Draw a card\. If ~ was cast from a graveyard, draw (two|three) cards instead\.", _draw_instead_if_cast_from),
    (r"(?:~|He|She) deals damage equal to the number of (.+? you control) to (any target|target .+?)\.", _dmg_equal_count),
]


def _exile_all_but_bottom(m, sc):
    """"Each player exiles all but the bottom six cards of their library face down" (Doomsday
    Excruciator; ruling 2024-09-20: a library of six or fewer cards loses nothing). One
    simultaneous event (CR 610.1); face-down exiled cards are hidden (CR 406.3)."""
    k = num(m.group(1))

    def fn(g, ctx, k=k):
        moves = []
        for p in (g.s.active, 1 - g.s.active):
            lib = g.s.players[p].library
            if len(lib) > k:
                g.top_hook(p, len(lib) - k, "exile")  # harness: the cards the EUT exiled go on top
            for o in lib[:max(0, len(lib) - k)]:
                moves.append((o, "exile", {"face_down": True, "cause": "effect"}))
        if moves:
            g.move_batch(moves)
    return fx.Fn_(fn, text=m.group(0))


def _bounce_all(m, sc):
    """"Return all non-Elemental creatures to their owners' hands" (one simultaneous move)."""
    f = parse_noun(m.group(1))
    return fx.MoveTo(fx.all_matching(f), "hand", text=m.group(0))


def _discard_hand_then_draw(m, sc):
    """"Discard your hand, then draw two cards." (CR 701.9a)."""
    return Seq(fx.Discard("all", fx.you(), text=m.group(0)), fx.Draw(num(m.group(1)), text=m.group(0)))


PATTERNS[0:0] = [
    (r"Each player exiles all but the bottom (\w+) cards of their library face down\.", _exile_all_but_bottom),
    (r"Return all (.+?) to their owners' hands\.", _bounce_all),
    (r"Discard your hand, then draw (two|three) cards\.", _discard_hand_then_draw),
]


class _MayCost(fx.E):
    """An optional action whose "If you do" follow-up needs it done (CR 608.2c, 608.2d): it
    can't be chosen when impossible, and reports whether it happened."""

    def __init__(self, possible, do, text=""):
        self._possible = possible
        self._do = do
        self.text = text
        self.reports_done = True

    def possible(self, g, ctx):
        return self._possible(g, ctx)

    def run(self, g, ctx):
        done = bool(self._do(g, ctx))
        ctx.setdefault("vars", {})["last_done"] = done
        return done


def _sacrifice_you(m, sc):
    """"Sacrifice another creature or artifact." (CR 701.21a: a permanent you control; you
    choose which)."""
    f = parse_noun(m.group(2) + " you control")
    if m.group(1) == "another":
        f.other = True

    def cands(g, ctx, f=f):
        p = ctx["controller"]
        return [o for o in g.s.battlefield if g.ctrl(o) == p and f.match(g, o, ctx.get("source"), {"controller": p})]

    def do(g, ctx):
        cs = cands(g, ctx)
        if not cs:
            return False
        from ..engine.decisions import Decision, Opt
        opts = [Opt(f"choose:{g.ref(o)}", o, sig=("sac", g.obj_sig(o))) for o in cs]
        o = g.ask(Decision(ctx["controller"], "Sacrifice", opts, prompt="Choose a permanent to sacrifice."))
        g.sacrifice(o)
        return True
    return _MayCost(lambda g, ctx: bool(cands(g, ctx)), do, text=m.group(0))


def _remove_counter_from_self(m, sc):
    """"Remove a +1/+1 counter from ~." (CR 122.8: only if it has one)."""
    kind = m.group(1)

    def has(g, ctx, kind=kind):
        o = g.s.objs.get(ctx.get("source"))
        return o is not None and o.zone == "battlefield" and o.counters.get(kind, 0) > 0

    def do(g, ctx, kind=kind):
        if not has(g, ctx):
            return False
        return g.remove_counters(ctx["source"], kind, 1) > 0
    return _MayCost(has, do, text=m.group(0))


def _you_lose_that_much(m, sc):
    return fx.LoseLife(fx.event_amount(), fx.you(), text=m.group(0))


def _untap_and_transform_self(m, sc):
    """"untap ~ and transform it" (CR 701.27a: it's the same object, so 'it' is ~)."""
    me = fx.self_permanent()
    return Seq(fx.Tap(me, untap=True, text=m.group(0)), fx.Transform(me, text=m.group(0)))


def _draw_half_x(m, sc):
    """"Draw half X cards, rounded down." (CR 107.1a)."""
    return fx.Draw(fx.Amount(lambda g, ctx: (ctx.get("x", 0) or 0) // 2, "half X"), text=m.group(0))


def _pump_minus_x(m, sc):
    """"Target creature gets -X/-0 until end of turn, where X is ..." (X counted on resolution,
    CR 608.2h)."""
    sel = obj_ref(m.group(1), sc)
    sc.last = sel
    x = amount_of("X", sc)
    if not isinstance(x, fx.Amount):
        raise Unsupported(f"-X/-0 without a 'where X is' in {m.group(0)!r}")
    return fx.Pump(sel, fx.Amount(lambda g, ctx, x=x: -x(g, ctx), "-X"), 0, text=m.group(0))


def _counters_x_sacrificed(m, sc):
    """Obsessive Pursuit: "put X +1/+1 counters on target attacking creature, where X is the
    number of permanents you've sacrificed this turn. If X is three or more, that creature
    gains lifelink until end of turn." X is counted as the ability resolves (CR 608.2h), and the
    second sentence uses that same X."""
    sel = target_noun(m.group(1), sc)
    sc.last = sel
    k = num(m.group(2))
    kw = m.group(3)

    def x_of(g, ctx):
        return g.s.players[ctx["controller"]].turn_stats.get("sacrificed", 0)

    def fn(g, ctx, k=k, kw=kw):
        x = x_of(g, ctx)
        fx.PutCounters(sel, "+1/+1", x).run(g, ctx)
        if x >= k:
            fx.Pump(sel, None, None, keywords=[kw]).run(g, ctx)
    return fx.Fn_(fn, text=m.group(0))


def _draw_if_gained(m, sc):
    """"Draw a card if you've gained 3 or more life this turn." (checked on resolution; Haliya
    ruling 2025-07-25: life gained this turn, not the life total's change)."""
    n = int(m.group(1))
    return fx.If(lambda g, ctx, n=n: g.s.players[ctx["controller"]].life_gained >= n, fx.Draw(1), text=m.group(0))


MERGE_NEXT = MERGE_NEXT + (
    r"^If X is (?:two|three|four|five) or more, that creature gains (?:lifelink|flying|trample|haste|deathtouch|indestructible) until end of turn\.$",
)

PATTERNS[0:0] = [
    (r"Sacrifice (a|an|another) ((?:creature|artifact|enchantment|land|nonland permanent|permanent)(?: or (?:creature|artifact|enchantment|land))?)\.", _sacrifice_you),
    (r"Remove an? ([+\-]1/[+\-]1) counter from ~\.", _remove_counter_from_self),
    (r"You lose that much life\.", _you_lose_that_much),
    (r"Untap ~ and transform it\.", _untap_and_transform_self),
    (r"Draw half X cards, rounded down\.", _draw_half_x),
    (r"(Target .+?|~|It) gets -X/-0 until end of turn\.", _pump_minus_x),
    (r"Put X \+1/\+1 counters on (target .+?), where X is the number of permanents you've sacrificed this turn\. If X is (two|three|four|five) or more, that creature gains (lifelink|flying|trample|haste|deathtouch|indestructible) until end of turn\.", _counters_x_sacrificed),
    (r"Draw a card if you've gained (\d+) or more life this turn\.", _draw_if_gained),
]


def _ability_on_stack_filter():
    """'activated or triggered ability' on the stack (CR 113.3b-c; mana abilities never use the
    stack, 605.3b)."""
    # CR 115.5: an ability on the stack is an illegal target for itself (targets are chosen with
    # the ability's own context).
    return Filter(custom=lambda g, c, it, src, ctx: (getattr(it, "kind", None) in ("activated", "triggered")
                                                     and getattr(it, "ctx", None) is not ctx),
                  text="activated or triggered ability")


class _CounterAbilityLoseAbilities(fx.E):
    """Tishana's Tidebinder: "Counter up to one target activated or triggered ability. If an
    ability of an artifact, creature, or planeswalker is countered this way, that permanent loses
    all abilities for as long as ~ remains on the battlefield." The source is checked as the
    ability resolves (ruling 2023-11-10); if ~ already left, nothing loses abilities (611.2b)."""

    def __init__(self, sel, types, text=""):
        self.sel = sel
        self.types = set(types)
        self.text = text

    def run(self, g, ctx):
        for r in self.sel.get(g, ctx):
            if not (isinstance(r, tuple) and r[0] == "stack"):
                continue
            it = next((x for x in g.s.stack if x.id == r[1]), None)
            if it is None:
                continue
            src = it.source
            if not g.counter(r):
                continue
            o = g.s.objs.get(src)
            if o is None or o.zone != "battlefield" or not (self.types & g.chars(src).types):
                continue
            fx.LoseAllAbilities(fx.Sel(lambda g, ctx, src=src: [src], "that permanent"), "while_host").run(g, ctx)


def _counter_ability_lose(m, sc):
    spec = TargetSpec("ability", filt=_ability_on_stack_filter(), zone="stack", n=(0, 1),
                      text="up to one target activated or triggered ability")
    sel = sc.tc.add(spec)
    types = [{"artifact": "Artifact", "creature": "Creature", "planeswalker": "Planeswalker"}[w]
             for w in re.split(r",? or |, ", m.group(1))]
    return _CounterAbilityLoseAbilities(sel, types, text=m.group(0))


MERGE_NEXT = MERGE_NEXT + (
    r"^If an ability of an? .+? is countered this way, that permanent loses all abilities for as long as ~ remains on the battlefield\.$",
)

PATTERNS[0:0] = [
    (r"Counter up to one target activated or triggered ability\. If an ability of an? ((?:artifact|creature|planeswalker)(?:,? or |, )(?:(?:artifact|creature|planeswalker)(?:,? or |, )?)*) is countered this way, that permanent loses all abilities for as long as ~ remains on the battlefield\.", _counter_ability_lose),
]


class _LKICountersAndEquipmentTo(fx.E):
    """Zack Fair: "Put ~'s counters on that creature and attach an Equipment that was attached
    to ~ to that creature." ~ was sacrificed to pay the cost: its counters as it last existed
    (CR 608.2h; ruling 2025-06-06: the same number of each kind, all kinds) and an Equipment
    attached to it then (still on the battlefield; its controller chooses one if several). An
    Equipment that can't equip that creature doesn't move (CR 301.5b-c, 701.3b)."""

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        fx.PutLKICountersOn(self.sel).run(g, ctx)
        tgts = [r for r in self.sel.get(g, ctx) if not isinstance(r, tuple) and r in g.s.objs
                and g.s.objs[r].zone == "battlefield"]
        if not tgts:
            return
        t = tgts[0]
        att = (ctx.get("paid") or {}).get("attached_to_self") or []
        eqs = [x for x in att if x in g.s.objs and g.s.objs[x].zone == "battlefield"
               and "Equipment" in g.chars(x).subtypes]
        if not eqs:
            return
        if len(eqs) == 1:
            e = eqs[0]
        else:
            from ..engine.decisions import Decision, Opt
            opts = [Opt(f"choose:{g.ref(x)}", x, sig=("eq", g.obj_sig(x))) for x in eqs]
            e = g.ask(Decision(ctx["controller"], "ChooseCard", opts, prompt="Choose an Equipment to attach."))
        c = g.chars(e)
        if e == t or "Creature" in c.types or "Creature" not in g.chars(t).types:
            return  # 301.5c: can't equip itself; an Equipment creature can't equip
        if g.protected_from(t, e):
            return  # 702.16d: can't be equipped by an Equipment with a quality it's protected from
        o = g.s.objs[e]
        if o.attached_to == t:
            return  # 701.3b
        o.attached_to = t
        o.timestamp = g.s.new_ts()  # 701.3c
        g.s.touch()
        g.emit({"type": "attached", "obj": e, "to": t})


def _zack_counters_equipment(m, sc):
    if sc.last is None:
        raise Unsupported("'that creature' without antecedent")
    return _LKICountersAndEquipmentTo(sc.last, text=m.group(0))


class _DelayedThisTurn(fx.E):
    """"Whenever <event> this turn, <effect>" from a resolving ability: a delayed triggered
    ability that triggers every time for the rest of the turn (CR 603.7a, 603.7b)."""

    def __init__(self, ability, text=""):
        self.ability = ability
        self.text = text

    def run(self, g, ctx):
        g.s.delayed.append({"trigger": self.ability.trigger, "ability": self.ability,
                            "controller": ctx["controller"], "source": ctx.get("source"),
                            "source_chars": ctx.get("source_chars") or ctx.get("chars"),
                            "once": False, "this_turn": g.s.turn})


def _delayed_this_turn(m, sc):
    from .compiler import compile_line
    inner = f"Whenever {m.group(1)}, {m.group(2)}."
    res = compile_line(inner, sc.cx)
    if res == "spell" or len(res) != 1 or res[0].kind != "triggered" or res[0].condition is not None:
        raise Unsupported(f"delayed trigger {m.group(0)!r}")
    return _DelayedThisTurn(res[0], text=m.group(0))


def _surveil_then_hand_if_mv(m, sc):
    """Enlightened Confidant: "surveil 1. If you put a card with mana value less than or equal
    to the amount of life you gained this turn into your graveyard this way, put that card into
    your hand." Life gained is counted as the ability resolves (ruling 2026-08-21); X in a
    graveyard card's mana cost is 0 (CR 107.3b)."""
    n = num(m.group(1))

    def fn(g, ctx, n=n):
        from ..engine.mana import mana_value
        fx.Scry(n, surveil=True).run(g, ctx)
        gained = g.s.players[ctx["controller"]].life_gained
        put = [x for x in (ctx.get("vars") or {}).get("surveiled", []) if x in g.s.objs
               and g.s.objs[x].zone == "graveyard"
               and mana_value(g.chars(x).mana_cost, 0) <= gained]
        if put:
            g.move_batch([(x, "hand", {}) for x in put])
    return fx.Fn_(fn, text=m.group(0))


MERGE_NEXT = MERGE_NEXT + (
    r"^If you put a card with mana value less than or equal to the amount of life you gained this turn into your graveyard this way, put that card into your hand\.$",
)

PATTERNS[0:0] = [
    (r"Put ~'s counters on that creature and attach an Equipment that was attached to ~ to that creature\.", _zack_counters_equipment),
    (r"Whenever (.+?) this turn, (.+)\.", _delayed_this_turn),
    (r"Surveil (\d+)\. If you put a card with mana value less than or equal to the amount of life you gained this turn into your graveyard this way, put that card into your hand\.", _surveil_then_hand_if_mv),
]


class _PunisherPerCounter(fx.E):
    """Rottenmouth Viper: "Then for each blight counter on it, each opponent loses 4 life unless
    that player sacrifices a nonland permanent of their choice or discards a card." (rulings
    2024-07-26): for each counter (as it last existed if it left) the opponent chooses a card to
    discard, a nonland permanent to sacrifice (none chosen twice), or neither; then the chosen
    cards are discarded, the permanents sacrificed, and they lose 4 life for each 'neither'."""

    def __init__(self, kind, life, text=""):
        self.kind = kind
        self.life = life
        self.text = text

    def run(self, g, ctx):
        from ..engine.decisions import Decision, Opt
        src = ctx.get("source")
        o = g.s.objs.get(src)
        if o is None or o.zone != "battlefield":
            o = g.s.lki_obj.get(src)
        n = (o.counters.get(self.kind, 0) if o is not None else 0)
        for (_, p) in fx.opponent().get(g, ctx):
            disc, sac, lose = [], [], 0
            for _ in range(n):
                # The choice of what to do, then of which permanent or card (the EUT's shape:
                # mode:1 sacrifice, mode:2 discard, mode:3 lose the life).
                perms = [x for x in g.s.battlefield
                         if x not in sac and g.ctrl(x) == p and "Land" not in g.chars(x).types]
                cards = [x for x in g.s.players[p].hand if x not in disc]
                opts = []
                if perms:
                    opts.append(Opt("mode:1", "sacrifice"))
                if cards:
                    opts.append(Opt("mode:2", "discard"))
                opts.append(Opt("mode:3", "lose"))
                what = g.ask(Decision(p, "ChooseMode", opts, subject=g.ref(src) if src in g.s.objs else None,
                                      prompt=f"Sacrifice a nonland permanent, discard a card, or lose {self.life} life?"))
                if what == "lose":
                    lose += 1
                elif what == "discard":
                    disc.append(g.ask(Decision(p, "Discard", [Opt(f"choose:{g.ref(x)}", x, sig=("pd", g.obj_sig(x))) for x in cards],
                                               prompt="Choose a card to discard.")))
                else:
                    sac.append(g.ask(Decision(p, "Sacrifice", [Opt(f"choose:{g.ref(x)}", x, sig=("ps", g.obj_sig(x))) for x in perms],
                                              prompt="Choose a nonland permanent to sacrifice.")))
            for x in disc:
                if x in g.s.objs and g.s.objs[x].zone == "hand":
                    g.discard(p, x, cause="effect")
            for x in sac:
                if x in g.s.objs and g.s.objs[x].zone == "battlefield":
                    g.sacrifice(x)
            for _ in range(lose):
                g.lose_life(p, self.life, source=src)


PATTERNS[0:0] = [
    (r"Put a (blight) counter on (?:it|~)\.", lambda m, sc: fx.PutCounters(fx.self_permanent(), m.group(1), 1, text=m.group(0))),
    (r"For each (blight) counter on it, each opponent loses (\d+) life unless that player sacrifices a nonland permanent of their choice or discards a card\.",
     lambda m, sc: _PunisherPerCounter(m.group(1), int(m.group(2)), text=m.group(0))),
]


class _CopySelfSpell(fx.E):
    """"copy this spell" (CR 707.10; effects.copy_spell): a copy of the spell, with its copiable
    values and the decisions made for it (modes, X, kicker, costs), goes on the stack under the
    ability's controller; it isn't cast. A copy of a permanent spell becomes a token as it
    resolves (707.10f, 608.3f)."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        s = g.s
        src = ctx.get("source")
        o = s.objs.get(src)
        it0 = next((x for x in s.stack if x.kind == "spell" and x.obj == src), None)
        if o is not None and o.zone == "stack" and it0 is not None:
            fx.copy_spell(g, it0, ctx["controller"], new_targets=False)
            return
        # The spell has left the stack (countered, or resolved first): the effect copies it as it
        # last existed on the stack (CR 608.2h, 707.10), with the decisions made for it.
        it0, lo = s.lki_spell.get(src), s.lki_obj.get(src)
        if o is not None or it0 is None or lo is None or lo.zone != "stack":
            raise Unsupported("copying a spell that has left the stack (no last known information of it as a spell)")
        fx.copy_spell(g, it0, ctx["controller"], new_targets=False, lki=lo)


PATTERNS[0:0] = [
    (r"Copy ~\.", lambda m, sc: _CopySelfSpell(text=m.group(0))),
]
