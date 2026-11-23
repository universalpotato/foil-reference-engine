"""Static abilities (CR 604) and replacement-effect lines (CR 614) parsed from Oracle text."""
import re

from ..engine.ir import Static, LayerPart, RulePart, ReplacementPart, Keyword
from ..engine.effects import Filter
from ..engine.decisions import Decision, Opt
from .nouns import parse_noun, num, COLORS, singular
from .. import Unsupported

KWS = r"(flying|first strike|double strike|deathtouch|haste|hexproof|indestructible|lifelink|menace|reach|trample|vigilance)"


def _enter_tapped(cond=None, text=""):
    def applies(g, ev, src):
        return cond is None or not cond(g, ev)

    def apply(g, ev, src):
        ev["opts"]["tapped"] = True
    p = ReplacementPart("enter_self", applies, apply, text=text)
    p.commutes = True
    return Static([p], text=text)


def already_there(g, ev):
    """Permanents on the battlefield before this enter event (CR 614.12a: the replacement is
    applied before the permanent enters; a token being created is listed early, see
    create_token)."""
    skip = ev.get("entering") or ()
    return [o for o in g.s.battlefield if o not in skip]


def _controls_n_other_lands(g, ev, op, n):
    p = ev["opts"].get("controller", ev.get("owner"))
    lands = [o for o in already_there(g, ev) if g.ctrl(o) == p and "Land" in g.chars(o).types]
    k = len(lands)
    return k <= n if op == "<=" else k >= n


def _shock(life, text):
    """"As ~ enters, you may pay N life. If you don't, it enters tapped." (614.12, 614.1c)"""
    def applies(g, ev, src):
        return True

    def apply(g, ev, src):
        p = ev["opts"].get("controller", ev.get("owner"))
        if g.s.players[p].life >= life:  # CR 119.4
            a = g.ask(Decision(p, "PayUnless", [Opt("yes", True), Opt("no", False)],
                               subject=f"key:{g.s.objs[ev['old']].key}" if ev.get("old") in g.s.objs else None,
                               prompt=f"Pay {life} life? If you don't, it enters tapped."))
        else:
            a = False
        if a:
            g.pay_life(p, life)
        else:
            ev["opts"]["tapped"] = True
    part = ReplacementPart("enter_self", applies, apply, text=text)
    return Static([part], text=text)


def _anthem(filt, p, t, kws, text, attached=False):
    parts = []
    aff = "attached" if attached else filt
    if p or t:
        def fn(g, oid, c, src, p=p, t=t):
            if c.power is not None:
                c.power += p
                c.toughness += t
        parts.append(LayerPart("7c", aff, fn, text))
    if kws:
        ks = [Keyword(k) for k in kws]

        def grant(g, oid, c, src, ks=ks):
            c.abilities.extend(ks)
        parts.append(LayerPart(6, aff, grant, text))
    return Static(parts, text=text)


def _split_kws(t):
    return [k.strip() for k in re.split(r",? and |, ", t)]


def parse_static(line, cx):
    """Returns a list of abilities or None if the line isn't a static ability we know."""
    l = line
    for ext in EXTRA_STATICS:
        r = ext(l, cx)
        if r is not None:
            return r
    # ---------------------------------------------------------------- enters tapped (614.1c)
    if l in ("~ enters tapped.",):
        return [_enter_tapped(text=l)]
    m = re.fullmatch(r"~ enters tapped unless you control (\w+) or (fewer|more) other lands\.", l)
    if m:
        n = num(m.group(1))
        op = "<=" if m.group(2) == "fewer" else ">="
        return [_enter_tapped(lambda g, ev, n=n, op=op: _controls_n_other_lands(g, ev, op, n), text=l)]
    m = re.fullmatch(r"~ enters tapped unless you control an? (\w+) or an? (\w+)\.", l)
    if m:
        a, b = m.group(1), m.group(2)

        def cond(g, ev, a=a, b=b):
            p = ev["opts"].get("controller", ev.get("owner"))
            return any(({a, b} & g.chars(o).subtypes) for o in already_there(g, ev) if g.ctrl(o) == p)
        return [_enter_tapped(cond, text=l)]
    if l == "~ enters tapped unless you control a basic land.":
        def cond(g, ev):
            p = ev["opts"].get("controller", ev.get("owner"))
            return any("Basic" in g.chars(o).supertypes and "Land" in g.chars(o).types for o in already_there(g, ev) if g.ctrl(o) == p)
        return [_enter_tapped(cond, text=l)]
    if l == "~ enters tapped unless it's your first, second, or third turn of the game.":
        def cond(g, ev):
            p = ev["opts"].get("controller", ev.get("owner"))
            # Turn number of p's own turns: p's k-th turn.
            s = g.s
            if s.active != p:
                return False
            k = (s.turn + (1 if p == s.first_player else 0)) // 2
            return k <= 3
        return [_enter_tapped(cond, text=l)]
    if l == "~ enters tapped if it's not your turn.":
        return [_enter_tapped(lambda g, ev: g.s.active == ev["opts"].get("controller", ev.get("owner")), text=l)]
    m = re.fullmatch(r"As ~ enters, you may pay (\d+) life\. If you don't, it enters tapped\.", l)
    if m:
        return [_shock(int(m.group(1)), l)]
    m = re.fullmatch(r"~ enters with (a|an|one|two|three|four|five|X) ([+\-]1/[+\-]1) counters? on it\.", l)
    if m:
        n = m.group(1)
        kind = m.group(2)

        def apply(g, ev, src, n=n, kind=kind):
            cs = ev["opts"].setdefault("counters", {})
            if n == "X":
                # CR 107.3m: X of the resolving permanent spell.
                lo = ev.get("lki_obj")
                k = lo.x if lo is not None and ev.get("from") == "stack" else 0
            else:
                k = num(n)
            cs[kind] = cs.get(kind, 0) + k
        p = ReplacementPart("enter_self", lambda g, ev, src: True, apply, text=l)
        p.commutes = True
        return [Static([p], text=l)]
    # ---------------------------------------------------------------- anthems (613.4c, 613.1f)
    m = re.fullmatch(r"(Other )?(.+?) you control get \+(\d+)/\+(\d+)(?: and have (.+?))?\.", l)
    if m:
        f = parse_noun(m.group(2) + " you control")
        if m.group(1):
            f.other = True
        kws = _split_kws(m.group(5)) if m.group(5) else []
        _check_kws(kws)
        return [_anthem(f, int(m.group(3)), int(m.group(4)), kws, l)]
    m = re.fullmatch(r"(Other )?(.+?) you control have (.+?)\.", l)
    if m and re.fullmatch(KWS + r"((,? and |, )" + KWS + r")*", m.group(3)):
        f = parse_noun(m.group(2) + " you control")
        if m.group(1):
            f.other = True
        return [_anthem(f, 0, 0, _split_kws(m.group(3)), l)]
    m = re.fullmatch(r"(Enchanted|Equipped) creature gets \+(\d+)/\+(\d+)(?: and has (.+?))?\.", l)
    if m:
        kws = _split_kws(m.group(4)) if m.group(4) else []
        _check_kws(kws)
        return [_anthem(None, int(m.group(2)), int(m.group(3)), kws, l, attached=True)]
    m = re.fullmatch(r"(Enchanted|Equipped) creature has (.+?)\.", l)
    if m and re.fullmatch(KWS + r"((,? and |, )" + KWS + r")*", m.group(2)):
        return [_anthem(None, 0, 0, _split_kws(m.group(2)), l, attached=True)]
    m = re.fullmatch(r"~ has " + KWS + r" during your turn\.", l)
    if m:
        k = Keyword(m.group(1))

        def grant(g, oid, c, src, k=k):
            c.abilities.append(k)
        # CR 611.3a: a static ability's effect applies only while its condition is true.
        return [Static([LayerPart(6, "self", grant, l)], text=l,
                       condition=lambda g, src: g.s.active == g.ctrl(src))]
    # ---------------------------------------------------------------- rule effects
    if l in ("~ can't block.",):
        return [Static([RulePart("cant_block", "self")], text=l)]
    if l in ("~ can't be blocked.",):
        return [Static([RulePart("unblockable", "self")], text=l)]
    if l in ("~ attacks each combat if able.",):
        return [Static([RulePart("must_attack", "self")], text=l)]
    if l in ("~ can't be countered.",):
        return [Static([RulePart("cant_be_countered", "self")], text=l, zones=("stack",))]
    m = re.fullmatch(r"(.+?) spells you control can't be countered\.", l)
    if m:
        from .triggers import parse_spell_noun
        f = parse_spell_noun(m.group(1).lower())
        f.controller = "you"
        return [Static([RulePart("cant_be_countered", f)], text=l)]
    if l == "You have hexproof.":
        return [Static([RulePart("player_hexproof", "self")], text=l)]
    m = re.fullmatch(r"~ enters tapped with (\w+) stun counters on it\.", l)
    if m:
        n = num(m.group(1))

        def apply(g, ev, src, n=n):
            ev["opts"]["tapped"] = True
            cs = ev["opts"].setdefault("counters", {})
            cs["stun"] = cs.get("stun", 0) + n
        p = ReplacementPart("enter_self", lambda g, ev, src: True, apply, text=l)
        p.commutes = True
        return [Static([p], text=l)]
    m = re.fullmatch(r"As ~ enters, choose a (creature type|color|basic land type)\.", l)
    if m:
        what = m.group(1)

        def apply(g, ev, src, what=what):
            p = ev["opts"].get("controller", ev.get("owner"))
            from .compiler import _CREATURE_TYPES
            if what == "creature type":
                opts = [Opt(f"type:{t}", t) for t in sorted(set(_CREATURE_TYPES))]
                key = "type"
            elif what == "color":
                opts = [Opt(f"color:{c}", c) for c in "WUBRG"]
                key = "color"
            else:
                opts = [Opt(f"type:{t}", t) for t in ("Plains", "Island", "Swamp", "Mountain", "Forest")]
                key = "type"
            a = g.ask(Decision(p, "ChooseType" if key == "type" else "ChooseColor", opts,
                               prompt=f"Choose a {what}."))
            ev["opts"].setdefault("chosen", {})[key] = a
        part = ReplacementPart("enter_self", lambda g, ev, src: True, apply, text=l)
        return [Static([part], text=l)]
    m = re.fullmatch(r"You may have ~ enter as a copy of any creature on the battlefield\.", l)
    if m:
        def apply(g, ev, src):
            p = ev["opts"].get("controller", ev.get("owner"))
            cands = [o for o in already_there(g, ev) if "Creature" in g.chars(o).types]
            if not cands:
                return
            opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("cp", g.obj_sig(o))) for o in cands]
            a = g.ask(Decision(p, "CopyChoice", opts, prompt="Choose a creature to copy (or none)."))
            if a is not None:
                ev["opts"]["copy_of"] = g.base_chars(g.s.objs[a])
                apply_copied_enter_replacements(g, ev, ev["opts"]["copy_of"])
        part = ReplacementPart("enter_self", lambda g, ev, src: True, apply, text=l)
        part.copy_effect = True
        return [Static([part], text=l)]
    if l == "As long as ~ has a counter on it, it can attack as though it didn't have defender.":
        cond = lambda g, oid: any(v > 0 for v in g.s.objs[oid].counters.values())
        return [Static([RulePart("attack_despite_defender", "self")], text=l, condition=cond)]
    if l == "If an opponent would lose life during your turn, they lose twice that much life instead.":
        def applies(g, ev, src):
            ctrl = g.ctrl(src)
            return ev["player"] != ctrl and g.s.active == ctrl
        def apply(g, ev, src):
            ev["n"] *= 2  # CR 614.1a
        return [Static([ReplacementPart("lifeloss", applies, apply, text=l)], text=l)]
    if l == "You may play an additional land on each of your turns.":
        return [Static([RulePart("extra_land", "self")], text=l)]
    if l == "Enchanted creature can't attack or block.":
        return [Static([RulePart("cant_attack", "attached"), RulePart("cant_block", "attached")], text=l)]
    if l == "Enchanted creature can't block.":
        return [Static([RulePart("cant_block", "attached")], text=l)]
    if l == "Enchanted creature doesn't untap during its controller's untap step.":
        return [Static([RulePart("doesnt_untap", "attached")], text=l)]
    m = re.fullmatch(r"Creatures your opponents control enter tapped\.", l)
    if m:
        raise Unsupported("enter-tapped for opponents' creatures")
    # ---------------------------------------------------------------- cost modification
    m = re.fullmatch(r"(.+?) spells you cast cost \{(\d+)\} less to cast\.", l)
    if m:
        from .triggers import parse_spell_noun
        f = parse_spell_noun(m.group(1).lower())
        n = int(m.group(2))
        return [Static([RulePart("cost_mod", "self", who="you", n=-n,
                                 filter=lambda g, c, oid, src, f=f: f.match_chars(g, c, g.s.objs.get(oid), src))], text=l)]
    return None


def _check_kws(kws):
    for k in kws:
        if not re.fullmatch(KWS, k):
            raise Unsupported(f"granted ability {k!r}")



# =============================================================================== extensions
# Each parser returns a list of abilities or None. They run before the templates above.

EXTRA_STATICS = []


def static_parser(fn):
    EXTRA_STATICS.append(fn)
    return fn


def _src_ctrl(g, src):
    if isinstance(src, int):
        return g.ctrl(src)
    return getattr(src, "controller", None)


def _cant_prevent(g, ev):
    from ..engine.effects import _cant_prevent as cp
    return cp(g, ev)


def damage_recipient(text):
    """'other creatures you control', '~', 'you', 'an opponent or a permanent an opponent
    controls' -> pred(g, target, src)."""
    t = text.strip()
    if t == "~":
        return lambda g, tgt, src: tgt == src
    if t == "you":
        return lambda g, tgt, src: tgt == ("player", _src_ctrl(g, src))
    if t == "an opponent":
        return lambda g, tgt, src: isinstance(tgt, tuple) and tgt[1] != _src_ctrl(g, src)
    if t == "an opponent or a permanent an opponent controls":
        def pred(g, tgt, src):
            me = _src_ctrl(g, src)
            if isinstance(tgt, tuple):
                return tgt[1] != me
            return tgt in g.s.objs and g.ctrl(tgt) != me
        return pred
    f = parse_noun(t)

    def pred(g, tgt, src, f=f):
        return not isinstance(tgt, tuple) and tgt in g.s.objs and f.match(g, tgt, src if isinstance(src, int) else None,
                                                                          {"controller": _src_ctrl(g, src)})
    return pred


def prevention_part(kind, recipient, text):
    """CR 615.1a: "Prevent all [combat|noncombat] damage that would be dealt to X." A
    prevention effect is a kind of replacement effect (615.1); damage that can't be prevented
    isn't (615.12)."""
    def applies(g, ev, src):
        if ev["n"] <= 0:
            return False
        if kind == "combat" and not ev.get("combat"):
            return False
        if kind == "noncombat" and ev.get("combat"):
            return False
        if not recipient(g, ev["target"], src):
            return False
        return not _cant_prevent(g, ev)

    def apply(g, ev, src):
        ev["prevented"] = ev.get("prevented", 0) + ev["n"]
        ev["n"] = 0
        ev["cancel"] = True  # CR 120.8: 0 damage isn't dealt; nothing is left to modify
    return ReplacementPart("damage", applies, apply, text=text)


@static_parser
def _prevention_statics(l, cx):
    m = re.fullmatch(r"Prevent all (combat |noncombat )?damage that would be dealt to (.+?)\.", l)
    if m:
        kind = (m.group(1) or "").strip() or None
        return [Static([prevention_part(kind, damage_recipient(m.group(2)), l)], text=l)]
    m = re.fullmatch(r"(Combat )?[Dd]amage can't be prevented\.", l)
    if m:
        # CR 615.12: the rule effect makes prevention effects prevent nothing.
        f = (lambda g, ev: bool(ev.get("combat"))) if m.group(1) else None
        return [Static([RulePart("damage_cant_be_prevented", "self", filter=f)], text=l)]
    return None


def _damage_source_pred(text, cx):
    """'a source you control', 'a red source you control', 'sources you control of the chosen
    type' -> pred(g, ev, src) on the event's source (its characteristics as it deals damage)."""
    t = text.strip()
    m = re.fullmatch(r"(?:an? )?(?:(white|blue|black|red|green) )?sources? you control( of the chosen type)?", t)
    if not m:
        raise Unsupported(f"damage source {t!r}")
    col = COLORS[m.group(1)] if m.group(1) else None
    chosen = bool(m.group(2))

    def pred(g, ev, src, col=col, chosen=chosen):
        sc = ev.get("source_chars")
        if sc is None:
            # An emblem (CR 114.2: controlled by its player; no colors, no types).
            ec = ev.get("source_controller")
            return ec is not None and ec == _src_ctrl(g, src) and col is None and not chosen
        if sc.controller != _src_ctrl(g, src):
            return False
        if col is not None and col not in sc.colors:
            return False
        if chosen:
            ct = g.s.objs[src].chosen.get("type") if src in g.s.objs else None
            if ct is None or ct not in sc.subtypes:
                return False
        return True
    return pred


def damage_mod_part(source_pred, kind, recipient, modify, text):
    def applies(g, ev, src):
        if ev["n"] <= 0 or not isinstance(src, int) or src not in g.s.objs:
            return False
        if kind == "combat" and not ev.get("combat"):
            return False
        if kind == "noncombat" and ev.get("combat"):
            return False
        if recipient is not None and not recipient(g, ev["target"], src):
            return False
        return source_pred(g, ev, src)

    def apply(g, ev, src):
        ev["n"] = modify(g, ev, src)
    return ReplacementPart("damage", applies, apply, text=text)


@static_parser
def _damage_mod_statics(l, cx):
    """CR 614.1a: "If a source would deal damage ..., it deals [more] instead"."""
    m = re.fullmatch(r"If (an? (?:\w+ )?source you control) would deal (noncombat |combat )?damage to (.+?), it deals (double that damage|that much damage plus (\d+)) instead\.", l)
    if m:
        sp = _damage_source_pred(m.group(1), cx)
        kind = (m.group(2) or "").strip() or None
        rec = damage_recipient(m.group(3))
        if m.group(4).startswith("double"):
            mod = lambda g, ev, src: ev["n"] * 2
        else:
            k = int(m.group(5))
            mod = lambda g, ev, src, k=k: ev["n"] + k
        return [Static([damage_mod_part(sp, kind, rec, mod, l)], text=l)]
    m = re.fullmatch(r"Double all damage that (sources you control(?: of the chosen type)?) would deal\.", l)
    if m:
        sp = _damage_source_pred(m.group(1), cx)
        return [Static([damage_mod_part(sp, None, None, lambda g, ev, src: ev["n"] * 2, l)], text=l)]
    m = re.fullmatch(r"If (an? (?:\w+ )?source you control) would deal an amount of (noncombat |combat )?damage less than ~'s power to (.+?), that source deals damage equal to ~'s power instead\.", l)
    if m:
        sp = _damage_source_pred(m.group(1), cx)
        kind = (m.group(2) or "").strip() or None
        rec = damage_recipient(m.group(3))

        def power(g, src):
            p = g.chars(src).power
            return p if p is not None else 0

        def sp2(g, ev, src, sp=sp):
            return sp(g, ev, src) and ev["n"] < power(g, src)
        return [Static([damage_mod_part(sp2, kind, rec, lambda g, ev, src: power(g, src), l)], text=l)]
    return None


@static_parser
def _life_statics(l, cx):
    if l == "Players can't gain life.":
        # CR 119.7, 614.17c: an event that can't happen can be modified only by self-replacement
        # effects, so this is checked like one (before Leyline-style replacements).
        def apply(g, ev, src):
            ev["n"] = 0
            ev["cancel"] = True
        p = ReplacementPart("lifegain", lambda g, ev, src: ev["n"] > 0, apply, self_replacement=True, text=l)
        return [Static([p], text=l)]
    m = re.fullmatch(r"If you would gain life, you gain that much life plus (\d+) instead\.", l)
    if m:
        k = int(m.group(1))

        def apply(g, ev, src, k=k):
            ev["n"] += k
        # CR 119.10: applies only if a life gain event would occur (n > 0).
        p = ReplacementPart("lifegain", lambda g, ev, src: ev["n"] > 0 and ev["player"] == g.ctrl(src), apply, text=l)
        return [Static([p], text=l)]
    return None


def runtime_guard(pred, why, text):
    """A no-op characteristic part that raises Unsupported when `pred(g, obj)` holds while the
    object's characteristics are computed: for rules the engine doesn't implement that matter
    only in some zones (fail loudly when they would matter)."""
    def fn(g, oid, c, src):
        o = g.s.objs.get(oid)
        if o is not None and pred(g, o):
            raise Unsupported(why)
    st = Static([LayerPart(1, "self", fn, text)], text=text, zones=("all",))
    return st


@static_parser
def _leyline(l, cx):
    if l == "If ~ is in your opening hand, you may begin the game with it on the battlefield.":
        # CR 103.6: an action taken from the opening hand once the mulligan process is
        # complete (turn.begin_game_actions); the ability does nothing anywhere else.
        return [Keyword("begin_on_battlefield", text=l)]
    return None


def parse_static_condition(t, cx):
    """'As long as ...' conditions (CR 611.3a) -> predicate(g, src)."""
    from ..engine import effects as fx
    t = t.strip()
    m = re.fullmatch(r"there are (\w+) or more card types among cards (exiled with ~|in your graveyard|in all graveyards)", t)
    if m:
        n = num(m.group(1))
        where = m.group(2)

        def cond(g, src, n=n, where=where):
            if where == "exiled with ~":
                ids = fx.exiled_with_ids(g, src)
            elif where == "in your graveyard":
                ids = list(g.s.players[g.ctrl(src)].graveyard)
            else:
                ids = list(g.s.players[0].graveyard) + list(g.s.players[1].graveyard)
            return len(fx.card_types_among(g, ids)) >= n
        return cond
    m = re.fullmatch(r"you have at least (\d+) life more than your starting life total", t)
    if m:
        k = int(m.group(1))
        # CR 103.4: the starting life total is 20 in a two-player game.
        return lambda g, src, k=k: g.s.players[g.ctrl(src)].life >= 20 + k
    if t == "~ entered this turn":
        return lambda g, src: g.s.objs[src].entered_turn == g.s.turn
    m = re.fullmatch(r"you have (one|two|three) or fewer cards in hand", t)
    if m:
        k = num(m.group(1))
        return lambda g, src, k=k: len(g.s.players[g.ctrl(src)].hand) <= k
    m = re.fullmatch(r"~ has (?:a|one or more) counters? on (?:it|him|her)", t)
    if m:
        return lambda g, src: any(v > 0 for v in g.s.objs[src].counters.values())
    m = re.fullmatch(r"~ has an? ([\w+\-/]+) counter on (?:it|him|her)", t)
    if m:
        k = m.group(1)
        return lambda g, src, k=k: g.s.objs[src].counters.get(k, 0) > 0
    for h in STATIC_CONDITION_HOOKS:
        r = h(t, cx)
        if r is not None:
            return r
    raise Unsupported(f"static condition {t!r}")


# Card modules' "As long as <condition>" conditions: fn(text, cx) -> predicate(g, src) | None,
# tried after the ones above (ref/cards/round2_a2.py).
STATIC_CONDITION_HOOKS = []


def _amount_per(noun, cx):
    """'for each enchantment you control' -> fn(g, src) counting from the source's controller."""
    f = parse_noun(noun)

    def n(g, src, f=f):
        me = g.ctrl(src)
        return sum(1 for o in g.s.battlefield if f.match(g, o, src, {"controller": me}))
    return n


@static_parser
def _more_statics(l, cx):
    # "As long as <condition>, <static ability>."
    m = re.fullmatch(r"As long as (.+?), (.+)", l)
    if m and not m.group(2).startswith(("it can attack as though",)):
        cond = parse_static_condition(m.group(1), cx)
        body = m.group(2)
        body = re.sub(r"^(it|he|she) (gets|has|is)\b", r"~ \2", body)
        body = body[0].upper() + body[1:]
        inner = parse_static(body, cx)
        if not inner or any(a.kind != "static" or a.condition is not None for a in inner):
            raise Unsupported(f"conditional static {l!r}")
        out = []
        for st in inner:
            out.append(Static(st.parts, text=l, condition=cond, zones=st.zones))
        return out
    # Self anthem: "~ gets +N/+M and has K."
    m = re.fullmatch(r"~ gets \+(\d+)/\+(\d+)(?: and has (.+?))?\.", l)
    if m:
        kws = _split_kws(m.group(3)) if m.group(3) else []
        _check_kws(kws)
        st = _anthem("self", int(m.group(1)), int(m.group(2)), kws, l)
        return [st]
    # "Enchanted creature gets +1/+1 for each enchantment you control and has first strike."
    m = re.fullmatch(r"(Enchanted|Equipped) creature gets \+(\d+)/\+(\d+) for each (.+?)(?: and has (.+?))?\.", l)
    if m:
        per = _amount_per(m.group(4), cx)
        kws = _split_kws(m.group(5)) if m.group(5) else []
        _check_kws(kws)
        dp, dt = int(m.group(2)), int(m.group(3))

        def fn(g, oid, c, src, per=per, dp=dp, dt=dt):
            if c.power is not None:
                k = per(g, src)
                c.power += dp * k
                c.toughness += dt * k
        parts = [LayerPart("7c", "attached", fn, l)]
        if kws:
            ks = [Keyword(k) for k in kws]

            def grant(g, oid, c, src, ks=ks):
                c.abilities.extend(ks)
            parts.append(LayerPart(6, "attached", grant, l))
        return [Static(parts, text=l)]
    # "~ can't attack or block unless <condition>."
    m = re.fullmatch(r"~ can't (attack|block|attack or block) unless (.+?)\.", l)
    if m:
        cond = parse_static_condition(m.group(2), cx)
        when = lambda g, oid, src, kw, cond=cond: not cond(g, src)
        rules = {"attack": ["cant_attack"], "block": ["cant_block"],
                 "attack or block": ["cant_attack", "cant_block"]}[m.group(1)]
        return [Static([RulePart(r, "self", when=when) for r in rules], text=l)]
    return None


def entering_chars(g, ev):
    """The characteristics a permanent would have on the battlefield, for replacement effects
    that modify how permanents enter (CR 614.12): copy effects already applied, face-down
    status, the face it enters with."""
    from ..engine.objects import Chars
    from ..cards.model import DB
    opts = ev["opts"]
    if opts.get("copy_of") is not None:
        return opts["copy_of"]
    if opts.get("face_down"):
        c = Chars()
        c.types = {"Creature"}
        return c
    if ev.get("token_obj") is not None:
        return g.base_chars(ev["token_obj"])
    card = DB.get(ev["card"])
    fi = opts.get("face", 0) or 0
    face = card.faces[fi] if fi < len(card.faces) else card.faces[0]
    c = Chars()
    c.name = face.name
    c.types = set(face.types)
    c.subtypes = set(face.subtypes)
    c.supertypes = set(face.supertypes)
    c.colors = set(face.colors)
    # Its own static abilities and the counters the replacement effects applied so far put on
    # it (614.12; Rules.would_have_chars), when the entering card is known.
    w = g.would_have_chars(ev) if hasattr(g, "would_have_chars") else None
    if w is not None:
        c.name, c.power, c.toughness = w.name, w.power, w.toughness
        c.types, c.subtypes = set(w.types), set(w.subtypes)
        c.supertypes, c.colors = set(w.supertypes), set(w.colors)
    # Continuous effects that already exist and would apply to it on the battlefield (614.12):
    # the effect that puts it there with other characteristics ("It's an enchantment. (It's
    # not a creature.)", Enduring cycle; its ruling: it won't be a creature).
    old = g.s.objs.get(ev.get("old"))
    owner = old.owner if old is not None else ev.get("owner")
    for ce in g.s.effects:
        cards = (ce.data or {}).get("enters_cards")
        if cards and (ev["card"], owner) in cards:
            for layer in sorted(k for k in ce.parts if isinstance(k, int)):
                ce.parts[layer](g, None, c, ce)
    return c


@static_parser
def _others_enter_tapped(l, cx):
    """"Nonbasic lands enter tapped." / "Creatures your opponents control enter tapped."
    (CR 614.1d, 614.12)."""
    m = re.fullmatch(r"(.+?) enter tapped\.", l)
    if not m or m.group(1).startswith("~"):
        return None
    noun = m.group(1)
    opp = False
    mm = re.fullmatch(r"(.+?) your opponents control", noun)
    if mm:
        noun, opp = mm.group(1), True
    f = parse_noun(noun)
    if f.controller is not None or f.owner is not None:
        raise Unsupported(f"enter tapped for {m.group(1)!r}")

    def applies(g, ev, src, f=f, opp=opp):
        if ev.get("to") != "battlefield":
            return False
        if opp and ev["opts"].get("controller", ev.get("owner")) == g.ctrl(src):
            return False
        c = entering_chars(g, ev)
        return f.match_chars(g, c, None, src)

    def apply(g, ev, src):
        ev["opts"]["tapped"] = True
    p = ReplacementPart("zone", applies, apply, text=l)
    p.commutes = True
    return [Static([p], text=l)]


@static_parser
def _lands_are_basic_type(l, cx):
    """"Nonbasic lands are Mountains." (CR 305.7, layer 4): the land loses its other land types
    and the abilities from its rules text and copiable effects, and gains the mana ability of
    the new type."""
    m = re.fullmatch(r"(.+?) are (Plains|Islands|Swamps|Mountains|Forests)\.", l)
    if not m:
        return None
    f = parse_noun(m.group(1))
    if "Land" not in f.types:
        raise Unsupported(f"type-setting static {l!r}")
    basic = {"Plains": "Plains", "Islands": "Island", "Swamps": "Swamp", "Mountains": "Mountain",
             "Forests": "Forest"}[m.group(2)]

    def fn(g, oid, c, src, basic=basic):
        from .compiler import subtypes_of
        from ..engine.effects import intrinsic_mana_ability
        from ..cards.model import BASIC_LAND_TYPES
        c.subtypes = (c.subtypes - subtypes_of("Land")) | {basic}
        c.abilities = [intrinsic_mana_ability(BASIC_LAND_TYPES[basic])]
    return [Static([LayerPart(4, f, fn, l)], text=l)]


def _reflexive(text, cx):
    """"When you do, <effect>" attached to a replacement effect (CR 603.12, 614.6): a reflexive
    triggered ability that triggers when the replacement is applied."""
    from .compiler import compile_effect
    from ..engine.ir import Triggered
    eff, specs, _ = compile_effect(text[0].upper() + text[1:], cx, ability=True)
    return Triggered(None, eff, specs, text="When you do, " + text)


def reflexive_index(chars):
    """Canonical number of a reflexive "When you do" trigger (603.12) of an object: after its
    own triggered abilities, so `trigger:<ref>:<n>` stays unambiguous (format §3.1)."""
    return sum(1 for a in chars.abilities if a.kind == "triggered") + 1


def _fire_reflexive(g, ab, src, ev):
    from ..engine.triggers import PendingTrigger
    c = g.chars(src)
    g.s.pending.append(PendingTrigger(ab, src, c, g.ctrl(src), ev, {}, reflexive_index(c)))


@static_parser
def _zone_replacements(l, cx):
    if l == "If a card or token would be put into a graveyard from anywhere, exile it instead.":
        # CR 614.1a
        def apply(g, ev, src):
            ev["to"] = "exile"
        p = ReplacementPart("zone", lambda g, ev, src: ev.get("to") == "graveyard", apply, text=l)
        p.redirect = "exile"
        return [Static([p], text=l)]
    m = re.fullmatch(r"If (a|another) (.+?) would die, exile it instead\.(?: When you do, (.+))?", l)
    if m:
        f = parse_noun(m.group(2))
        other = m.group(1) == "another"
        ab = _reflexive(m.group(3), cx) if m.group(3) else None

        def applies(g, ev, src, f=f, other=other):
            # "Dies" = put into a graveyard from the battlefield (CR 700.4); the creature's
            # current characteristics (it's still on the battlefield).
            if ev.get("from") != "battlefield" or ev.get("to") != "graveyard":
                return False
            old = ev.get("old")
            if old not in g.s.objs or (other and old == src):
                return False
            c = g.chars(old)
            return "Creature" in c.types and f.match(g, old, src, {"controller": g.ctrl(src)})

        def apply(g, ev, src, ab=ab):
            ev["to"] = "exile"
            if ab is not None:
                _fire_reflexive(g, ab, src, ev)
        p = ReplacementPart("zone", applies, apply, text=l)
        if ab is None:
            p.redirect = "exile"
        return [Static([p], text=l)]
    return None


@static_parser
def _cda_pt(l, cx):
    """CR 604.3, 613.4a: characteristic-defining P/T abilities (layer 7a, all zones)."""
    m = re.fullmatch(r"~'s power is equal to the number of card types among cards in (all graveyards|your graveyard) and its toughness is equal to that number plus (\d+)\.", l)
    if not m:
        return None
    where, plus = m.group(1), int(m.group(2))
    from ..engine import effects as fx

    def fn(g, oid, c, src, where=where, plus=plus):
        o = g.s.objs.get(oid)
        if where == "all graveyards":
            ids = list(g.s.players[0].graveyard) + list(g.s.players[1].graveyard)
        else:
            p = c.controller if o is not None and o.zone in ("battlefield", "stack") else (o.owner if o else 0)
            ids = list(g.s.players[p].graveyard)
        n = len(fx.card_types_among(g, ids))
        c.power, c.toughness = n, n + plus
    st = Static([LayerPart("7a", "self", fn, l)], text=l, zones=("all",))
    st.cda = True
    return [st]


@static_parser
def _equip_cost_reduction(l, cx):
    m = re.fullmatch(r"Equip abilities you activate that target ~ cost \{(\d+)\} less to activate\.", l)
    if not m:
        return None
    # The reduction depends on the equip ability's target: targets are chosen before the total
    # cost is determined (CR 602.2b, 601.2c, 601.2f).
    def may_apply(g, oid, ab, src):
        return getattr(ab, "keyword", None) == "equip" and g.ctrl(oid) == g.ctrl(src)

    def filt_t(g, oid, ab, src, targets):
        return may_apply(g, oid, ab, src) and any(src in grp for grp in targets)
    return [Static([RulePart("ability_cost_mod", "self", filter=lambda g, oid, ab, src: False, filter_t=filt_t,
                             may_apply=may_apply, n=-int(m.group(1)))], text=l)]


@static_parser
def _doubling(l, cx):
    # Token doublers (CR 614.1a; one creation event, 111.2): the older wording (Doubling Season)
    # and the current one (Elspeth, Storm Slayer; Adrix and Nev).
    if l in ("If an effect would create one or more tokens under your control, it creates twice that many of those tokens instead.",
             "If one or more tokens would be created under your control, twice that many of those tokens are created instead."):
        def apply(g, ev, src):
            ev["n"] *= 2
        p = ReplacementPart("token", lambda g, ev, src: ev["n"] > 0 and ev["controller"] == g.ctrl(src), apply, text=l)
        return [Static([p], text=l)]
    if l == "If an effect would put one or more counters on a permanent you control, it puts twice that many of those counters on that permanent instead.":
        # Counters put by an effect (not as a cost or turn-based action: those have no
        # source here). Entering with counters is putting counters on it too (CR 122.6).
        def applies(g, ev, src):
            return (ev["n"] > 0 and ev.get("source") is not None and ev["obj"] in g.s.objs
                    and g.s.objs[ev["obj"]].zone == "battlefield" and g.ctrl(ev["obj"]) == g.ctrl(src))

        def apply(g, ev, src):
            ev["n"] *= 2

        def applies_enter(g, ev, src):
            return (ev.get("to") == "battlefield" and ev["opts"].get("controller", ev.get("owner")) == g.ctrl(src)
                    and any(v > 0 for v in (ev["opts"].get("counters") or {}).values()))

        def apply_enter(g, ev, src):
            ev["opts"]["counters"] = {k: v * 2 for k, v in ev["opts"]["counters"].items()}
        return [Static([ReplacementPart("counters", applies, apply, text=l),
                        ReplacementPart("zone", applies_enter, apply_enter, text=l)], text=l)]
    m = re.fullmatch(r"Each other (\w+) you control enters with an additional \+1/\+1 counter on it for each \1 you already control\.", l)
    if m:
        st = singular(m.group(1))

        def count(g, src, ev, st=st):
            # "Each [type] you already control": other than the one(s) entering, including the
            # source itself (Giada, Font of Hope ruling 2024-11-08).
            me = g.ctrl(src)
            return sum(1 for o in already_there(g, ev) if g.ctrl(o) == me and st in g.chars(o).subtypes)

        def applies(g, ev, src, st=st):
            if ev.get("to") != "battlefield" or ev["opts"].get("controller", ev.get("owner")) != g.ctrl(src):
                return False
            if ev.get("old") == src or src in (ev.get("entering") or ()):
                return False
            c = entering_chars(g, ev)
            return st in c.subtypes and count(g, src, ev) > 0

        def apply(g, ev, src):
            cs = ev["opts"].setdefault("counters", {})
            cs["+1/+1"] = cs.get("+1/+1", 0) + count(g, src, ev)
        return [Static([ReplacementPart("zone", applies, apply, text=l)], text=l)]
    return None


@static_parser
def _self_cost_reduction(l, cx):
    """"This spell costs {N} less to cast for each ..." (CR 601.2f). It modifies what that
    object costs to cast, so it functions in any zone it could be cast from and on the stack
    (CR 113.6d-e); the engine reads it from the face being cast."""
    m = re.fullmatch(r"~ costs \{(\d+)\} less to cast for each (.+?)\.", l)
    if not m:
        return None
    return [self_cost_reduction(int(m.group(1)), m.group(2), l)]


def self_cost_reduction(k, noun, text):
    """RulePart 'self_cost_mod' with n(g, oid, p) = -k * count (generic mana only)."""
    count = count_noun(noun)
    part = RulePart("self_cost_mod", "self", n=lambda g, oid, p: -k * count(g, oid, p))
    return Static([part], text=text, zones=("all",))


def count_noun(noun):
    """'creature card in your graveyard' / 'artifact you control' -> count(g, oid, p)."""
    m = re.fullmatch(r"(.+?) cards? (in your graveyard|you own in exile and in your graveyard)", noun)
    if m:
        zones = ("graveyard",) if m.group(2) == "in your graveyard" else ("exile", "graveyard")
        what = m.group(1)
        # "instant and sorcery card": each card that is an instant or a sorcery.
        what = re.sub(r"^(\w+) and (\w+)$", r"\1 or \2", what)
        f = parse_noun(what)

        def count(g, oid, p, f=f, zones=zones):
            pl = g.s.players[p]
            return sum(1 for z in zones for x in getattr(pl, z) if x != oid and f.match(g, x, oid, {"controller": p}))
        return count
    m = re.fullmatch(r"(.+?) you control", noun)
    if m:
        f = parse_noun(m.group(1))

        def count(g, oid, p, f=f):
            return sum(1 for x in g.s.battlefield if g.ctrl(x) == p and f.match(g, x, oid, {"controller": p}))
        return count
    raise Unsupported(f"cost reduction count {noun!r}")


@static_parser
def _chosen_type_anthem(l, cx):
    m = re.fullmatch(r"(Other )?(.+?) you control of the chosen type get \+(\d+)/\+(\d+)(?: and have (.+?))?\.", l)
    if not m:
        return None
    base = parse_noun(m.group(2) + " you control")
    other = bool(m.group(1))

    def aff(g, oid, c, src, base=base, other=other):
        # CR 607.2d: "the chosen type" is the type chosen for this object as it entered.
        t = g.s.objs[src].chosen.get("type") if src in g.s.objs else None
        if t is None or t not in c.subtypes or (other and oid == src):
            return False
        return base.match_chars(g, c, g.s.objs.get(oid), src)
    kws = _split_kws(m.group(5)) if m.group(5) else []
    _check_kws(kws)
    return [_anthem(aff, int(m.group(3)), int(m.group(4)), kws, l)]


def _copy_exceptions(text, cx):
    """CR 707.9b: "except ..." clauses of a copy effect -> fn(copiable Chars) (they become part
    of the copy's copiable values)."""
    from .compiler import _CREATURE_TYPES
    fns = []
    for part in re.split(r",? and (?=(?:it|he|she|his|her)\b)", text):
        part = part.strip()
        m = re.fullmatch(r"(?:it's|he's|she's) an? ((?:[A-Z][a-z]+ ?)+) in addition to (?:its|his|her) other types", part)
        if m:
            subs = m.group(1).split()
            fns.append(lambda c, subs=subs: c.subtypes.update(subs))
            continue
        m = re.fullmatch(r"(?:he's|she's|it's) an? (\d+)/(\d+) ((?:[A-Z][a-z]+ ?)+) in addition to (?:its|his|her) other types", part)
        if m:
            p, t, subs = int(m.group(1)), int(m.group(2)), m.group(3).split()

            def f(c, p=p, t=t, subs=subs):
                c.power, c.toughness = p, t
                c.subtypes.update(subs)
                # CR 707.9d: an exception giving specific P/T values doesn't copy a
                # characteristic-defining ability that defines P/T (Superior Spider-Man copying
                # Tarmogoyf is 4/4; the rule's Quicksilver Gargantuan example).
                c.abilities = [a for a in c.abilities if not (
                    getattr(a, "kind", None) == "static" and getattr(a, "cda", False)
                    and any(getattr(pt, "kind", None) == "layer" and str(getattr(pt, "layer", "")) == "7a" for pt in a.parts))]
            fns.append(f)
            continue
        m = re.fullmatch(r"(?:it|he|she) has " + KWS, part)
        if m:
            k = Keyword(m.group(1))
            fns.append(lambda c, k=k: c.abilities.append(k))
            continue
        m = re.fullmatch(r"(?:his|her|its) name is ~", part)
        if m:
            name = cx.name
            fns.append(lambda c, name=name: setattr(c, "name", name))
            continue
        raise Unsupported(f"copy exception {part!r}")

    def apply_all(c):
        for f in fns:
            f(c)
    return apply_all


@static_parser
def _enter_as_copy(l, cx):
    m = re.fullmatch(r"You may have ~ enter as a copy of any (creature on the battlefield|creature card in a graveyard)"
                     r"( with mana value less than or equal to the amount of mana spent to cast ~)?"
                     r"(?:, except (.+?))?\.(?: When you do, (.+))?", l)
    if not m or (not m.group(2) and not m.group(3) and not m.group(4)):
        return None  # the plain form is handled by the template below
    where, mv_cap = m.group(1), bool(m.group(2))
    exc = _copy_exceptions(m.group(3), cx) if m.group(3) else None
    refl = None
    if m.group(4):
        if m.group(4) != "exile that card.":
            raise Unsupported(f"copy reflexive {m.group(4)!r}")
        from ..engine.ir import Triggered
        from ..engine import effects as fx

        def exile_it(g, ctx):
            card = (ctx.get("data") or {}).get("card")
            o = g.s.objs.get(card)
            if o is not None and o.zone == "graveyard":
                g.move(card, "exile")
        refl = Triggered(None, fx.Fn_(exile_it, "exile that card"), [], text="When you do, exile that card.")

    def apply(g, ev, src):
        from ..engine.mana import mana_value
        p = ev["opts"].get("controller", ev.get("owner"))
        if where == "creature on the battlefield":
            cands = [o for o in g.s.battlefield if "Creature" in g.chars(o).types]
        else:
            cands = [o for q in (0, 1) for o in g.s.players[q].graveyard if "Creature" in g.base_chars(g.s.objs[o]).types]
        if mv_cap:
            lo = ev.get("lki_obj")
            spent = sum((lo.mana_spent or {}).values()) if lo is not None and ev.get("from") == "stack" else 0
            cands = [o for o in cands if mana_value(g.chars(o).mana_cost if g.s.objs[o].zone == "battlefield"
                                                    else g.base_chars(g.s.objs[o]).mana_cost) <= spent]
        if not cands:
            return
        opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("cp", g.obj_sig(o))) for o in cands]
        a = g.ask(Decision(p, "CopyChoice", opts, prompt="Choose what to copy (or none)."))
        if a is None:
            return
        cop = g.base_chars(g.s.objs[a]).copy()
        if exc is not None:
            exc(cop)
        ev["opts"]["copy_of"] = cop
        apply_copied_enter_replacements(g, ev, cop)
        # Another copy effect was applied after this one (the
        # copied card's own "enter as a copy", 614.12): this copy effect's linked "When you do"
        # doesn't trigger (CR 707.9g). Superior Spider-Man copying a Superior Spider-Man card,
        # then a Grizzly Bears card: only the Bears card is exiled (rt4-f 1017978).
        hk = getattr(g, "hooks", None)
        noop = refl is not None and ev["opts"].get("copy_of") is not cop and getattr(hk, "o3_707_9g_noop", False)
        if refl is not None and (ev["opts"].get("copy_of") is cop or noop):
            # CR 603.12: "When you do" triggers as the permanent enters as a copy.
            from ..engine.triggers import PendingTrigger
            src = ev.get("old")
            if src is None and ev.get("token_obj") is not None:
                src = ev["token_obj"].id  # a token entering as a copy (707.5)
            data = {"card": a}
            if noop:
                # Opt-in alignment for lockstep runs (Hooks.o3_707_9g_noop; off by default, never used by
                # ref.corpus): the suppressed trigger is put on the stack anyway and keeps its card.
                hk.o3_707_9g_hit = True
            idx = reflexive_index(cop)
            if noop:
                # Same opt-in alignment: the suppressed trigger is numbered after the final copy's own
                # triggered abilities.
                idx = reflexive_index(ev["opts"]["copy_of"])
            g.s.pending.append(PendingTrigger(refl, src, cop, p, ev, data, idx))
    part = ReplacementPart("enter_self", lambda g, ev, src: True, apply, text=l)
    part.copy_effect = True
    return [Static([part], text=l)]


@static_parser
def _chosen_type_self(l, cx):
    if l == "~ is the chosen type in addition to its other types.":
        def fn(g, oid, c, src):
            t = g.s.objs[oid].chosen.get("type") if oid in g.s.objs else None
            if t is not None:
                c.subtypes.add(t)  # CR 205.1b, 607.2d
        return [Static([LayerPart(4, "self", fn, l)], text=l)]
    if l == "If a triggered ability of another creature you control of the chosen type triggers, it triggers an additional time.":
        # CR 603.2d: applied where abilities trigger (Rules._add_pending reads this marker).
        st = Static([], text=l)
        st.extra_trigger = "chosen_type_creature"
        return [st]
    return None


def apply_copied_enter_replacements(g, ev, cop):
    """CR 614.12: once a copy effect has modified how a permanent enters, the "as this enters"
    replacement effects of the copied characteristics apply too (e.g. a Clone copying Roaming
    Throne makes its own creature-type choice, 707.6). The engine looks only at the entering
    card's own face, so they are applied here (keys recorded in ev['applied'])."""
    applied = ev.setdefault("applied", set())
    # Opt-in alignment for lockstep runs (off by default): another reading, in which each newly
    # copied "enter as a copy" ability applies to the same entry again (another CopyChoice each
    # time, until "done" or a creature without such an ability). Default reading: the printed
    # ability and one copied one, each once (CR 614.5). Capped at 20 copies per entry.
    gen = ()
    if getattr(g.s, "patch_sq1_follow", False):
        n = ev.get("sq1_depth", 0)
        if n and n < 20:
            gen = (n,)
        ev["sq1_depth"] = n + 1
    for ai, ab in enumerate(cop.abilities):
        if ab.kind != "static":
            continue
        for pi, part in enumerate(ab.parts):
            # Distinct from the entering card's own ("self", ai, pi). :
            # keyed by the part's text too, so a chain's later copied card's "enters with" isn't
            # taken for the earlier copied card's ability at the same index (Mockingbird copying a
            # Mockingbird, then Flitterwing Nuisance: its -1/-1 counter, CR 614.12; campaign21
            # 1340670). The same text again (a Mockingbird copy of a Mockingbird copy) still
            # applies once.
            key = ("copied", ai, pi, getattr(part, "text", None)) + gen
            if part.kind == "replacement" and part.event == "enter_self" and key not in applied \
                    and part.applies(g, ev, None):
                applied.add(key)
                if gen and getattr(part, "copy_effect", False):
                    hook = getattr(getattr(g, "hooks", None), "patch_hit", None)
                    if hook is not None:
                        hook("patch:copy-chain-follow")
                part.apply(g, ev, None)
                if ev.get("cancel"):
                    return
    # The counters a permanent of the copied types enters with (the entering card's own face was
    # looked at before the copy was chosen): a Saga's lore counter (CR 714.3a: Mockingbird copying
    # Summon: Esper Maduin, campaign13 830205), a planeswalker's loyalty (306.5b), a battle's
    # defense (310.4b), from the copiable values (707.2).
    try:
        from ..cards.model import DB
        own = DB.get(ev["card"]).faces[ev["opts"].get("face", 0)]
    except Exception:  # noqa: BLE001 - no face to compare: the copy alone decides
        own = None
    cs = ev["opts"].setdefault("counters", {})
    if "Saga" in cop.subtypes and ("lore",) not in applied and not (own is not None and "Saga" in own.subtypes):
        applied.add(("lore",))
        cs["lore"] = cs.get("lore", 0) + 1
    if "Planeswalker" in cop.types and isinstance(cop.loyalty, int) and ("loyalty",) not in applied \
            and not (own is not None and "Planeswalker" in own.types):
        applied.add(("loyalty",))
        cs["loyalty"] = cs.get("loyalty", 0) + cop.loyalty
    if "Battle" in cop.types and isinstance(cop.defense, int) and ("defense",) not in applied \
            and not (own is not None and "Battle" in own.types):
        applied.add(("defense",))
        cs["defense"] = cs.get("defense", 0) + cop.defense


@static_parser
def _damage_heals_other_damage(l, cx):
    """Wolverine, Fierce Fighter: "If damage would be dealt to ~, instead that damage is dealt, but
    all other damage already dealt to him is healed." A replacement effect (CR 614.1a): the event
    becomes "that damage is dealt and the damage marked on him before it is removed". Damage dealt
    at the same time (combat damage from several sources, 510.2) is one event: only damage marked
    before it is healed. Deathtouch damage still destroys him (704.5h counts damage dealt, not
    marked)."""
    if l != "If damage would be dealt to ~, instead that damage is dealt, but all other damage already dealt to him is healed.":
        return None

    def applies(g, ev, src):
        return ev["target"] == src and ev["n"] > 0 and src in g.s.objs and g.s.objs[src].zone == "battlefield"

    def apply(g, ev, src):
        batch = getattr(g, "_lifelink_batch", None)
        mark = g.__dict__.setdefault("_heal_batches", {})
        if batch is not None and mark.get(src) is batch:
            return  # simultaneous damage: healed once, before this batch
        mark[src] = batch
        if g.s.objs[src].damage:
            g.s.objs[src].damage = 0
            g.s.touch()
    part = ReplacementPart("damage", applies, apply, text=l)
    part.commutes = True
    return [Static([part], text=l)]



@static_parser
def _chosen_name_statics(l, cx):
    """Petrified Hamlet: "Activated abilities of sources with the chosen name can't be activated
    unless they're mana abilities." (CR 602.5: a rule may forbid activating; 605.1a mana
    abilities stay allowed) and "Lands with the chosen name have "{T}: Add {C}."" (layer 6,
    613.1f). "Sources" are objects in any zone (cycling from a hand too). No name chosen yet: no
    effect."""
    from ..engine.ir import RulePart

    def chosen(g, src):
        o = g.s.objs.get(src) if isinstance(src, int) else None
        return (o.chosen or {}).get("name") if o is not None else None

    if l == "Activated abilities of sources with the chosen name can't be activated unless they're mana abilities.":
        def aff(g, oid, src):
            n = chosen(g, src)
            return n is not None and oid in g.s.objs and g.chars(oid).name == n
        # "unless they're mana abilities": the part doesn't apply to a mana ability (the engine
        # asks with mana=True), so Clarion Conqueror's ban (mana abilities too) stays separate.
        return [Static([RulePart("cant_activate", aff, when=lambda g, oid, src, kw: not kw.get("mana"))], text=l)]
    m = re.fullmatch(r'Lands with the chosen name have "(\{T\}: Add \{[WUBRGC]\}\.)"', l)
    if m:
        from .compiler import compile_line
        granted = compile_line(m.group(1), cx)
        if granted == "spell" or not granted:
            raise Unsupported("granted ability " + repr(m.group(1)))
        ks = tuple(granted)

        def aff6(g, oid, c, src):
            n = chosen(g, src)
            return n is not None and "Land" in c.types and c.name == n

        def grant(g, oid, c, src, ks=ks):
            c.abilities.extend(ks)
        return [Static([LayerPart(6, aff6, grant, l)], text=l)]
    return None
# =============================================================================== card support

@static_parser
def _per_n_cards_in_graveyard(l, cx):
    """"~ gets +2/+0 for every seven cards in your graveyard." (CR 613.4c: layer 7c; the count
    is the number of whole groups of N cards, CR 107.1a rounds down)."""
    m = re.fullmatch(r"~ gets \+(\d+)/\+(\d+) for every (\w+) cards in your graveyard\.", l)
    if not m:
        return None
    dp, dt, k = int(m.group(1)), int(m.group(2)), num(m.group(3))

    def fn(g, oid, c, src, dp=dp, dt=dt, k=k):
        if c.power is None:
            return
        n = len(g.s.players[c.controller].graveyard) // k  # layer 2 already applied
        c.power += dp * n
        c.toughness += dt * n
    return [Static([LayerPart("7c", "self", fn, l)], text=l)]


def _has_adventure(g, oid):
    """CR 715.2: an adventurer card "has an Adventure" in every zone (Hearth Elemental ruling
    2023-09-01: even if it was never cast as an Adventure). Its inset spell has the Adventure
    spell type; an Omen card (CR 720, same two-part data layout) doesn't have an Adventure."""
    from .model import DB
    o = g.s.objs.get(oid)
    if o is None or o.face_down:
        return False
    card = DB.get(o.card)
    return card.layout == "adventure" and any("Adventure" in f.subtypes for f in card.faces[1:])


def _x_cost_amount(t):
    """'X' of "This spell costs {X} less to cast, where X is ..." -> n(g, oid, p)."""
    if t == "the number of cards in your graveyard that are instant cards, sorcery cards, and/or have an Adventure":
        def n(g, oid, p):
            out = 0
            for x in g.s.players[p].graveyard:
                if x == oid:
                    continue
                c = g.chars(x)
                if {"Instant", "Sorcery"} & c.types or _has_adventure(g, x):
                    out += 1
            return out
        return n
    m = re.fullmatch(r"the greatest mana value among (.+?) you control", t)
    if m:
        f = parse_noun(m.group(1))

        def n(g, oid, p, f=f):
            # CR 202.3: a permanent's mana value from its mana cost (X is 0 on the battlefield).
            vals = [g.chars(x).mana_value for x in g.s.battlefield
                    if g.ctrl(x) == p and f.match(g, x, oid, {"controller": p})]
            return max(vals) if vals else 0
        return n
    raise Unsupported(f"cost reduction amount {t!r}")


@static_parser
def _self_cost_reduction_x(l, cx):
    """"This spell costs {X} less to cast, where X is ..." (CR 601.2f: generic mana only,
    118.7a; the total can't go below {0})."""
    m = re.fullmatch(r"~ costs \{X\} less to cast, where X is (.+)\.", l)
    if not m:
        return None
    amt = _x_cost_amount(m.group(1))
    part = RulePart("self_cost_mod", "self", n=lambda g, oid, p, amt=amt: -amt(g, oid, p))
    return [Static([part], text=l, zones=("all",))]


_PERM_TYPE_WORDS = {"artifacts": "Artifact", "creatures": "Creature", "planeswalkers": "Planeswalker",
                    "enchantments": "Enchantment", "lands": "Land", "battles": "Battle"}


def _type_list(t):
    """'artifacts, creatures, and planeswalkers' -> ['Artifact', 'Creature', 'Planeswalker']."""
    words = [w for w in re.split(r",? and |, |,? or ", t.strip()) if w]
    out = []
    for w in words:
        if w not in _PERM_TYPE_WORDS:
            return None
        out.append(_PERM_TYPE_WORDS[w])
    return out


@static_parser
def _cant_be_activated(l, cx):
    """"Activated abilities of artifacts, creatures, and planeswalkers can't be activated."
    (CR 602.5: a rule that keeps abilities from being activated; mana abilities too, 605.3a.
    "Artifacts" etc. are permanents, CR 109.2: cycling from a hand isn't affected.)"""
    m = re.fullmatch(r"Activated abilities of (.+?) can't be activated\.", l)
    if not m:
        return None
    types = _type_list(m.group(1))
    if types is None:
        raise Unsupported(f"activation restriction {l!r}")

    def aff(g, oid, src, types=tuple(types)):
        o = g.s.objs.get(oid)
        return o is not None and o.zone == "battlefield" and bool(set(types) & g.chars(oid).types)
    return [Static([RulePart("cant_activate", aff)], text=l)]


@static_parser
def _entering_doesnt_trigger(l, cx):
    """Doorkeeper Thrull: "Artifacts and creatures entering don't cause abilities to trigger."
    (checked in the engine's trigger dispatch; replacement effects such as "enters tapped" are
    unaffected, ruling 2024-02-02)."""
    m = re.fullmatch(r"(.+?) entering don't cause abilities to trigger\.", l)
    if not m:
        return None
    types = _type_list(m.group(1).lower())
    if types is None:
        raise Unsupported(f"trigger suppression {l!r}")
    return [Static([RulePart("entering_doesnt_trigger", "self", types=tuple(types))], text=l)]


@static_parser
def _ability_cost_reduction_tokens(l, cx):
    """"Activated abilities of artifact tokens you control cost {1} less to activate." (CR
    602.2b, 601.2f: generic mana only; can reduce a {1} cost to {0}, Mutagen Man ruling)."""
    m = re.fullmatch(r"Activated abilities of (.+?) you control cost \{(\d+)\} less to activate\.", l)
    if not m:
        return None
    f = parse_noun(m.group(1) + " you control")
    k = int(m.group(2))

    def filt(g, oid, ab, src, f=f):
        o = g.s.objs.get(oid)
        return (o is not None and o.zone == "battlefield" and g.ctrl(oid) == g.ctrl(src)
                and f.match(g, oid, src, {"controller": g.ctrl(src)}))
    return [Static([RulePart("ability_cost_mod", "self", filter=filt, n=-k)], text=l)]


@static_parser
def _draw_that_many_plus(l, cx):
    """"If you would draw one or more cards, you draw that many cards plus one instead." A
    replacement of the whole draw instruction, before replacements of individual draws (CR
    614.1a; Quantum Riddler rulings 2025-07-25)."""
    m = re.fullmatch(r"If you would draw one or more cards, you draw that many cards plus (one|two) instead\.", l)
    if not m:
        return None
    k = num(m.group(1))

    def applies(g, ev, src):
        return ev["n"] >= 1 and ev["player"] == g.ctrl(src)

    def apply(g, ev, src, k=k):
        ev["n"] += k
    part = ReplacementPart("draw_n", applies, apply, text=l)
    part.commutes = True
    return [Static([part], text=l)]
