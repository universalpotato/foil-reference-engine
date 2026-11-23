"""Effect primitives, selectors and filters used by compiled card abilities."""
from .ir import Effect, Activated, Cost, ManaProduce, LayerPart, Keyword, TargetSpec
from .objects import ContEffect, Obj, StackItem
from .decisions import Decision, Opt
from ..cards.model import DB
from .. import Unsupported


def _nocopy(cls):
    cls.__deepcopy__ = lambda self, memo: self
    cls.__copy__ = lambda self: self
    return cls


# ============================================================================ filters

@_nocopy
class Filter:
    """Object filter for noun phrases ("creature an opponent controls with mana value 3 or
    less"). Characteristics are read through g.chars (current, after layers)."""

    def __init__(self, types=(), any_types=(), subtypes=(), any_subtypes=(), not_types=(),
                 not_subtypes=(), controller=None, other=False, token=None, colors=(),
                 not_colors=(), colorless=None, multicolored=None, mv=None, power=None,
                 toughness=None, tapped=None, attacking=None, blocking=None, attacking_or_blocking=None,
                 name=None, supertypes=(), not_supertypes=(), keywords=(), not_keywords=(),
                 custom=None, owner=None, text="", spell=False, nonland=False, has_counters=None,
                 not_self=False, permanent=False, card=False, any_colors=()):
        self.types = tuple(types)
        self.any_types = tuple(any_types)
        self.subtypes = tuple(subtypes)
        self.any_subtypes = tuple(any_subtypes)
        self.not_types = tuple(not_types)
        self.not_subtypes = tuple(not_subtypes)
        self.controller = controller
        self.other = other
        self.token = token
        self.colors = tuple(colors)
        self.not_colors = tuple(not_colors)
        self.colorless = colorless
        self.multicolored = multicolored
        self.mv = mv
        self.power = power
        self.toughness = toughness
        self.tapped = tapped
        self.attacking = attacking
        self.blocking = blocking
        self.attacking_or_blocking = attacking_or_blocking
        self.name = name
        self.supertypes = tuple(supertypes)
        self.not_supertypes = tuple(not_supertypes)
        self.keywords = tuple(keywords)
        self.not_keywords = tuple(not_keywords)
        self.custom = custom
        self.owner = owner
        self.text = text
        self.spell = spell
        self.nonland = nonland
        self.has_counters = has_counters
        self.permanent = permanent
        self.card = card
        self.any_colors = tuple(any_colors)

    def __repr__(self):
        return f"Filter({self.text!r})"

    def _ctrl_of(self, g, src, ctx):
        if ctx and ctx.get("controller") is not None:
            return ctx["controller"]
        if isinstance(src, int):
            if src in g.s.objs:
                return g.ctrl(src)
            c = g.s.lki.get(src)
            return c.controller if c else None
        return None

    def match(self, g, oid, src, ctx=None):
        s = g.s
        o = s.objs.get(oid)
        if o is None:
            return False
        c = g.chars(oid)
        return self.match_chars(g, c, o, src, ctx)

    def match_chars(self, g, c, o, src, ctx=None):
        s = g.s
        for t in self.types:
            if t not in c.types:
                return False
        if self.any_types and not (set(self.any_types) & c.types):
            return False
        for t in self.not_types:
            if t in c.types:
                return False
        for t in self.subtypes:
            if t not in c.subtypes:
                return False
        if self.any_subtypes and not (set(self.any_subtypes) & c.subtypes):
            return False
        for t in self.not_subtypes:
            if t in c.subtypes:
                return False
        for t in self.supertypes:
            if t not in c.supertypes:
                return False
        for t in self.not_supertypes:
            if t in c.supertypes:
                return False
        if self.permanent and not ({"Artifact", "Creature", "Enchantment", "Land", "Planeswalker", "Battle"} & c.types):
            return False
        if self.nonland and "Land" in c.types:
            return False
        if self.controller is not None:
            me = self._ctrl_of(g, src, ctx)
            ctrl = c.controller if o is None or o.zone in ("battlefield", "stack") else o.owner
            if self.controller == "you" and ctrl != me:
                return False
            if self.controller == "opp" and ctrl == me:
                return False
        if self.owner is not None:
            me = self._ctrl_of(g, src, ctx)
            if self.owner == "you" and o.owner != me:
                return False
            if self.owner == "opp" and o.owner == me:
                return False
        if self.other and o is not None and src == o.id:
            return False
        if self.token is not None and o is not None and o.token != self.token:
            return False
        for col in self.colors:
            if col not in c.colors:
                return False
        if self.colors == () and False:
            pass
        for col in self.not_colors:
            if col in c.colors:
                return False
        if self.any_colors and not (set(self.any_colors) & c.colors):
            return False
        if self.colorless is True and c.colors:
            return False
        if self.colorless is False and not c.colors:
            return False
        if self.multicolored is True and len(c.colors) < 2:
            return False
        if self.mv is not None:
            from .mana import mana_value
            x = o.x if o is not None and o.zone == "stack" else 0
            if not _cmp(mana_value(c.mana_cost, x), self.mv, g, src, ctx):
                return False
        if self.power is not None and (c.power is None or not _cmp(c.power, self.power, g, src, ctx)):
            return False
        if self.toughness is not None and (c.toughness is None or not _cmp(c.toughness, self.toughness, g, src, ctx)):
            return False
        if self.tapped is not None and o is not None and o.tapped != self.tapped:
            return False
        comb = s.combat
        if self.attacking is not None:
            if (comb is not None and o is not None and o.id in comb.attackers) != self.attacking:
                return False
        if self.blocking is not None:
            if (comb is not None and o is not None and o.id in comb.blockers) != self.blocking:
                return False
        if self.attacking_or_blocking:
            if not (comb is not None and o is not None and (o.id in comb.attackers or o.id in comb.blockers)):
                return False
        if self.name is not None and c.name != self.name:
            return False
        for k in self.keywords:
            if not c.has_kw(k):
                return False
        for k in self.not_keywords:
            if c.has_kw(k):
                return False
        if self.has_counters is not None and o is not None:
            if self.has_counters == "any":
                if not any(v > 0 for v in o.counters.values()):
                    return False
            elif o.counters.get(self.has_counters, 0) <= 0:
                return False
        if self.custom is not None and not self.custom(g, c, o, src, ctx):
            return False
        return True

    def match_stack(self, g, it, ctx):
        """Abilities on the stack ("target activated or triggered ability")."""
        return self.custom is not None and self.custom(g, None, it, None, ctx)


def _cmp(v, spec, g, src, ctx):
    op, n = spec
    if callable(n):
        n = n(g, src, ctx)
    return {"<=": v <= n, ">=": v >= n, "==": v == n, "<": v < n, ">": v > n}[op]


# ============================================================================ amounts

@_nocopy
class Amount:
    """Number expressions: int, 'X', counts."""

    def __init__(self, fn, text=""):
        self.fn = fn
        self.text = text

    def __call__(self, g, ctx):
        return self.fn(g, ctx)


def amount(v):
    if isinstance(v, int):
        return Amount(lambda g, ctx, v=v: v, str(v))
    if v == "X":
        return Amount(lambda g, ctx: ctx.get("x", 0), "X")
    if isinstance(v, Amount):
        return v
    raise Unsupported(f"amount {v!r}")


def count_amount(filt):
    return Amount(lambda g, ctx: sum(1 for oid in g.s.battlefield if filt.match(g, oid, ctx.get("source"), ctx)),
                  f"count({filt.text})")


# ============================================================================ selectors

@_nocopy
class Sel:
    """Resolves to a list of refs: obj ids or ('player', p)."""

    def __init__(self, fn, text="", single=True):
        self.fn = fn
        self.text = text
        self.single = single

    def get(self, g, ctx):
        return self.fn(g, ctx)

    def __repr__(self):
        return f"Sel({self.text})"


def target(i=0, j=None):
    """Legal targets of target word i (CR 608.2b: illegal ones are unaffected)."""
    def fn(g, ctx):
        groups = ctx.get("mode_targets") if ctx.get("mode_targets") is not None else ctx.get("legal")
        if groups is None:
            groups = ctx.get("targets") or []
        if i >= len(groups):
            return []
        grp = groups[i]
        return list(grp) if j is None else ([grp[j]] if j < len(grp) else [])
    return Sel(fn, f"target{i}")


def self_sel():
    def fn(g, ctx):
        src = ctx.get("source")
        o = g.s.objs.get(src)
        if o is None:
            return []
        return [src]
    return Sel(fn, "self")


def self_permanent():
    """~ as a permanent: only if it's still the same object on the battlefield."""
    def fn(g, ctx):
        src = ctx.get("source")
        o = g.s.objs.get(src)
        return [src] if o is not None and o.zone == "battlefield" else []
    return Sel(fn, "self-permanent")


def self_lki():
    """~ as the object an effect needs information from (CR 113.7a, 608.2h): the permanent if
    it's still on the battlefield, else its last known information (e.g. "Create a token that's
    a copy of this Equipment" after the source was sacrificed in response)."""
    def fn(g, ctx):
        src = ctx.get("source")
        o = g.s.objs.get(src)
        if o is not None and o.zone == "battlefield":
            return [src]
        return [src] if src in g.s.lki_obj else []
    return Sel(fn, "self-lki")


def you():
    return Sel(lambda g, ctx: [("player", ctx["controller"])], "you")


def opponent():
    return Sel(lambda g, ctx: [("player", 1 - ctx["controller"])], "opponent")


def each_player():
    return Sel(lambda g, ctx: [("player", g.s.active), ("player", 1 - g.s.active)], "each player", single=False)


def all_matching(filt, zone="battlefield"):
    def fn(g, ctx):
        if zone == "battlefield":
            return [oid for oid in list(g.s.battlefield) if filt.match(g, oid, ctx.get("source"), ctx)]
        raise Unsupported(f"all {zone}")
    return Sel(fn, f"all {filt.text}", single=False)


def event_obj(key="new"):
    """The object of the triggering event ("it", "that creature")."""
    def fn(g, ctx):
        ev = ctx.get("event") or {}
        x = ev.get(key)
        if x is None:
            x = ev.get("obj")
        return [x] if x is not None and x in g.s.objs else []
    return Sel(fn, f"event.{key}")


def event_player(key="player"):
    def fn(g, ctx):
        ev = ctx.get("event") or {}
        p = ev.get(key)
        return [("player", p)] if p is not None else []
    return Sel(fn, f"event.{key}")


def var(name):
    return Sel(lambda g, ctx: [x for x in (ctx.get("vars", {}).get(name) or []) if (isinstance(x, tuple) or x in g.s.objs)], f"var:{name}")


def controller_of(sel):
    def fn(g, ctx):
        out = []
        for r in sel.get(g, ctx):
            if isinstance(r, tuple):
                out.append(r)
            else:
                out.append(("player", g.ctrl(r)))
        return out
    return Sel(fn, f"controller of {sel.text}")


def players_of(sel, g, ctx):
    out = []
    for r in sel.get(g, ctx):
        if isinstance(r, tuple) and r[0] == "player":
            out.append(r[1])
    return out


def objs_of(sel, g, ctx):
    return [r for r in sel.get(g, ctx) if not isinstance(r, tuple)]


# ============================================================================ effects

@_nocopy
class E(Effect):
    """Base for primitive effects with a text label."""
    text = ""

    def __repr__(self):
        return f"{type(self).__name__}({self.text})"


class Damage(E):
    def __init__(self, n, to, source=None, text=""):
        self.n = amount(n)
        self.to = to
        self.source = source
        self.text = text

    def run(self, g, ctx):
        n = self.n(g, ctx)
        src = ctx.get("source")
        sc = None
        if self.source is not None:
            srcs = self.source.get(g, ctx)
            if not srcs:
                return
            src = srcs[0]
        elif src not in g.s.objs:
            sc = g.s.lki.get(src) or ctx.get("source_chars")
        prev = g.damage_batch_start()  # all recipients at once (one lifelink gain, CR 119.9)
        # An emblem's ability has no source object; the emblem is controlled by its player (114.2).
        ectl = ctx.get("controller") if src is None and sc is None else None
        for r in self.to.get(g, ctx):
            g.deal_damage(src, r, n, source_chars=sc, source_controller=ectl)
        g.damage_batch_end(prev)


class Draw(E):
    def __init__(self, n, who=None, text=""):
        self.n = amount(n)
        self.who = who or you()
        self.text = text

    def run(self, g, ctx):
        n = self.n(g, ctx)
        for p in players_of(self.who, g, ctx):
            drawn = g.draw(p, n)
            ctx.setdefault("vars", {})["drawn"] = drawn


class GainLife(E):
    def __init__(self, n, who=None, text=""):
        self.n = amount(n)
        self.who = who or you()
        self.text = text

    def run(self, g, ctx):
        n = self.n(g, ctx)
        for p in players_of(self.who, g, ctx):
            g.gain_life(p, n, source=ctx.get("source"))


class LoseLife(E):
    def __init__(self, n, who, text=""):
        self.n = amount(n)
        self.who = who
        self.text = text

    def run(self, g, ctx):
        n = self.n(g, ctx)
        tot = 0
        for p in players_of(self.who, g, ctx):
            tot += g.lose_life(p, n, source=ctx.get("source"))
        ctx.setdefault("vars", {})["life_lost"] = tot


class Destroy(E):
    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        # A target that left the battlefield earlier in this resolution is a new object (CR
        # 400.7): unaffected (608.2b), and the old id is gone from s.objs.
        objs = [o for o in objs_of(self.sel, g, ctx) if o in g.s.objs and g.s.objs[o].zone == "battlefield"]
        if len(objs) <= 1:
            for o in objs:
                g.destroy(o)
            return
        # Simultaneous destruction (CR 701.8): one batch.
        batch = []
        for o in objs:
            if g.chars(o).has_kw("indestructible"):
                continue
            ev = g.replace("destroy", {"type": "destroy", "obj": o, "applied": set()})
            if not ev.get("cancel"):
                batch.append((o, "graveyard", {"cause": "destroy"}))
        g.move_batch(batch)


class MoveTo(E):
    """Exile / return to hand / put into graveyard / put on library."""
    reports_done = True

    def __init__(self, sel, zone, pos="top", text="", link=None, tapped=False, controller=None,
                 remember=None, under_owner=True, face_down=False):
        self.sel = sel
        self.zone = zone
        self.pos = pos
        self.text = text
        self.link = link
        self.tapped = tapped
        self.controller = controller
        self.remember = remember
        self.face_down = face_down

    def run(self, g, ctx):
        objs = [o for o in objs_of(self.sel, g, ctx)]
        moves = []
        for o in objs:
            opts = {"pos": self.pos, "tapped": self.tapped}
            if self.link is not None:
                opts["link"] = (ctx.get("source"), self.link)
            if self.face_down:
                opts["face_down"] = True
            if self.zone == "battlefield":
                ob = g.s.objs[o]
                opts["controller"] = ctx["controller"] if self.controller == "you" else ob.owner
            moves.append((o, self.zone, opts))
        res = g.move_batch(moves)
        kept = [r for r in res if r is not None]
        ctx.setdefault("vars", {})["last_done"] = bool(kept)
        if self.remember:
            ctx.setdefault("vars", {})[self.remember] = kept
        if self.zone == "exile":
            ctx.setdefault("vars", {})["exiled"] = kept


class Tap(E):
    def __init__(self, sel, untap=False, text=""):
        self.sel = sel
        self.untap_ = untap
        self.text = text

    def run(self, g, ctx):
        for o in objs_of(self.sel, g, ctx):
            if g.s.objs[o].zone != "battlefield":
                continue
            if self.untap_:
                g.untap(o)
            else:
                g.tap(o)


class PutCounters(E):
    def __init__(self, sel, kind, n, text=""):
        self.sel = sel
        self.kind = kind
        self.n = amount(n)
        self.text = text

    def run(self, g, ctx):
        n = self.n(g, ctx)
        hk = getattr(g, "hooks", None)
        if n == 0 and getattr(hk, "o13_negative_counters", False) and self.kind == "+1/+1" \
                and getattr(self.n, "text", "") in ("~'s power", "its power", "this creature's power"):
            # Opt-in alignment for lockstep runs (Hooks.o13_negative_counters; off by default, never
            # used by ref.corpus): a negative count removes up to that many counters. By default a
            # negative number is zero (CR 107.1b).
            sc = self_chars(g, ctx)
            raw = (sc.power or 0) if sc is not None else 0
            if raw < 0:
                for r in self.sel.get(g, ctx):
                    if not isinstance(r, tuple) and g.s.objs[r].zone == "battlefield":
                        have = g.s.objs[r].counters.get("+1/+1", 0)
                        if have:
                            g.remove_counters(r, "+1/+1", min(have, -raw))
                            hk.o13_negative_counters_hit = True
                ctx.setdefault("vars", {})["did"] = False
                return
        put = 0
        for r in self.sel.get(g, ctx):
            if isinstance(r, tuple):
                g.player_counters(r[1], self.kind, n)
                put += n
            elif g.s.objs[r].zone == "battlefield":
                put += g.add_counters(r, self.kind, n, source=ctx.get("source")) or 0
        # CR 603.12: a following "When you do" triggers only if counters were put (Earthbender
        # Ascension destroyed with its landfall trigger on the stack gets no quest counter).
        ctx.setdefault("vars", {})["did"] = put > 0


class Pump(E):
    """"gets +N/+M until end of turn" (CR 611.2c: affected set locked as it begins)."""

    def __init__(self, sel, p, t, duration="eot", keywords=(), text="", set_pt=None):
        self.sel = sel
        self.p = amount(p) if p is not None else None
        self.t = amount(t) if t is not None else None
        self.duration = duration
        self.keywords = tuple(keywords)
        self.text = text
        self.set_pt = set_pt

    def run(self, g, ctx):
        objs = [o for o in objs_of(self.sel, g, ctx) if g.s.objs[o].zone == "battlefield"]
        if not objs:
            return
        s = g.s
        ce = ContEffect(s.new_id())
        ce.source = ctx.get("source")
        ce.controller = ctx["controller"]
        ce.timestamp = s.new_ts()
        ce.duration = self.duration
        ce.affected = objs
        if self.p is not None:
            dp, dt = self.p(g, ctx), self.t(g, ctx)

            def pt(g, oid, c, ce, dp=dp, dt=dt):
                if c.power is not None:
                    c.power += dp
                    c.toughness += dt
            ce.parts["7c"] = pt
        if self.set_pt is not None:
            sp, st = self.set_pt

            def setpt(g, oid, c, ce, sp=sp, st=st):
                c.power, c.toughness = sp, st
            ce.parts["7b"] = setpt
        if self.keywords:
            kws = [Keyword(k) for k in self.keywords]

            def grant(g, oid, c, ce, kws=kws):
                c.abilities.extend(kws)
            ce.parts[6] = grant
        s.effects.append(ce)
        s.touch()


class AddEffect(E):
    """Generic continuous effect from a resolving spell/ability with explicit layer parts."""

    def __init__(self, sel, parts, duration="eot", text="", kind="layer", data=None):
        self.sel = sel
        self.parts = parts
        self.duration = duration
        self.text = text
        self.kind = kind
        self.data = data or {}

    def run(self, g, ctx):
        s = g.s
        ce = ContEffect(s.new_id())
        ce.source = ctx.get("source")
        ce.controller = ctx["controller"]
        ce.timestamp = s.new_ts()
        ce.duration = self.duration
        ce.kind = self.kind
        ce.data = dict(self.data)
        if self.sel is not None:
            ce.affected = [o for o in objs_of(self.sel, g, ctx) if g.s.objs[o].zone == "battlefield"]
            if not ce.affected and self.kind == "layer":
                return
        ce.parts = dict(self.parts)
        if self.kind == "rule":
            ce.data.setdefault("part", None)
        s.effects.append(ce)
        s.touch()


class CreateToken(E):
    def __init__(self, carddef_fn, n=1, who=None, tapped=False, attacking=False, text="",
                 remember=None):
        self.carddef_fn = carddef_fn
        self.n = amount(n)
        self.who = who or you()
        self.tapped = tapped
        self.attacking = attacking
        self.text = text
        self.remember = remember

    def run(self, g, ctx):
        n = self.n(g, ctx)
        out = []
        for p in players_of(self.who, g, ctx):
            att = None
            if self.attacking and g.s.combat is not None:
                src = ctx.get("source")
                att = g.s.combat.attackers.get(src, ("player", 1 - p))
            out += g.create_token(self.carddef_fn(), p, n, tapped=self.tapped, attacking=att)
        if self.remember:
            ctx.setdefault("vars", {})[self.remember] = out


class CounterSpell(E):
    def __init__(self, sel, text="", exile=False):
        self.sel = sel
        self.text = text
        self.exile = exile

    def run(self, g, ctx):
        for r in self.sel.get(g, ctx):
            ok = g.counter(r, to="exile" if self.exile else "graveyard")
            ctx.setdefault("vars", {})["countered"] = ok


class Scry(E):
    def __init__(self, n, surveil=False, text=""):
        self.n = amount(n)
        self.surveil = surveil
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        n = self.n(g, ctx)
        pl = g.s.players[p]
        kind = "Surveil" if self.surveil else "Scry"
        keep, away = [], []
        k = min(n, len(pl.library))
        # CR 701.22a / 701.25a: the top N are looked at together, before any is placed (the
        # differential harness's look_hook below may still realign each card as it is asked).
        g.s.know.looks(g, p, pl.library[:k])
        for i in range(k):
            g.look_hook(p, i)
            oid = pl.library[i]
            if self.surveil:
                opts = [Opt("top", "top"), Opt("graveyard", "gy")]
            else:
                opts = [Opt("top", "top"), Opt("bottom", "bottom")]
            a = g.ask(Decision(p, kind, opts, subject=g.ref(oid), prompt=f"{kind}: {g.name_of(oid)}"))
            (keep if a == "top" else away).append(oid)
        top = pl.library[:k]
        rest = pl.library[k:]
        if len(keep) > 1:
            ordered = []
            remaining = list(keep)
            while len(remaining) > 1:
                opts = [Opt(f"choose:{g.ref(o)}", o, sig=("ord", g.obj_sig(o))) for o in remaining]
                a = g.ask(Decision(p, "OrderTop", opts, prompt="Choose the next card from the top."))
                ordered.append(a)
                remaining.remove(a)
            keep = ordered + remaining
        if self.surveil:
            pl.library[:] = keep + away + rest
            # 701.25a: "the rest on top of your library in any order": the order of the kept cards
            # is the surveilling player's hidden choice (401.4). The cards put into the graveyard
            # are public there (400.2), so the split needs no record (unlike scry's, below).
            g.s.know.reorder(g, keep, chooser=p)
            moved = g.move_batch([(o, "graveyard", {"cause": "surveil"}) for o in away])
            # "the card put into your graveyard this way" (Enlightened Confidant).
            ctx.setdefault("vars", {})["surveiled"] = [x for x in moved if x is not None]
            g.emit({"type": "surveil", "player": p, "n": n})
        else:
            if len(away) > 1:
                ordered = []
                remaining = list(away)
                while len(remaining) > 1:
                    opts = [Opt(f"choose:{g.ref(o)}", o, sig=("ordb", g.obj_sig(o))) for o in remaining]
                    a = g.ask(Decision(p, "OrderBottom", opts, prompt="Choose the next card for the bottom."))
                    ordered.append(a)
                    remaining.remove(a)
                away = ordered + remaining
            pl.library[:] = keep + rest + away
            # How many cards went each way is public; the order, and for the other player which
            # card went where, is the scrying player's hidden choice (401.4): anyone else who knew
            # these cards knows only that they are among them (knowledge.py `scried`).
            g.s.know.scried(g, p, keep, away)
            g.emit({"type": "scry", "player": p, "n": n})
        g.s.touch()


class Mill(E):
    def __init__(self, n, who=None, text=""):
        self.n = amount(n)
        self.who = who or you()
        self.text = text

    def run(self, g, ctx):
        n = self.n(g, ctx)
        for p in players_of(self.who, g, ctx):
            res = g.mill(p, n)
            ctx.setdefault("vars", {})["milled"] = res


class Discard(E):
    def __init__(self, n, who, chooser="self", filt=None, text="", random=False):
        self.n = amount(n) if n != "all" else None
        self.who = who
        self.chooser = chooser
        self.filt = filt
        self.text = text
        self.random = random

    def run(self, g, ctx):
        for p in players_of(self.who, g, ctx):
            pl = g.s.players[p]
            if self.n is None:
                for oid in list(pl.hand):
                    g.discard(p, oid, cause="effect")
                continue
            n = self.n(g, ctx)
            chooser = p if self.chooser == "self" else ctx["controller"]
            out = []
            for _ in range(n):
                cands = [o for o in pl.hand if self.filt is None or self.filt.match(g, o, ctx.get("source"), ctx)]
                if not cands:
                    break
                if self.random:
                    o = g.s.rng.choice(cands)
                else:
                    opts = [Opt(f"choose:{g.ref(o)}", o, sig=("disc", g.obj_sig(o))) for o in cands]
                    o = g.ask(Decision(chooser, "Discard", opts, prompt="Choose a card to discard."))
                out.append(g.discard(p, o, cause="effect"))
            ctx.setdefault("vars", {})["discarded"] = out


class DiscardThenDrawPer(E):
    """"<players> each discard a card. For each of those opponents who didn't discard a card with
    mana value N or greater, draw a card." (Hollow Marauder). Players with an empty hand discard
    nothing, so they count (CR 701.9)."""

    def __init__(self, who, mv, text=""):
        self.who = who
        self.mv = mv
        self.text = text

    def run(self, g, ctx):
        from .mana import mana_value
        draws = 0
        for p in players_of(self.who, g, ctx):
            hand = list(g.s.players[p].hand)
            big = False
            if hand:
                opts = [Opt(f"choose:{g.ref(o)}", o, sig=("disc", g.obj_sig(o))) for o in hand]
                o = g.ask(Decision(p, "Discard", opts, prompt="Choose a card to discard."))
                mv = mana_value(g.base_chars(g.s.objs[o]).mana_cost)
                g.discard(p, o, cause="effect")
                big = mv >= self.mv
            if not big:
                draws += 1
        for _ in range(draws):
            g.draw(ctx["controller"])


class Sacrifice(E):
    def __init__(self, who, filt, n=1, text=""):
        self.who = who
        self.filt = filt
        self.n = amount(n)
        self.text = text

    def run(self, g, ctx):
        n = self.n(g, ctx)
        every = []
        for p in players_of(self.who, g, ctx):
            chosen = []
            for _ in range(n):
                cands = [o for o in g.s.battlefield if g.ctrl(o) == p and o not in chosen
                         and self.filt.match(g, o, ctx.get("source"), {"controller": p})]
                if not cands:
                    break
                opts = [Opt(f"choose:{g.ref(o)}", o, sig=("sac", g.obj_sig(o))) for o in cands]
                chosen.append(g.ask(Decision(p, "Sacrifice", opts, prompt="Choose a permanent to sacrifice.")))
            every.extend(chosen)
        # Every player chooses, then the permanents are sacrificed at the same time (CR 101.4,
        # 701.21a; zones.sacrifice_batch).
        g.sacrifice_batch(every)


class Search(E):
    """Search your library for up to n cards matching filt, put them into zone (CR 701.23)."""

    def __init__(self, filt, n=1, zone="hand", tapped=False, reveal=True, shuffle=True, text="",
                 up_to=True):
        self.filt = filt
        self.n = n
        self.zone = zone
        self.tapped = tapped
        self.reveal = reveal
        self.shuffle = shuffle
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        pl = g.s.players[p]
        # CR 701.23a: the player searches their library (abilities that trigger on a search
        # trigger even if nothing is found, 701.23b, 701.23f).
        g.emit({"type": "search", "player": p, "library": p})
        found = []
        while len(found) < self.n:
            cands = [o for o in pl.library if o not in found and self.filt.match(g, o, ctx.get("source"), ctx)]
            if not cands:
                break
            opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("srch", g.obj_sig(o))) for o in cands]
            a = g.ask(Decision(p, "Search", opts, prompt=f"Search: {self.text}"))
            if a is None:
                break
            found.append(a)
        moves = [(o, self.zone, {"tapped": self.tapped, "controller": p}) for o in found]
        res = g.move_batch(moves)
        ctx.setdefault("vars", {})["found"] = [r for r in res if r]
        if self.shuffle:
            g.shuffle(p)


class May(E):
    """"You may [effect]" (CR 603.5 / 608.2c): a yes/no choice at resolution."""

    def __init__(self, effect, who="you", text=""):
        self.effect = effect
        self.who = who
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        a = g.ask(Decision(p, "OptionalEffect", [Opt("yes", True), Opt("no", False)],
                           subject=g.ref(ctx["source"]) if ctx.get("source") in g.s.objs else None,
                           prompt=f"Use the optional effect? {self.text}"))
        ctx.setdefault("vars", {})["did"] = a
        if a:
            self.effect.run(g, ctx)


class If(E):
    def __init__(self, cond, then, otherwise=None, text=""):
        self.cond = cond
        self.then = then
        self.otherwise = otherwise
        self.text = text

    def run(self, g, ctx):
        if self.cond(g, ctx):
            self.then.run(g, ctx)
        elif self.otherwise is not None:
            self.otherwise.run(g, ctx)


class ExileUntilLeaves(E):
    """"Exile X until ~ leaves the battlefield" (CR 610.3). The return happens immediately
    after the host leaves (610.3); if it already left, nothing is exiled (610.3a-b)."""

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        s = g.s
        host = ctx.get("source")
        ho = s.objs.get(host)
        if ho is None or ho.zone != "battlefield":
            return  # 610.3a-b: the specified event already happened
        objs = [o for o in objs_of(self.sel, g, ctx) if s.objs[o].zone == "battlefield"]
        res = g.move_batch([(o, "exile", {"link": (host, "until"), "cause": "until"}) for o in objs])
        ids = [r for r in res if r is not None]
        if ids:
            s.until_leaves.append({"host": host, "objs": ids, "zone": "exile"})
            s.touch()


class Fight(E):
    def __init__(self, a, b, text=""):
        self.a = a
        self.b = b
        self.text = text

    def run(self, g, ctx):
        A = [o for o in objs_of(self.a, g, ctx) if g.s.objs[o].zone == "battlefield"]
        B = [o for o in objs_of(self.b, g, ctx) if g.s.objs[o].zone == "battlefield"]
        if not A or not B:
            return  # CR 701.14b
        a, b = A[0], B[0]
        pa, pb = g.chars(a).power or 0, g.chars(b).power or 0
        g.deal_damage(a, b, pa)
        g.deal_damage(b, a, pb)


class AddMana(E):
    def __init__(self, colors, text=""):
        self.colors = colors
        self.text = text

    def run(self, g, ctx):
        from .mana import Unit
        p = ctx["controller"]
        for c in self.colors:
            g.s.players[p].pool.append(Unit(c, source=ctx.get("source")))


# ============================================================================ helpers

def intrinsic_mana_ability(color):
    """CR 305.6: "{T}: Add [mana symbol]" of a basic land type."""
    return _INTRINSIC[color]


_INTRINSIC = {}
for _c in "WUBRG":
    _a = Activated(Cost(tap=True), effect=None, text="{T}: Add {%s}." % _c, mana=ManaProduce([(_c,)]))
    _a.intrinsic = True
    _INTRINSIC[_c] = _a


class CounterUnlessPays(E):
    """"Counter it/target spell unless its controller pays [cost]" (ward: CR 702.21a)."""

    def __init__(self, cost, life=0, sel=None, text="", exile=False):
        self.cost = list(cost)
        self.life = life
        self.sel = sel
        self.text = text
        self.exile = exile

    def run(self, g, ctx):
        s = g.s
        if self.sel is not None:
            refs = self.sel.get(g, ctx)
        else:
            item = (ctx.get("data") or {}).get("item")
            it = next((x for x in s.stack if x.id == item), None)
            if it is None:
                # Ward whose spell or ability already left the stack: "counter it unless that
                # player pays" means "that player may pay; if they don't, counter it" (CR
                # 118.12a), so the player may still pay, with nothing left to counter (Abrupt
                # Decay ruling 2024-04-12: the ward cost may be paid though it can't counter).
                payer = (ctx.get("data") or {}).get("player")
                if payer is not None:
                    self._offer(g, payer, None)
                return
            refs = [it.obj if it.kind == "spell" else ("stack", it.id)]
        for r in refs:
            if isinstance(r, tuple):
                it = next((x for x in s.stack if x.id == r[1]), None)
                payer = it.controller if it else None
            else:
                if r not in s.objs or s.objs[r].zone != "stack":
                    continue
                payer = g.ctrl(r)
            if payer is None:
                continue
            if self._offer(g, payer, r):
                continue
            g.counter(r, to="exile" if self.exile else "graveyard")

    def _offer(self, g, payer, r):
        """The payer may pay the cost (CR 118.12a) if they can (118.3); True if they paid."""
        s = g.s
        if self.life and s.players[payer].life < self.life:
            return False
        if self.cost and not g.mana_payable(payer, self.cost, 0, {"kind": "unless", "obj": r if isinstance(r, int) else None, "chars": None}):
            return False
        a = g.ask(Decision(payer, "PayUnless", [Opt("yes", True), Opt("no", False)],
                           prompt=f"Pay {self.text or 'the cost'} to avoid being countered?"))
        if a:
            if self.life:
                g.pay_life(payer, self.life)
            if self.cost:
                g.pay_mana(payer, self.cost, 0, {"kind": "unless", "obj": None, "chars": None}, None)
        return bool(a)


class Attach(E):
    """Attach the source (Equipment/Aura) to an object (CR 701.3)."""

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        src = ctx.get("source")
        o = g.s.objs.get(src)
        if o is None or o.zone != "battlefield":
            return
        ts = objs_of(self.sel, g, ctx)
        if not ts:
            return
        t = ts[0]
        if t == src or g.s.objs[t].zone != "battlefield":
            return
        if o.attached_to == t:
            return  # 701.3b: already attached - nothing happens
        o.attached_to = t
        o.timestamp = g.s.new_ts()  # 613.7e
        g.s.touch()
        g.emit({"type": "attached", "obj": src, "to": t})


class RuleEffect(E):
    """A rule-modifying effect from a resolving spell/ability ("can't block this turn")."""

    def __init__(self, sel, rule, duration="eot", text="", **data):
        self.sel = sel
        self.rule = rule
        self.duration = duration
        self.text = text
        self.data = data

    def run(self, g, ctx):
        from .ir import RulePart
        objs = [o for o in objs_of(self.sel, g, ctx) if g.s.objs[o].zone == "battlefield"]
        if not objs:
            return
        s = g.s
        ce = ContEffect(s.new_id())
        ce.source = ctx.get("source")
        ce.controller = ctx["controller"]
        ce.timestamp = s.new_ts()
        ce.duration = self.duration
        ce.kind = "rule"
        ce.affected = objs
        ce.data = {"rule": self.rule, "part": RulePart(self.rule, "locked", **self.data)}
        s.effects.append(ce)
        s.touch()


class Dig(E):
    """"Look at the top N cards of your library. Put K of them into your hand and the rest on
    the bottom of your library in any order." """

    def __init__(self, n, k, rest_to="bottom", text="", order="any", reveal_creature=None):
        # n: how many cards are looked at (an Amount: "the top X cards", X = lands you control);
        # k: how many of them go to the hand (an int, or an Amount for "instead put two").
        # order: how the rest goes to the bottom: "any" (the player orders them, CR 401.4) or
        # "random" ("in a random order").
        self.n = amount(n)
        self.k = amount(k)
        self.rest_to = rest_to
        self.text = text
        self.order = order
        self.reveal_creature = reveal_creature

    def run(self, g, ctx):
        p = ctx["controller"]
        pl = g.s.players[p]
        n = min(max(0, self.n(g, ctx)), len(pl.library))
        if n <= 0:
            return
        # CR 401.2/701.16a: the cards are looked at in the library (hidden information); the
        # differential harness aligns our hidden order with what the EUT showed first.
        g.dig_hook(p, n)
        top = pl.library[:n]
        chosen = []
        if self.reveal_creature is not None:
            # Break Out: "You may reveal a creature card from among them" (CR 701.20a), then the
            # revealed card's handling; the rest goes to the bottom below.
            self.reveal_creature(g, ctx, p, top, chosen)
        else:
            for _ in range(min(max(0, self.k(g, ctx)), len(top))):
                cands = [o for o in top if o not in chosen]
                opts = [Opt(f"choose:{g.ref(o)}", o, sig=("dig", g.obj_sig(o))) for o in cands]
                chosen.append(g.ask(Decision(p, "Dig", opts, prompt=f"Choose a card to put into your hand ({self.text})")))
            g.move_batch([(o, "hand", {}) for o in chosen])
        rest = [o for o in top if o not in chosen and o in pl.library]
        if self.order == "random":
            # "in a random order": no player choice (CR 401.4 doesn't apply).
            g.s.rng.shuffle(rest)
            ordered = rest
            g.s.know.reorder(g, rest, chooser=None)  # nobody can tell these cards apart now
        else:
            # Bottom in any order: a choice of order (ordered one at a time, CR 401.4).
            ordered = []
            while len(rest) > 1:
                opts = [Opt(f"choose:{g.ref(o)}", o, sig=("digb", g.obj_sig(o))) for o in rest]
                a = g.ask(Decision(p, "OrderBottom", opts, prompt="Choose the next card to put on the bottom."))
                ordered.append(a)
                rest.remove(a)
            ordered += rest
            g.s.know.reorder(g, ordered, chooser=p)  # 401.4: the order isn't revealed
        for o in ordered:
            pl.library.remove(o)
            pl.library.append(o)
        g.s.touch()


# ============================================================================ more effects

def _new_ce(g, ctx, duration="eot", kind="layer"):
    s = g.s
    ce = ContEffect(s.new_id())
    ce.source = ctx.get("source")
    ce.controller = ctx["controller"]
    ce.timestamp = s.new_ts()
    ce.duration = duration
    ce.kind = kind
    return ce


class CopyToken(E):
    """"Create a token that's a copy of X" (CR 707.2, 111.4): the token's copiable values are
    X's copiable values."""

    def __init__(self, sel, text="", n=1, extra=None):
        self.sel = sel
        self.text = text
        self.n = n
        self.extra = extra

    def run(self, g, ctx):
        for oid in objs_of(self.sel, g, ctx):
            o = g.s.objs.get(oid)
            if o is not None and (o.zone == "battlefield" or oid not in g.s.lki_obj):
                cop = g.base_chars(o)
                card = o.card
            else:
                lo = g.s.lki_obj.get(oid)
                if lo is None:
                    continue
                cop = g.base_chars(lo)
                card = lo.card
            cop.controller = ctx["controller"]
            if self.extra is not None:
                self.extra(cop)
            from ..cards.model import DB
            ids = g.create_token(DB.get(card), ctx["controller"], self.n, copiable=cop)
            ctx.setdefault("vars", {})["token"] = ids


class PreventNext(E):
    """"Prevent the next N damage that would be dealt to X this turn" (CR 615.7)."""

    def __init__(self, sel, n, text=""):
        self.sel = sel
        self.n = n
        self.text = text

    def run(self, g, ctx):
        from .ir import ReplacementPart
        refs = self.sel.get(g, ctx)
        if not refs:
            return
        ce = _new_ce(g, ctx, "eot", "replacement")
        ce.data = {"event": "damage", "left": self.n, "who": refs[0]}

        def applies(g, ev, ce):
            return ev["target"] == ce.data["who"] and ce.data["left"] > 0 and not _cant_prevent(g, ev)

        def apply(g, ev, ce):
            k = min(ce.data["left"], ev["n"])
            ce.data["left"] -= k
            ev["n"] -= k
        ce.data["part"] = ReplacementPart("damage", applies, apply, text=self.text)
        g.s.effects.append(ce)


def _cant_prevent(g, ev):
    for part, src in g.rule_parts("damage_cant_be_prevented"):
        f = part.data.get("filter")
        if f is None or f(g, ev):
            return True
    return False


class DieExileInstead(E):
    """"If that creature would die this turn, exile it instead" (CR 614.1a)."""

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        from .ir import ReplacementPart
        objs = [o for o in objs_of(self.sel, g, ctx) if g.s.objs[o].zone == "battlefield"]
        if not objs:
            return
        ce = _new_ce(g, ctx, "eot", "replacement")
        ce.data = {"event": "zone", "objs": objs}

        def applies(g, ev, ce):
            return ev.get("old") in ce.data["objs"] and ev.get("from") == "battlefield" and ev.get("to") == "graveyard"

        def apply(g, ev, ce):
            ev["to"] = "exile"
        ce.data["part"] = ReplacementPart("zone", applies, apply, text=self.text)
        ce.data["part"].redirect = "exile"
        g.s.effects.append(ce)


class SwitchPT(E):
    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        objs = [o for o in objs_of(self.sel, g, ctx) if g.s.objs[o].zone == "battlefield"]
        if not objs:
            return
        ce = _new_ce(g, ctx)
        ce.affected = objs

        def sw(g, oid, c, ce):
            if c.power is not None:
                c.power, c.toughness = c.toughness, c.power
        ce.parts["7d"] = sw
        g.s.effects.append(ce)
        g.s.touch()


class LoseAbilities(E):
    def __init__(self, sel, duration="eot", text="", while_source=False):
        self.sel = sel
        self.duration = duration
        self.text = text
        self.while_source = while_source

    def run(self, g, ctx):
        objs = [o for o in objs_of(self.sel, g, ctx) if g.s.objs[o].zone == "battlefield"]
        if not objs:
            return
        ce = _new_ce(g, ctx, self.duration)
        ce.affected = objs

        def lose(g, oid, c, ce):
            c.abilities = []
        ce.parts[6] = lose
        if self.while_source:
            ce.duration = "while_source"
            ce.data["host"] = ctx.get("source")
        g.s.effects.append(ce)
        g.s.touch()


class Earthbend(E):
    """CR 701.66a: target land you control becomes a 0/0 land creature with haste; put N
    +1/+1 counters on it; when it dies or is put into exile, return it tapped."""

    def __init__(self, sel, n, text=""):
        self.sel = sel
        self.n = amount(n)
        self.text = text

    def run(self, g, ctx):
        from .ir import Keyword, Triggered
        objs = [o for o in objs_of(self.sel, g, ctx) if g.s.objs[o].zone == "battlefield"]
        if not objs:
            return
        oid = objs[0]
        ce = _new_ce(g, ctx, "forever")
        ce.affected = [oid]

        def typ(g, o, c, ce):
            c.types.add("Creature")

        def pt(g, o, c, ce):
            c.power, c.toughness = 0, 0
        hk = Keyword("haste")

        def kw(g, o, c, ce, hk=hk):
            c.abilities.append(hk)
        ce.parts[4] = typ
        ce.parts["7b"] = pt
        ce.parts[6] = kw
        g.s.effects.append(ce)
        g.s.touch()
        g.add_counters(oid, "+1/+1", self.n(g, ctx), source=ctx.get("source"))
        g.s.delayed.append({"trigger": _LeavesTo(oid), "ability": _EARTHBEND_RETURN,
                            "controller": ctx["controller"], "source": ctx.get("source"),
                            "source_chars": ctx.get("source_chars"), "once": True,
                            "data": {"land": oid}})
        g.emit({"type": "earthbend", "player": ctx["controller"], "obj": oid})


class _LeavesTo:
    events = ("zone",)
    lookback = True

    def __init__(self, oid):
        self.oid = oid

    def __deepcopy__(self, memo):
        return self

    def match(self, g, ev, src, c, ctrl):
        if ev.get("old") == self.oid and ev.get("from") == "battlefield" and ev.get("to") in ("graveyard", "exile"):
            return {"obj": ev.get("new")}
        return None


class _ReturnTapped(E):
    def run(self, g, ctx):
        obj = (ctx.get("data") or {}).get("obj")
        o = g.s.objs.get(obj)
        if o is None or o.zone not in ("graveyard", "exile"):
            return
        g.move(obj, "battlefield", tapped=True, controller=ctx["controller"])


def _mk_return():
    from .ir import Triggered
    return Triggered(None, _ReturnTapped(), text="return it to the battlefield tapped under your control")


_EARTHBEND_RETURN = _mk_return()


class Manifest(E):
    """CR 701.40a / 701.62a (manifest dread): put a card face down as a 2/2 creature."""

    def __init__(self, dread=True, text=""):
        self.dread = dread
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        pl = g.s.players[p]
        if self.dread:
            if len(pl.library) > 1:
                # Library alignment for the differential harness (a Dig, as the EUT asks it; the
                # other card goes to the graveyard).
                g.dig_hook(p, 2, rest_to="graveyard", pick_to="battlefield")
            top = pl.library[:2]
            if not top:
                return
            if len(top) == 1:
                pick = top[0]
            else:
                opts = [Opt(f"choose:{g.ref(o)}", o, sig=("mf", g.obj_sig(o))) for o in top]
                # A Dig decision (the EUT's kind for looking at cards and choosing among them).
                pick = g.ask(Decision(p, "Dig", opts, prompt="Manifest dread: choose the card to manifest."))
            rest = [o for o in top if o != pick]
            g.move(pick, "battlefield", face_down=True, face_down_kind="manifest", controller=p)
            g.move_batch([(o, "graveyard", {}) for o in rest])
        else:
            if not pl.library:
                return
            g.move(pl.library[0], "battlefield", face_down=True, face_down_kind="manifest", controller=p)


# ============================================================================ cost parts

class CostPart:
    """An additional cost part (CR 118.8, 601.2b/h)."""
    name = "cost"
    life = 0

    def __deepcopy__(self, memo):
        return self

    def payable(self, g, p, oid):
        raise NotImplementedError

    def pay(self, g, p, oid, ctx):
        raise NotImplementedError


class SacCost(CostPart):
    name = "sacrifice"

    def __init__(self, filt, n=1):
        self.filt = filt
        self.n = n

    def payable(self, g, p, oid):
        return len([x for x in g.s.battlefield if g.ctrl(x) == p and self.filt.match(g, x, oid, {"controller": p})]) >= self.n

    def pay(self, g, p, oid, ctx):
        chosen = []
        for _ in range(self.n):
            cands = [x for x in g.s.battlefield if g.ctrl(x) == p and x not in chosen
                     and self.filt.match(g, x, oid, {"controller": p})]
            x = g.ask(Decision(p, "PayCost", [Opt(f"choose:{g.ref(c)}", c, sig=("sc", g.obj_sig(c))) for c in cands],
                               subject=g.ref(oid), prompt="Choose a permanent to sacrifice."))
            chosen.append(x)
            ctx.setdefault("paid", {}).setdefault("sacrificed", []).append(g.chars(x))
        # One cost's permanents are sacrificed together (CR 701.21a, 603.10a).
        g.sacrifice_batch(chosen)


class DiscardCost(CostPart):
    name = "discard"

    def __init__(self, filt=None, n=1):
        self.filt = filt
        self.n = n

    def _cands(self, g, p, oid):
        return [x for x in g.s.players[p].hand if x != oid and (self.filt is None or self.filt.match(g, x, oid, {"controller": p}))]

    def payable(self, g, p, oid):
        return len(self._cands(g, p, oid)) >= self.n

    def pay(self, g, p, oid, ctx):
        for _ in range(self.n):
            cands = self._cands(g, p, oid)
            x = g.ask(Decision(p, "PayCost", [Opt(f"choose:{g.ref(c)}", c, sig=("dc", g.obj_sig(c))) for c in cands],
                               subject=g.ref(oid), prompt="Choose a card to discard."))
            g.discard(p, x, cause="cost")


class LifeCost(CostPart):
    name = "life"

    def __init__(self, n):
        self.n = n
        self.life = n

    def payable(self, g, p, oid):
        return g.s.players[p].life >= self.n  # 119.4

    def pay(self, g, p, oid, ctx):
        g.pay_life(p, self.n)


class ManaCostPart(CostPart):
    name = "mana"

    def __init__(self, cost):
        self.cost = cost

    def payable(self, g, p, oid):
        # One total cost (601.2f): the spell's cost plus this, cost reductions applied to both.
        fn = getattr(g, "_cost_with", None)
        total = fn(self.cost) if fn is not None else list(getattr(g, "_base_cost", [])) + self.cost
        return g.mana_payable(p, total, 0, {"kind": "spell", "obj": oid, "chars": g.chars(oid)})

    def pay(self, g, p, oid, ctx):
        g.pay_mana(p, self.cost, 0, {"kind": "spell", "obj": oid, "chars": g.chars(oid)}, oid)


class BlightCost(CostPart):
    """CR 701.68a: put N -1/-1 counters on a creature you control."""
    name = "blight"

    def __init__(self, n):
        self.n = n

    def payable(self, g, p, oid):
        return any("Creature" in g.chars(x).types for x in g.s.battlefield if g.ctrl(x) == p)

    def pay(self, g, p, oid, ctx):
        cands = [x for x in g.s.battlefield if g.ctrl(x) == p and "Creature" in g.chars(x).types]
        x = g.ask(Decision(p, "PayCost", [Opt(f"choose:{g.ref(c)}", c, sig=("bl", g.obj_sig(c))) for c in cands],
                           subject=g.ref(oid), prompt=f"Blight {self.n}: choose a creature you control."))
        g.add_counters(x, "-1/-1", self.n)


class ChoiceCost(CostPart):
    """"A or B" additional cost: the caster chooses one payable alternative (cost:1/cost:2)."""
    name = "choice"

    def __init__(self, alts, optional=False, label=None):
        self.alts = alts
        self.optional = optional
        self.label = label

    def payable(self, g, p, oid):
        return self.optional or any(a.payable(g, p, oid) for a in self.alts)

    def choose(self, g, p, oid, ctx, allowed=None):
        """CR 601.2b: announce which alternative (or whether an optional cost) is paid.
        `allowed`: the alternatives with which the whole cost stays payable (casting)."""
        opts = []
        for i, a in enumerate(self.alts):
            if allowed is not None and not any(a is b for b in allowed):
                continue
            if a.payable(g, p, oid):
                opts.append(Opt(self.label(i, a) if self.label else f"cost:{i + 1}", a))
        if self.optional:
            opts.append(Opt("no", None))
        if not opts:
            from .decisions import IllegalAction
            raise IllegalAction("additional cost can't be paid")
        return g.ask(Decision(p, "AdditionalCost", opts, subject=g.ref(oid), prompt="Choose how to pay the additional cost."))

    def pay(self, g, p, oid, ctx):
        a = self.choose(g, p, oid, ctx)
        if a is not None:
            ctx.setdefault("paid_additional", []).append(a.name)
            a.pay(g, p, oid, ctx)


# ============================================================================ delayed / misc

class CaptureThen(E):
    """Binds objects now (at resolution) and builds the effect to run later."""

    def __init__(self, sel, factory, text=""):
        self.sel = sel
        self.factory = factory
        self.text = text

    def bind(self, g, ctx):
        ids = [x for x in self.sel.get(g, ctx) if not isinstance(x, tuple)]
        return self.factory(tuple(ids))

    def run(self, g, ctx):
        self.bind(g, ctx).run(g, ctx)


class DelayedAtNextEndStep(E):
    """CR 603.7: a delayed triggered ability "at the beginning of the next end step"."""

    def __init__(self, inner, text=""):
        self.inner = inner
        self.text = text

    def run(self, g, ctx):
        from .ir import Triggered
        from ..cards.triggers import Step
        eff = self.inner.bind(g, ctx) if hasattr(self.inner, "bind") else self.inner
        ab = Triggered(Step("End", "each"), eff, text=self.text)
        g.s.delayed.append({"trigger": ab.trigger, "ability": ab, "controller": ctx["controller"],
                            "source": ctx.get("source"), "source_chars": ctx.get("source_chars"),
                            "once": True, "data": {}})


class SacrificeObjs(E):
    def __init__(self, ids):
        self.ids = ids

    def run(self, g, ctx):
        # "Sacrifice them": one event (zones.sacrifice_batch, CR 701.21a).
        g.sacrifice_batch([oid for oid in self.ids if oid in g.s.objs and g.s.objs[oid].zone == "battlefield"
                           and g.ctrl(oid) == ctx["controller"]])


class ExileObjs(E):
    def __init__(self, ids):
        self.ids = ids

    def run(self, g, ctx):
        g.move_batch([(oid, "exile", {}) for oid in self.ids if oid in g.s.objs and g.s.objs[oid].zone == "battlefield"])


class ReturnObjs(E):
    def __init__(self, ids, zone):
        self.ids = ids
        self.zone = zone

    def run(self, g, ctx):
        moves = []
        for oid in self.ids:
            o = g.s.objs.get(oid)
            if o is not None and o.zone == "exile":
                moves.append((oid, self.zone, {"controller": o.owner}))
        g.move_batch(moves)


class LookAtHand(E):
    """Information only (no change to the game state): the controller looks at the selected
    player's hand (CR 701.20e; knowledge.py)."""

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        for q in players_of(self.sel, g, ctx):
            g.s.know.looks(g, ctx["controller"], list(g.s.players[q].hand))
        return None


class LoseHalfLife(E):
    def __init__(self, who, text=""):
        self.who = who
        self.text = text

    def run(self, g, ctx):
        for p in players_of(self.who, g, ctx):
            life = g.s.players[p].life
            g.lose_life(p, (life + 1) // 2 if life > 0 else 0, source=ctx.get("source"))


class Reflexive(E):
    """CR 603.12: "When you do, ..." - a reflexive triggered ability (optionally with an
    intervening "if", checked as it triggers and on resolution, 603.4)."""

    def __init__(self, eff, specs, text="", condition=None):
        self.eff = eff
        self.specs = list(specs)
        self.text = text
        self.condition = condition

    def run(self, g, ctx):
        from .ir import Triggered
        from .triggers import PendingTrigger
        if ctx.get("vars", {}).get("did") is False:
            return
        if self.condition is not None and not self.condition(g, ctx):
            return
        ab = Triggered(None, self.eff, self.specs, text=self.text, condition=self.condition)
        sc = ctx.get("source_chars") or ctx.get("chars")
        g.s.pending.append(PendingTrigger(ab, ctx.get("source"), sc, ctx["controller"], ctx.get("event"),
                                          dict(ctx.get("data") or {}), g.after_own_triggers(ctx.get("source"), sc)))


class ControllerMaySearchBasic(E):
    """Erode/Get Lost style: "Its controller may search their library for a basic land card, put
    it onto the battlefield tapped, then shuffle." The controller is the target's (LKI)."""

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        refs = ctx.get("legal") and ctx["legal"][0] or []
        for oid in refs:
            c = g.s.lki.get(oid) if oid not in g.s.objs else g.chars(oid)
            if c is None:
                continue
            p = c.controller
            a = g.ask(Decision(p, "OptionalEffect", [Opt("yes", True), Opt("no", False)],
                               prompt="Search your library for a basic land card?"))
            if a:
                sub = dict(ctx)
                sub["controller"] = p
                Search(Filter(supertypes=["Basic"], types=["Land"], text="basic land"), 1, "battlefield",
                       tapped=True).run(g, sub)


class Explore(E):
    """CR 701.44a: reveal the top card; a land goes to hand; otherwise a +1/+1 counter on the
    exploring permanent and the card may go to the graveyard."""

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        objs = objs_of(self.sel, g, ctx)
        if not objs:
            # The permanent told to explore ("~"/"it") has left the battlefield: it still explores,
            # its last known information telling which object explored and who controlled it
            # (CR 701.44c); only the +1/+1 counter is impossible (701.44b).
            x = None
            if self.sel.text == "self-permanent":
                x = ctx.get("source")
            elif self.sel.text.startswith("event."):
                ev = ctx.get("event") or {}
                x = ev.get(self.sel.text[len("event."):])
                if x is None:
                    x = ev.get("obj")
            if x is None or (x not in g.s.objs and x not in g.s.lki_obj):
                return
            objs = [x]
        oid = objs[0]
        o = g.s.objs.get(oid)
        if o is None:
            lo = g.s.lki_obj.get(oid)
            p = lo.controller if lo else ctx["controller"]
        else:
            p = g.ctrl(oid)
        pl = g.s.players[p]
        if not pl.library:
            # Nothing is revealed, which isn't a land card: "Otherwise" applies (701.44a; Cenote
            # Scout ruling 2023-11-10: the exploring creature receives a +1/+1 counter).
            if o is not None and o.zone == "battlefield":
                g.add_counters(oid, "+1/+1", 1, source=oid)
            g.emit({"type": "explored", "obj": oid, "player": p})
            return
        g.explore_hook(p)
        top = pl.library[0]
        if "Land" in g.chars(top).types:
            g.move(top, "hand", cause="explore")
        else:
            if o is not None and o.zone == "battlefield":
                g.add_counters(oid, "+1/+1", 1, source=oid)
            # Canonical kind: like surveil, "top" keeps it, "graveyard" puts it there.
            a = g.ask(Decision(p, "Surveil", [Opt("top", False), Opt("graveyard", True)], subject=g.ref(top),
                               prompt=f"Explore: put {g.name_of(top)} into your graveyard?"))
            if a:
                g.move(top, "graveyard", cause="explore")
        g.emit({"type": "explored", "obj": oid, "player": p})


# ============================================================================ optional actions

class MayThen(E):
    """"You may [action]. If you do, [effect]." (CR 608.2c, 608.2d). If the action is
    impossible (e.g. blight with no creature, CR 701.68b; a mana payment the player can't make,
    CR 118.3), the player can't choose to do it. `then` happens only if the action was done."""

    def __init__(self, first, then=None, text=""):
        self.first = first
        self.then = then
        self.text = text

    def _possible(self, g, ctx):
        f = self.first
        possible = getattr(f, "possible", None)
        if possible is not None:
            return possible(g, ctx)
        if isinstance(f, Discard) and f.n is not None:
            # "You may discard a card. If you do, ...": impossible with no card to discard.
            ps = players_of(f.who, g, ctx)
            return all(len([o for o in g.s.players[p].hand
                            if f.filt is None or f.filt.match(g, o, ctx.get("source"), ctx)]) >= f.n(g, ctx)
                       for p in ps)
        return True

    def run(self, g, ctx):
        p = ctx["controller"]
        if not self._possible(g, ctx):
            ctx.setdefault("vars", {})["did"] = False
            return
        a = g.ask(Decision(p, "OptionalEffect", [Opt("yes", True), Opt("no", False)],
                           subject=g.ref(ctx["source"]) if ctx.get("source") in g.s.objs else None,
                           prompt=f"Use the optional effect? {self.text}"))
        if not a:
            ctx.setdefault("vars", {})["did"] = False
            return
        done = self.first.run(g, ctx)
        ctx.setdefault("vars", {})["did"] = done is not False
        if done is not False and self.then is not None:
            self.then.run(g, ctx)


class Blight(E):
    """CR 701.68a: put N -1/-1 counters on a creature you control (the controller chooses).
    Returns False if it couldn't be done (701.68b)."""

    def __init__(self, n, text=""):
        self.n = amount(n)
        self.text = text

    def _cands(self, g, p):
        return [x for x in g.s.battlefield if g.ctrl(x) == p and "Creature" in g.chars(x).types]

    def possible(self, g, ctx):
        return bool(self._cands(g, ctx["controller"]))

    def run(self, g, ctx):
        p = ctx["controller"]
        cands = self._cands(g, p)
        if not cands:
            return False
        opts = [Opt(f"choose:{g.ref(c)}", c, sig=("bl", g.obj_sig(c))) for c in cands]
        x = g.ask(Decision(p, "ChooseCard", opts, prompt=f"Blight {self.text}: choose a creature you control."))
        g.add_counters(x, "-1/-1", self.n(g, ctx), source=ctx.get("source"))
        ctx.setdefault("vars", {})["blighted"] = [x]
        return True


class PayMana(E):
    """"Pay {cost}" as an action during resolution (CR 118.3: a player can't pay a cost they
    can't pay; "you may pay ... If you do" checks payability first)."""

    def __init__(self, cost, text=""):
        self.cost = list(cost)
        self.text = text

    def possible(self, g, ctx):
        return g.mana_payable(ctx["controller"], self.cost, 0, {"kind": "unless", "obj": None, "chars": None})

    def run(self, g, ctx):
        p = ctx["controller"]
        if not self.possible(g, ctx):
            return False
        g.pay_mana(p, self.cost, 0, {"kind": "unless", "obj": None, "chars": None}, None)
        return True


# ============================================================================ event selectors

def event_lki():
    """The object of the triggering event as it last existed (CR 603.10a, 608.2h): for a
    leaves-the-battlefield trigger, the old object id (its last known information is kept in
    `lki`/`lki_obj`); otherwise the event's object."""
    def fn(g, ctx):
        ev = ctx.get("event") or {}
        old = ev.get("old")
        if old is not None and ev.get("from") == "battlefield":
            return [old]
        x = ev.get("new", ev.get("obj"))
        return [x] if x is not None and x in g.s.objs else []
    return Sel(fn, "event.lki")


def event_obj_chars(g, ctx):
    """(Chars, Obj) of the triggering event's object, last known information if it left."""
    ev = ctx.get("event") or {}
    old = ev.get("old")
    if old is not None and ev.get("from") == "battlefield":
        return ev.get("lki") or g.s.lki.get(old), ev.get("lki_obj") or g.s.lki_obj.get(old)
    x = ev.get("new", ev.get("obj"))
    if x is not None and x in g.s.objs:
        return g.chars(x), g.s.objs[x]
    return None, None


class Flicker(E):
    """"Exile X, then return that card to the battlefield under [your|its owner's] control"
    (CR 400.7: the returned card is a new object; 111.8: a token that left the battlefield
    can't come back)."""

    def __init__(self, sel, controller="owner", counters=None, tapped=False, text=""):
        self.sel = sel
        self.controller = controller
        self.counters = dict(counters or {})
        self.tapped = tapped
        self.text = text

    def run(self, g, ctx):
        objs = [o for o in objs_of(self.sel, g, ctx) if g.s.objs[o].zone == "battlefield"]
        if not objs:
            return
        tokens = {o for o in objs if g.s.objs[o].token}
        res = g.move_batch([(o, "exile", {}) for o in objs])
        back = []
        for old, new in zip(objs, res):
            if new is None or old in tokens or new not in g.s.objs or g.s.objs[new].zone != "exile":
                continue
            o = g.s.objs[new]
            ctrl = ctx["controller"] if self.controller == "you" else o.owner
            back.append((new, "battlefield", {"controller": ctrl, "tapped": self.tapped,
                                              "counters": dict(self.counters)}))
        if back:
            res2 = g.move_batch(back)
            ctx.setdefault("vars", {})["returned"] = [r for r in res2 if r is not None]


def self_chars(g, ctx):
    """The source's current characteristics, or last known information if it left the
    battlefield (CR 608.2h): as it last existed there, not as it was when the ability triggered
    (an Ouroboroid made 0/0 by Multiply by Zero in response to its trigger: X is 0)."""
    src = ctx.get("source")
    o = g.s.objs.get(src)
    if o is not None and o.zone == "battlefield":
        return g.chars(src)
    return g.s.lki.get(src) or ctx.get("source_chars")


class PutOntoBattlefield(E):
    """Return/put objects from a non-battlefield zone onto the battlefield (CR 400.7: the
    selector must still name the same object in that zone). Enters-with counters and tapped
    status are part of the event (CR 614.1c, 122.6)."""

    def __init__(self, sel, from_zones=("graveyard",), controller="you", tapped=False, counters=None,
                 text="", enters_as=None, transformed=False):
        self.sel = sel
        self.transformed = transformed
        self.enters_as = enters_as  # {layer: fn(g, oid, chars, ce)}
        self.from_zones = tuple(from_zones)
        self.controller = controller
        self.tapped = tapped
        self.counters = dict(counters or {})
        self.text = text

    def run(self, g, ctx):
        moves = []
        for oid in objs_of(self.sel, g, ctx):
            o = g.s.objs.get(oid)
            if o is None or o.zone not in self.from_zones:
                continue
            ctrl = o.owner if self.controller == "owner" else ctx["controller"]
            opts = {"controller": ctrl, "tapped": self.tapped, "counters": dict(self.counters)}
            if self.transformed:
                # CR 712.14a: a card that isn't a transforming double-faced card stays where it is.
                if DB.get(o.card).layout != "transform" or o.flags.get("copiable") is not None:
                    continue
                opts["face"] = 1
            moves.append((oid, "battlefield", opts))
        ce = None
        if moves and self.enters_as:
            # The permanent enters with these characteristics (e.g. "It's an enchantment"):
            # the effect applies from the moment it enters, so it's already in force when
            # enters-the-battlefield abilities check it (rulings on the Enduring cycle).
            ce = _new_ce(g, ctx, "forever")
            before = set(g.s.objs)
            cards = {(g.s.objs[m[0]].card, g.s.objs[m[0]].owner) for m in moves}
            ce.filter = lambda gg, oid, c, before=before, cards=cards: (
                oid not in before and (gg.s.objs[oid].card, gg.s.objs[oid].owner) in cards)
            ce.parts = dict(self.enters_as)
            # While the move happens, replacement effects that modify how permanents enter
            # check the entering card with this effect applied (CR 614.12: continuous effects
            # that already exist and would apply to it; statics.entering_chars).
            ce.data["enters_cards"] = cards
            g.s.effects.append(ce)
            g.s.touch()
        res = g.move_batch(moves) if moves else []
        new = [r for r in res if r is not None]
        if ce is not None:
            ce.data.pop("enters_cards", None)
            ce.filter = None
            ce.affected = list(new)
            if not new:
                g.s.effects.remove(ce)
            g.s.touch()
        ctx.setdefault("vars", {})["returned"] = new


class ShuffleIntoLibrary(E):
    """"Shuffle X into its owner's library" (CR 701.24): put it into the library, then that
    library is shuffled."""

    def __init__(self, sels, text=""):
        self.sels = list(sels)
        self.text = text

    def run(self, g, ctx):
        objs = []
        for sel in self.sels:
            for o in objs_of(sel, g, ctx):
                if o in g.s.objs and g.s.objs[o].zone == "battlefield" and o not in objs:
                    objs.append(o)
        if not objs:
            return
        owners = sorted({g.s.objs[o].owner for o in objs})
        g.move_batch([(o, "library", {}) for o in objs])
        for p in owners:
            g.shuffle(p)


def event_amount():
    """'that much': the amount of the triggering event (life gained, damage dealt, ...)."""
    def fn(g, ctx):
        d = ctx.get("data") or {}
        if d.get("n") is not None:
            return d["n"]
        return (ctx.get("event") or {}).get("n", 0)
    return Amount(fn, "that much")


def set_card_types(c, new_types):
    """CR 205.1a: an effect sets an object's card types; subtypes correlated with a removed
    card type go away unless they also belong to a card type it still has."""
    from ..cards.compiler import subtypes_of
    keep = set(t for t in c.types if t in ("Instant", "Sorcery"))
    new = set(new_types) | keep
    removed = c.types - new
    if removed:
        drop = set()
        for t in removed:
            drop |= subtypes_of(t)
        for t in new:
            drop -= subtypes_of(t)
        c.subtypes -= drop
    c.types = new


class CounterUnlessPaysX(CounterUnlessPays):
    """"Counter target spell unless its controller pays {X}": X is the X of the resolving
    spell or ability (CR 107.3a)."""

    def run(self, g, ctx):
        x = ctx.get("x", 0) or 0
        cost = [s for s in self.cost if s != "X"]
        k = sum(1 for s in self.cost if s == "X") * x
        if k:
            cost.append(k)
        sub = CounterUnlessPays(cost, self.life, sel=self.sel, text=self.text, exile=self.exile)
        sub.run(g, ctx)


class RemoveCountersUpTo(E):
    """"Remove up to N counters from X" (CR 122.8): the controller of the effect chooses which
    counters, one at a time (decision 'RemoveCounters', options 'counter:<kind>' and 'done')."""

    def __init__(self, sel, n, kind=None, text=""):
        self.sel = sel
        self.n = amount(n)
        self.kind = kind
        self.text = text

    def run(self, g, ctx):
        n = self.n(g, ctx)
        p = ctx["controller"]
        for oid in objs_of(self.sel, g, ctx):
            o = g.s.objs.get(oid)
            if o is None or o.zone != "battlefield":
                continue
            removed = 0
            while removed < n:
                kinds = sorted(k for k, v in o.counters.items() if v > 0 and (self.kind is None or k == self.kind))
                if not kinds:
                    break
                opts = [Opt("done", None)] + [Opt(f"counter:{k}", k) for k in kinds]
                k = g.ask(Decision(p, "RemoveCounters", opts, subject=g.ref(oid),
                                   prompt=f"Remove a counter ({removed}/{n} removed)."))
                if k is None:
                    break
                removed += g.remove_counters(oid, k, 1)


class PutCountersGuarded(PutCounters):
    """PutCounters for a counter kind whose rules meaning the engine doesn't implement yet
    (e.g. hone counters, CR 122.1j): fails loudly if a counter would actually be put."""

    def __init__(self, sel, kind, n, why, text=""):
        super().__init__(sel, kind, n, text=text)
        self.why = why

    def run(self, g, ctx):
        n = self.n(g, ctx)
        if n > 0 and any(not isinstance(r, tuple) and g.s.objs[r].zone == "battlefield" for r in self.sel.get(g, ctx)):
            raise Unsupported(self.why)


CARD_TYPES = ("Artifact", "Battle", "Creature", "Enchantment", "Instant", "Kindred", "Land",
              "Planeswalker", "Sorcery")


def card_types_among(g, ids):
    """CR 205.2a card types among a set of cards (their current characteristics). Cards
    outside the battlefield and stack are read without the layer system, so this is safe to
    call while characteristics are being computed (static conditions, CDAs)."""
    out = set()
    for oid in ids:
        o = g.s.objs.get(oid)
        if o is None:
            continue
        # Card types aren't changed by characteristic-defining abilities in practice, and
        # base characteristics avoid recursion (a Tarmogoyf in a graveyard counting itself).
        c = g.chars(oid) if o.zone in ("battlefield", "stack") else g.base_chars(o)
        out |= set(c.types) & set(CARD_TYPES)
    return out


def exiled_with_ids(g, src):
    """Ids of cards in exile linked to `src` (CR 607.2a)."""
    s = g.s
    out = []
    for (host, _tag), ids in s.linked.items():
        if host == src:
            out += ids
    for rec in s.until_leaves:
        if rec["host"] == src:
            out += rec["objs"]
    seen = []
    for x in out:
        if x in s.objs and s.objs[x].zone == "exile" and x not in seen:
            seen.append(x)
    return seen


class BargainCost(SacCost):
    """CR 702.166a: "As an additional cost to cast this spell, you may sacrifice an artifact,
    enchantment, or token." """
    name = "bargain"

    def __init__(self):
        f = Filter(text="artifact, enchantment, or token you control", controller="you",
                   custom=lambda g, c, o, src, ctx: bool({"Artifact", "Enchantment"} & c.types) or bool(o is not None and o.token))
        super().__init__(f, 1)


class HarmonizeTap(CostPart):
    """CR 702.180a-b: as the harmonize cost is chosen (601.2b) the caster chooses up to one
    untapped creature they control; the total cost is reduced by its power (601.2f) and the
    creature is tapped as the total cost is paid (601.2h). Summoning sickness doesn't matter
    (CR 302.6 restricts only the creature's own {T} abilities)."""
    name = "harmonize"

    def __init__(self, oid=None, power=0):
        self.oid = oid
        self.power = power

    def payable(self, g, p, oid):
        o = g.s.objs.get(self.oid)
        return o is not None and o.zone == "battlefield" and not o.tapped and g.ctrl(self.oid) == p

    def pay(self, g, p, oid, ctx):
        if not self.payable(g, p, oid):
            from .decisions import IllegalAction
            raise IllegalAction("the creature chosen for harmonize can't be tapped")
        g.tap(self.oid)


class HarmonizeChoice(ChoiceCost):
    """The harmonize creature choice (PayCost: choose:<creature> or done). Only choices with
    which the spell's total cost can still be paid are offered."""
    name = "harmonize"

    def __init__(self, cost):
        self.cost = list(cost)
        self.alts = []
        self.optional = True
        self.label = None

    @staticmethod
    def _cands(g, p):
        return [x for x in g.s.battlefield if g.ctrl(x) == p and not g.s.objs[x].tapped
                and "Creature" in g.chars(x).types]

    def _options(self, g, p, oid, cost, spend):
        out = []
        if g.mana_payable(p, cost, 0, spend):
            out.append(None)
        for x in self._cands(g, p):
            pw = max(0, g.chars(x).power or 0)
            red = g.reduce_total(cost, pw)
            if g.mana_payable(p, red, 0, spend, exclude={x}):
                out.append(x)
        return out

    def castable(self, g, p, oid, cost, spend):
        # CR 601.2a: the card is already on the stack when the total cost is determined
        # (601.2f) and the creature to tap is chosen (702.180b): a creature whose power counts
        # the cards in a graveyard no longer counts this one (Dark Matter Manipulator's "+2/+0 for
        # every seven cards in your graveyard": seven cards with Winternight
        # Stories among them). Checked with the card out of its graveyard for a moment.
        o = g.s.objs.get(oid)
        gy = g.s.players[o.owner].graveyard if o is not None and o.zone == "graveyard" else None
        if gy is None or oid not in gy:
            return bool(self._options(g, p, oid, cost, spend))
        i = gy.index(oid)
        gy.pop(i)
        o.zone = "stack"
        g.s.touch()
        try:
            return bool(self._options(g, p, oid, cost, spend))
        finally:
            gy.insert(i, oid)
            o.zone = "graveyard"
            g.s.touch()

    def payable(self, g, p, oid):
        return True

    def choose(self, g, p, oid, ctx):
        spend = {"kind": "spell", "chars": g.chars(oid), "obj": oid}
        cands = self._options(g, p, oid, g._base_cost, spend)
        if not cands:
            from .decisions import IllegalAction
            raise IllegalAction("harmonize cost can't be paid")
        opts = [Opt("done", None) if x is None else Opt(f"choose:{g.ref(x)}", x, sig=("harm", g.obj_sig(x)))
                for x in cands]
        a = g.ask(Decision(p, "PayCost", opts, subject=g.ref(oid),
                           prompt="Choose up to one untapped creature to tap for harmonize (CR 702.180b)."))
        if a is None:
            return None
        return HarmonizeTap(a, max(0, g.chars(a).power or 0))

    def pay(self, g, p, oid, ctx):
        return None


class DamageRecord(Damage):
    """Damage that remembers which permanents were actually dealt damage (vars 'damaged'),
    for "a permanent dealt damage by ~ this turn"."""

    def run(self, g, ctx):
        n = self.n(g, ctx)
        src = ctx.get("source")
        sc = None
        if src not in g.s.objs:
            sc = g.s.lki.get(src) or ctx.get("source_chars")
        hit = []
        prev = g.damage_batch_start()  # all recipients at once (one lifelink gain, CR 119.9)
        for r in self.to.get(g, ctx):
            dealt = g.deal_damage(src, r, n, source_chars=sc)
            if dealt and not isinstance(r, tuple):
                hit.append(r)
        g.damage_batch_end(prev)
        ctx.setdefault("vars", {}).setdefault("damaged", []).extend(hit)


def while_host_on_battlefield(ce, ctx):
    """CR 611.2b: an effect that lasts "for as long as ~ remains on the battlefield" ends for
    good when that object leaves (a returning card is a new object, 400.7), and does nothing if
    it already left. Implemented as a conditional layer effect."""
    host = ctx.get("source")
    ce.kind = "layer_static"
    ce.duration = "forever"
    ce.data["host"] = host
    ce.data["condition"] = lambda g, ce: (ce.data["host"] in g.s.objs
                                          and g.s.objs[ce.data["host"]].zone == "battlefield")


class LoseAllAbilities(E):
    """"X loses all abilities [until end of turn | for as long as ~ remains on the battlefield]"
    (layer 6, CR 613.1f); the affected set is locked as the effect begins (611.2c)."""

    def __init__(self, sel, duration="eot", text="", lose_types=None):
        self.sel = sel
        self.duration = duration  # 'eot' | 'forever' | 'while_host'
        self.text = text

    def run(self, g, ctx):
        objs = [o for o in objs_of(self.sel, g, ctx) if o in g.s.objs and g.s.objs[o].zone == "battlefield"]
        if not objs:
            return
        ce = _new_ce(g, ctx, "eot" if self.duration == "eot" else "forever")
        ce.affected = objs

        def lose(g, oid, c, ce):
            c.abilities = []
        ce.parts[6] = lose
        if self.duration == "while_host":
            while_host_on_battlefield(ce, ctx)
            if not ce.data["condition"](g, ce):
                return  # 611.2b: the host already left
        g.s.effects.append(ce)
        g.s.touch()


class Remember(E):
    """Evaluate a selector now and keep the result (for "those creatures")."""

    def __init__(self, sel, name, text=""):
        self.sel = sel
        self.name = name
        self.text = text

    def run(self, g, ctx):
        ctx.setdefault("vars", {})[self.name] = [x for x in self.sel.get(g, ctx)]


class PutCountersMulti(E):
    """"Put a flying counter, a first strike counter, and a lifelink counter on X"."""

    def __init__(self, sel, kinds, text=""):
        self.sel = sel
        self.kinds = list(kinds)  # [(kind, n)]
        self.text = text

    def run(self, g, ctx):
        for r in self.sel.get(g, ctx):
            if isinstance(r, tuple) or r not in g.s.objs or g.s.objs[r].zone != "battlefield":
                continue
            for kind, n in self.kinds:
                g.add_counters(r, kind, n, source=ctx.get("source"))


def exiled_with_source():
    """Cards in exile "exiled with ~" (CR 607.2a): linked to this object (s.linked entries and
    "until ~ leaves" records whose host is the source), still the same objects in exile."""
    def fn(g, ctx):
        return exiled_with_ids(g, ctx.get("source"))
    return Sel(fn, "exiled with ~", single=False)


class MoveCountTo(E):
    """Move objects to a zone and remember the ones that actually got there ("put into a
    graveyard this way")."""

    def __init__(self, sel, zone, remember, text=""):
        self.sel = sel
        self.zone = zone
        self.remember = remember
        self.text = text

    def run(self, g, ctx):
        objs = objs_of(self.sel, g, ctx)
        res = g.move_batch([(o, self.zone, {}) for o in objs]) if objs else []
        got = [r for r in res if r is not None and r in g.s.objs and g.s.objs[r].zone == self.zone]
        ctx.setdefault("vars", {})[self.remember] = got


class SacrificeSelf(E):
    """"Sacrifice ~" (CR 701.21a): only if it's still on the battlefield under your control."""

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        src = ctx.get("source")
        o = g.s.objs.get(src)
        if o is not None and o.zone == "battlefield" and g.ctrl(src) == ctx["controller"]:
            g.sacrifice(src)


class ExileSelf(E):
    """"Exile ~": the source, if it is still the same object on the battlefield (CR 400.7).
    Records whether it happened for a following "If you do" (CR 608.2c)."""
    reports_done = True

    def __init__(self, text=""):
        self.text = text

    def run(self, g, ctx):
        src = ctx.get("source")
        o = g.s.objs.get(src)
        done = False
        if o is not None and o.zone == "battlefield":
            new = g.move(src, "exile")
            done = new is not None and new in g.s.objs and g.s.objs[new].zone == "exile"
        ctx.setdefault("vars", {})["last_done"] = done


class DestroyWithSameName(E):
    """"Destroy target X and all other permanents with the same name as that permanent": one
    simultaneous destruction (CR 701.8). An object with no name (face down, 708.2a) has the
    same name as nothing (201.2a)."""

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        tg = [o for o in objs_of(self.sel, g, ctx) if o in g.s.objs and g.s.objs[o].zone == "battlefield"]
        if not tg:
            return
        # A split permanent has both halves' names (CR 709.4a; a Room with both doors unlocked,
        # 709.5): objects with at least one name in common have the same name (201.2a).
        def names(n):
            return set(n.split(" // ")) if n else set()
        name = names(g.chars(tg[0]).name)
        objs = list(tg)
        if name:
            objs += [o for o in g.s.battlefield if o not in objs and name & names(g.chars(o).name)]
        Destroy(Sel(lambda gg, cc, objs=tuple(objs): list(objs), "same name", single=False)).run(g, ctx)


class GainControl(E):
    """"<player> gains control of <permanent>" (CR 613.1b, layer 2): lasts indefinitely."""

    def __init__(self, sel, who, duration="forever", text=""):
        self.sel = sel
        self.who = who
        self.duration = duration
        self.text = text

    def run(self, g, ctx):
        ps = players_of(self.who, g, ctx)
        objs = [o for o in objs_of(self.sel, g, ctx) if o in g.s.objs and g.s.objs[o].zone == "battlefield"]
        if not ps or not objs:
            return
        ce = _new_ce(g, ctx, self.duration)
        ce.affected = objs
        newc = ps[0]

        def ctl(g, oid, c, ce, newc=newc):
            c.controller = newc
        ce.parts[2] = ctl
        g.s.effects.append(ce)
        g.s.touch()
        g.s.know.controls(g, newc, objs)  # 708.5: the new controller may look at a face-down one


class DamageEachPlayerCount(E):
    """"~ deals damage to each player equal to the number of <noun> that player controls"."""

    def __init__(self, filt, text=""):
        self.filt = filt
        self.text = text

    def run(self, g, ctx):
        src = ctx.get("source")
        sc = None if src in g.s.objs else (g.s.lki.get(src) or ctx.get("source_chars"))
        for p in (g.s.active, 1 - g.s.active):
            n = sum(1 for o in g.s.battlefield if g.ctrl(o) == p and self.filt.match(g, o, src, {"controller": p}))
            if n > 0:
                g.deal_damage(src, ("player", p), n, source_chars=sc)


class Transform(E):
    """CR 701.27a: turn a transforming double-faced permanent over (it's the same object)."""

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        for oid in objs_of(self.sel, g, ctx):
            o = g.s.objs.get(oid)
            if o is None or o.zone != "battlefield" or o.face_down:
                continue
            if DB.get(o.card).layout != "transform" or o.flags.get("copiable") is not None:
                continue  # 701.27c: a non-transforming permanent doesn't transform
            o.face = 1 - o.face
            g.s.touch()
            g.emit({"type": "transformed", "obj": oid})


class Emblem(E):
    """"You get an emblem with <triggered ability>" (CR 114.1-114.4). The emblem's abilities
    function from the command zone and nothing can remove it; it is kept as a permanent
    (never-ending) triggered ability of its owner. Emblems aren't part of the canonical state
    format (which covers battlefield, graveyards and exile)."""

    def __init__(self, abilities, text=""):
        self.abilities = list(abilities)
        self.text = text

    def run(self, g, ctx):
        for ab in self.abilities:
            if ab.kind == "static":
                self._static(g, ctx, ab)
                continue
            if ab.kind != "triggered":
                raise Unsupported("emblem with a non-triggered ability")
            # The emblem has no source object (CR 114.4); "emblem_of" only names the card that
            # created it (bookkeeping for the differential harness, which matches trigger orders).
            sc = ctx.get("source_chars") or (g.chars(ctx["source"]) if ctx.get("source") in g.s.objs else None)
            g.s.delayed.append({"trigger": ab.trigger, "ability": ab, "controller": ctx["controller"],
                                "source": None, "source_chars": None, "once": False,
                                "data": {"emblem_of": getattr(sc, "name", None)}, "emblem": True})

    def _static(self, g, ctx, ab):
        """A characteristic-changing static ability of the emblem ("Ninjas you control get
        +1/+1", Kaito): it functions from the command zone for the rest of the game (CR 114.4)
        and applies to whatever matches it now (611.3a), with the emblem's timestamp (613.7a).
        Kept as a never-ending continuous effect with a filter; anything else fails loudly."""
        if ab.condition is not None or any(p.kind != "layer" or not hasattr(p.affected, "match") for p in ab.parts):
            raise Unsupported("emblem static ability other than a characteristic-changing one")
        s = g.s
        ce = ContEffect(s.new_id())
        ce.source = None
        ce.controller = ctx["controller"]
        ce.timestamp = s.new_ts()
        ce.duration = "forever"
        ce.kind = "layer"
        parts = {}
        for p in ab.parts:
            if p.layer in parts:
                raise Unsupported("emblem static ability with two effects in one layer")
            parts[p.layer] = (lambda g2, oid, c, ce2, fn=p.fn: fn(g2, oid, c, None))
        ce.parts = parts
        filt = ab.parts[0].affected
        ctrl = ctx["controller"]
        ce.filter = lambda g2, oid, c, filt=filt, ctrl=ctrl: filt.match_chars(g2, c, g2.s.objs.get(oid), None, {"controller": ctrl})
        s.effects.append(ce)
        s.touch()


class ExileFromHandUntil(E):
    """"Look at target opponent's hand. You may exile a nonland card from it until ~ leaves the
    battlefield." (CR 610.3: it returns to its owner's hand). The choice to exile nothing is
    'done'. If ~ already left the battlefield, nothing is exiled (610.3a)."""

    def __init__(self, who, filt, optional=True, text=""):
        self.who = who
        self.filt = filt
        self.optional = optional
        self.text = text

    def run(self, g, ctx):
        s = g.s
        host = ctx.get("source")
        ho = s.objs.get(host)
        if ho is None or ho.zone != "battlefield":
            return
        p = ctx["controller"]
        for q in players_of(self.who, g, ctx):
            cands = [o for o in s.players[q].hand if self.filt is None or self.filt.match(g, o, host, ctx)]
            if not cands:
                continue
            opts = ([Opt("done", None)] if self.optional else []) + \
                [Opt(f"choose:{g.ref(o)}", o, sig=("exh", g.obj_sig(o))) for o in cands]
            a = g.ask(Decision(p, "ChooseCard", opts, prompt="Choose a card to exile (or done)."))
            if a is None:
                continue
            res = g.move_batch([(a, "exile", {"link": (host, "until"), "cause": "until"})])
            ids = [r for r in res if r is not None]
            if ids:
                s.until_leaves.append({"host": host, "objs": ids, "zone": "exile", "back": "hand"})
                s.touch()


class PutLKICountersOn(E):
    """"Put this creature's counters on <it>": the counters the source had as it last existed
    (CR 603.10a, 608.2h)."""

    def __init__(self, sel, text=""):
        self.sel = sel
        self.text = text

    def run(self, g, ctx):
        src = ctx.get("source")
        o = g.s.objs.get(src)
        if o is not None and o.zone == "battlefield":
            counters = dict(o.counters)
        else:
            lo = g.s.lki_obj.get(src)
            counters = dict(lo.counters) if lo is not None else {}
        for r in self.sel.get(g, ctx):
            if isinstance(r, tuple) or r not in g.s.objs or g.s.objs[r].zone != "battlefield":
                continue
            for k, n in sorted(counters.items()):
                if n > 0:
                    g.add_counters(r, k, n, source=src)


class UnsupportedIf(E):
    """Fails loudly at resolution when `pred` holds (a rule the engine doesn't implement)."""

    def __init__(self, pred, why, text=""):
        self.pred = pred
        self.why = why
        self.text = text

    def run(self, g, ctx):
        if self.pred(g, ctx):
            raise Unsupported(self.why)


def spell_mana_spent(g, ctx):
    """Total mana spent to cast the triggering spell (CR 601.2h)."""
    ev = ctx.get("event") or {}
    oid = ev.get("obj")
    o = g.s.objs.get(oid) or g.s.lki_obj.get(oid)
    return sum((o.mana_spent or {}).values()) if o is not None else 0


class Fn_(E):
    """A hand-written effect step (fn(g, ctx))."""

    def __init__(self, fn, text=""):
        self.fn = fn
        self.text = text

    def run(self, g, ctx):
        self.fn(g, ctx)


def trigger_player(key="player"):
    """'they' / 'that player' in a triggered ability: the player the trigger condition named
    (trigger data, e.g. the player dealt combat damage), else the event's player."""
    def fn(g, ctx):
        d = ctx.get("data") or {}
        p = d.get(key)
        if p is None:
            p = (ctx.get("event") or {}).get(key)
        return [("player", p)] if p is not None else []
    return Sel(fn, f"trigger.{key}")


class IfYouDo(E):
    """"If you do, ..." after an action that may not happen (it checks the action's result)."""

    def __init__(self, then, text=""):
        self.then = then
        self.text = text

    def run(self, g, ctx):
        if ctx.get("vars", {}).get("last_done"):
            self.then.run(g, ctx)


# ============================================================================ hands, libraries, graveyards (card push s)

class ChooseFromHand(E):
    """A player's hand is revealed (CR 701.20a) or looked at (701.20e) and the controller chooses
    a card from it: "Target opponent reveals their hand. You choose a noncreature, nonland card
    from it. That player discards that card." (Duress; 701.9b: another player chooses the
    discarded card), "... Exile that card." (Intimidation Tactics), "Look at target player's
    hand. You may choose a card from it. If you do, that player discards that card, then draws a
    card." (Oildeep Gearhulk). Nothing matching: nothing is chosen (the reveal still happens).
    kind: the decision kind (Discard for a discard, Search for "choose ... exile that card", the
    EUT's kinds for the same choices)."""

    reports_done = True

    def __init__(self, who, filt, action, kind="Discard", optional=False, then_draw=0, link=False, text=""):
        self.who = who
        self.filt = filt
        self.action = action
        self.kind = kind
        self.optional = optional
        self.then_draw = then_draw
        self.link = link  # the exile is linked to the source (CR 607.2a)
        self.text = text

    def run(self, g, ctx):
        me = ctx["controller"]
        if self.action == "exile":
            ctx.setdefault("vars", {})["exiled"] = []
            ctx["vars"]["last_done"] = False
        for p in players_of(self.who, g, ctx):
            pl = g.s.players[p]
            g.emit({"type": "reveal_hand", "player": p, "to": None if not self.optional else me})
            cands = [o for o in pl.hand if self.filt is None or self.filt.match(g, o, ctx.get("source"), ctx)]
            if not cands:
                ctx.setdefault("vars", {})["did"] = False
                continue
            if self.optional:
                a = g.ask(Decision(me, "OptionalEffect", [Opt("yes", True), Opt("no", False)],
                                   subject=g.ref(ctx["source"]) if ctx.get("source") in g.s.objs else None,
                                   prompt=f"Choose a card from that hand? {self.text}"))
                if not a:
                    ctx.setdefault("vars", {})["did"] = False
                    continue
            opts = [Opt(f"choose:{g.ref(o)}", o, sig=("hand", g.obj_sig(o))) for o in cands]
            o = g.ask(Decision(me, self.kind, opts, prompt=f"Choose a card ({self.text})"))
            if self.action == "discard":
                g.discard(p, o, cause="effect")
            else:
                opts_m = {"cause": "effect"}
                src = ctx.get("source")
                if self.link and src is not None:
                    opts_m["link"] = (src, "exiled")
                got = [r for r in g.move_batch([(o, "exile", opts_m)]) if r is not None]
                ctx["vars"]["exiled"] += got
                ctx["vars"]["last_done"] = bool(ctx["vars"]["exiled"])
            ctx.setdefault("vars", {})["did"] = True
            if self.then_draw:
                g.draw(p, self.then_draw)


class DiscardUnless(E):
    """"Discard two cards unless you discard a creature card." (CR 118.12a: "[do A] unless [a
    player does B]" means "that player may do B; if they don't, do A"; 608.2d: B can be chosen
    only if possible, i.e. with such a card in hand). Discards are chosen one at a time
    (701.9b)."""

    def __init__(self, n, filt, text=""):
        self.n = amount(n)
        self.filt = filt
        self.text = text

    def run(self, g, ctx):
        p = ctx["controller"]
        pl = g.s.players[p]
        match = [o for o in pl.hand if self.filt.match(g, o, ctx.get("source"), ctx)]
        alt = False
        # With one card in hand both answers discard that card (of the N, as many as possible:
        # CR 609.3): a choice without consequence, not asked (campaign13 835708, 820865: Steamcore
        # Scholar's "discard two cards unless you discard ... a creature card with flying" with
        # one flyer in hand; the EUT discards it without a decision).
        if match and len(pl.hand) > 1:
            alt = g.ask(Decision(p, "OptionalEffect", [Opt("yes", True), Opt("no", False)],
                                 subject=g.ref(ctx["source"]) if ctx.get("source") in g.s.objs else None,
                                 prompt=f"Discard a {self.filt.text} card instead? ({self.text})"))
        if alt:
            opts = [Opt(f"choose:{g.ref(o)}", o, sig=("disc", g.obj_sig(o))) for o in match]
            o = g.ask(Decision(p, "Discard", opts, prompt=f"Choose a {self.filt.text} card to discard."))
            g.discard(p, o, cause="effect")
            return
        for _ in range(self.n(g, ctx)):
            if not pl.hand:
                break
            opts = [Opt(f"choose:{g.ref(o)}", o, sig=("disc", g.obj_sig(o))) for o in pl.hand]
            o = g.ask(Decision(p, "Discard", opts, prompt="Choose a card to discard."))
            g.discard(p, o, cause="effect")


class ExileCreatureAndGraveyard(E):
    """"Target opponent exiles a creature they control and their graveyard." (Strategic
    Betrayal): the player chooses the creature (CR 701.13a; the choice is theirs, 608.2d) and the
    creature and every card in their graveyard are exiled at once."""

    def __init__(self, who, filt, text=""):
        self.who = who
        self.filt = filt
        self.text = text

    def run(self, g, ctx):
        for p in players_of(self.who, g, ctx):
            cands = [o for o in g.s.battlefield if g.ctrl(o) == p and self.filt.match(g, o, ctx.get("source"), {"controller": p})]
            moves = []
            if cands:
                opts = [Opt(f"choose:{g.ref(o)}", o, sig=("exc", g.obj_sig(o))) for o in cands]
                o = g.ask(Decision(p, "ChooseCard", opts, prompt="Choose a creature you control to exile."))
                moves.append((o, "exile", {}))
            moves += [(o, "exile", {}) for o in list(g.s.players[p].graveyard)]
            g.move_batch(moves)


def break_out_reveal(g, ctx, p, top, chosen):
    """Break Out: "You may reveal a creature card from among them. If that card has mana value 2
    or less, you may put it onto the battlefield and it gains haste until end of turn. If you
    didn't put the revealed card onto the battlefield this way, put it into your hand." (CR
    701.20a reveal; the choices are made as the spell resolves, 608.2d)."""
    from .mana import mana_value
    cands = [o for o in top if "Creature" in g.chars(o).types]
    if not cands:
        return
    opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("dig", g.obj_sig(o))) for o in cands]
    a = g.ask(Decision(p, "Dig", opts, prompt="Reveal a creature card from among them (or done)."))
    if a is None:
        return
    chosen.append(a)
    g.emit({"type": "reveal", "obj": a, "player": p})
    if mana_value(g.chars(a).mana_cost) <= 2:
        y = g.ask(Decision(p, "OptionalEffect", [Opt("yes", True), Opt("no", False)],
                           subject=g.ref(ctx["source"]) if ctx.get("source") in g.s.objs else None,
                           prompt=f"Put {g.name_of(a)} onto the battlefield with haste?"))
        if y:
            res = g.move_batch([(a, "battlefield", {"controller": p})])
            nid = res[0] if res else None
            if nid is not None and g.s.objs.get(nid) is not None and g.s.objs[nid].zone == "battlefield":
                Pump(Sel(lambda g_, c_, nid=nid: [nid], "revealed card"), None, None, keywords=["haste"]).run(g, ctx)
                return
            if a not in g.s.objs or g.s.objs[a].zone != "library":
                return  # it went somewhere else instead (a replacement effect)
    g.move(a, "hand")


def lki_controllers(sel):
    """"Its controller" of objects an effect may have moved already (destroyed, exiled): the
    controller as the object last existed on the battlefield (CR 608.2h, 113.7a)."""
    def fn(g, ctx):
        out = []
        for x in sel.get(g, ctx):
            if isinstance(x, tuple):
                continue
            o = g.s.objs.get(x)
            if o is not None and o.zone == "battlefield":
                out.append(("player", g.ctrl(x)))
                continue
            c = g.s.lki.get(x)
            if c is not None:
                out.append(("player", c.controller))
        return out
    return Sel(fn, f"controller of {sel.text}")


class PlayerSearch(E):
    """A player searches their library (CR 701.23): "Its controller searches their library for a
    basic land card, puts it onto the battlefield tapped with a stun counter on it, then
    shuffles." (Magmatic Hellkite), "That land's controller may search ..." (Demolition Field),
    "That player may search their library for that many basic land cards ..." (Settle the
    Wreckage), "Search your library for a card, put it into your graveyard, then shuffle." (Lively
    Dirge). A search for cards with a stated quality may find fewer (701.23b: "done" offered); a
    search for a quantity of cards must find that many if it can (701.23d). "May search": the
    player chooses whether to search (118.12b); then the library is shuffled either way only if
    they searched."""

    def __init__(self, who, filt, n=1, zone="battlefield", tapped=False, counters=None, optional=False,
                 must_find=False, text=""):
        self.who = who
        self.filt = filt
        self.n = amount(n)
        self.zone = zone
        self.tapped = tapped
        self.counters = dict(counters or {})
        self.optional = optional
        self.must_find = must_find
        self.text = text

    def run(self, g, ctx):
        for p in players_of(self.who, g, ctx):
            n = self.n(g, ctx)
            if self.optional:
                a = g.ask(Decision(p, "OptionalEffect", [Opt("yes", True), Opt("no", False)],
                                   subject=g.ref(ctx["source"]) if ctx.get("source") in g.s.objs else None,
                                   prompt=f"Search your library? ({self.text})"))
                if not a:
                    continue
            pl = g.s.players[p]
            sub = dict(ctx, controller=p)
            # CR 701.23a, 701.23f: the player searches; "whenever an opponent searches their
            # library" triggers even if nothing is found (as in Search.run).
            g.emit({"type": "search", "player": p, "library": p})
            found = []
            while len(found) < n:
                cands = [o for o in pl.library if o not in found and (self.filt is None or self.filt.match(g, o, ctx.get("source"), sub))]
                if not cands:
                    break
                opts = [] if self.must_find else [Opt("done", None)]
                opts += [Opt(f"choose:{g.ref(o)}", o, sig=("srch", g.obj_sig(o))) for o in cands]
                a = g.ask(Decision(p, "Search", opts, prompt=f"Search: {self.text}"))
                if a is None:
                    break
                found.append(a)
            opts_ = {"tapped": self.tapped, "controller": p}
            if self.counters:
                opts_["counters"] = dict(self.counters)
            res = g.move_batch([(o, self.zone, dict(opts_)) for o in found])
            ctx.setdefault("vars", {})["found"] = [r for r in res if r]
            g.shuffle(p)


class MillEach(E):
    """"Any number of target players each mill cards equal to the number of cards in their
    graveyard." (CR 701.17a; the number is each player's own, counted as they mill)."""

    def __init__(self, who, per_player, text=""):
        self.who = who
        self.per_player = per_player
        self.text = text

    def run(self, g, ctx):
        for p in players_of(self.who, g, ctx):
            g.mill(p, max(0, self.per_player(g, ctx, p)))


class ReflexiveThatMany(E):
    """CR 603.12: "When you discard one or more nonland cards this way, tap up to that many target
    creatures ...": a reflexive trigger, created only if at least one such card was discarded; its
    "that many" (the number of target creatures it may have) is fixed as it triggers."""

    def __init__(self, eff, specs, count, text=""):
        self.eff = eff
        self.specs = list(specs)
        self.count = count
        self.text = text

    def run(self, g, ctx):
        from .ir import Triggered
        from .triggers import PendingTrigger
        n = self.count(g, ctx)
        if n <= 0:
            return
        ab = Triggered(None, self.eff, self.specs, text=self.text)
        data = dict(ctx.get("data") or {})
        data["that_many"] = n
        g.s.pending.append(PendingTrigger(ab, ctx.get("source"), ctx.get("source_chars") or ctx.get("chars"),
                                          ctx["controller"], ctx.get("event"), data, 1))


class ChooseKeyword(E):
    """"Target creature gains your choice of double strike or lifelink until end of turn." The
    choice is made as the spell resolves (CR 608.2d); it's offered as a mode-like choice (kind
    ChooseMode, mode:n in Oracle order, as the EUT does)."""

    def __init__(self, sel, kws, text=""):
        self.sel = sel
        self.kws = list(kws)
        self.text = text

    def run(self, g, ctx):
        objs = [o for o in objs_of(self.sel, g, ctx) if g.s.objs[o].zone == "battlefield"]
        if not objs:
            return
        opts = [Opt(f"mode:{i + 1}", k) for i, k in enumerate(self.kws)]
        k = g.ask(Decision(ctx["controller"], "ChooseMode", opts, prompt=f"Choose: {' or '.join(self.kws)}"))
        Pump(self.sel, None, None, keywords=[k]).run(g, ctx)


class ReturnTotalMV(E):
    """Lively Dirge: "Return up to two creature cards with total mana value 4 or less from your
    graveyard to the battlefield." Not targeted: the cards are chosen as the mode resolves (CR
    608.2d), one at a time, each keeping the total within the limit; they enter at once."""

    def __init__(self, n, total, filt, text=""):
        self.n = n
        self.total = total
        self.filt = filt
        self.text = text

    def run(self, g, ctx):
        from .mana import mana_value
        p = ctx["controller"]
        pl = g.s.players[p]
        chosen, used = [], 0
        while len(chosen) < self.n:
            cands = [o for o in pl.graveyard if o not in chosen and self.filt.match(g, o, ctx.get("source"), ctx)
                     and used + mana_value(g.chars(o).mana_cost) <= self.total]
            if not cands:
                break
            opts = [Opt("done", None)] + [Opt(f"choose:{g.ref(o)}", o, sig=("rtn", g.obj_sig(o))) for o in cands]
            a = g.ask(Decision(p, "Search", opts, prompt=f"Choose a card to return ({self.text})"))
            if a is None:
                break
            chosen.append(a)
            used += mana_value(g.chars(a).mana_cost)
        g.move_batch([(o, "battlefield", {"controller": p}) for o in chosen])


def play_permission_valid(g, o, p):
    """A card in exile that p may play (CR 601.3: an effect allows it; the permission belongs to
    that object, 400.7) until its duration ends: through turn `t` (step None), or until step `st`
    of turn `t` begins ("until your next end step"), or, with no end ("for as long as it remains
    exiled", Outrageous Robbery, Cruelclaw's Heist), while it stays in exile.

    The one record of a permission, flags['may_play'] = {"player": p, "until": (t, st) | None,
    "any_type": spend mana as though it were mana of any type (609.4b), "lands": a land card may
    be played too (305.1; "cast" permissions don't cover lands)}; casting.play_permissions reads
    it (and the linked "you may cast the exiled card" statics)."""
    mp = o.flags.get("may_play") if o is not None else None
    # "zone": a permission for a card in another zone (Tablet of Discovery: the milled card, in
    # the graveyard; ref/cards/round2_b2.py); exile otherwise.
    if not mp or mp.get("player") != p or o.zone != mp.get("zone", "exile"):
        return False
    cond = mp.get("if_control")
    if cond is not None and not any(g.ctrl(x) == p and cond.match(g, x, None, {"controller": p}) for x in g.s.battlefield):
        return False  # "you may play them if you control a Wizard" (Flameshape; checked as the card is played)
    if mp.get("until") is None:
        return True
    t, st = mp["until"]
    s = g.s
    if s.turn != t:
        return s.turn < t
    if st is None:
        return True
    from .turn import STEPS
    return STEPS.index(s.step) < STEPS.index(st)


class ExileTopMayPlay(E):
    """"Exile the top N cards of your library. You may play them this turn." (Bonehoard
    Dracosaur), "... Until the end of your next turn, you may play that card." (Cori Mountain
    Monastery), "... You may play those cards until your next end step." (Dragonhawk). The cards
    are exiled face up (406.3); playing them follows the normal rules for playing a card (a land
    uses a land play, 305.2; spells their costs and timing, 601.2), only the zone is allowed."""

    def __init__(self, n, duration, text=""):
        self.n = amount(n)
        self.duration = duration
        self.text = text

    def _until(self, g, p):
        s = g.s
        nxt = s.turn + (2 if s.active == p else 1)  # turns alternate (no extra turns, see run)
        if self.duration == "this turn":
            return (s.turn, None)
        if self.duration == "end of your next turn":
            return (nxt, None)
        if self.duration == "your next end step":
            from .turn import STEPS
            if s.active == p and STEPS.index(s.step) < STEPS.index("End"):
                return (s.turn, "End")
            return (nxt, "End")
        raise Unsupported("play permission duration " + repr(self.duration))

    def run(self, g, ctx):
        p = ctx["controller"]
        pl = g.s.players[p]
        n = min(max(0, self.n(g, ctx)), len(pl.library))
        ctx.setdefault("vars", {})["exiled"] = []
        if n <= 0:
            return
        if g.s.extra_turns:
            raise Unsupported("play-permission durations with extra turns pending")
        g.top_hook(p, n, "exile")
        top = pl.library[:n]
        until = self._until(g, p)
        res = g.move_batch([(o, "exile", {"cause": "effect"}) for o in top])
        kept = [r for r in res if r is not None]
        for r in kept:
            if g.s.objs[r].zone == "exile":
                g.s.objs[r].flags["may_play"] = {"player": p, "until": until, "any_type": False, "lands": True}
        ctx["vars"]["exiled"] = kept
        g.s.touch()


class DelayedYourNextEndStep(E):
    """CR 603.7: "At the beginning of your next end step, ~ deals 2 damage to each opponent for
    each of those cards that are still exiled." (Dragonhawk): a delayed trigger created on
    resolution; "those cards" are the objects exiled this way (the same objects only while they
    stay in exile, 400.7). The damage is one amount, 2 times the number of such cards."""

    def __init__(self, per, text=""):
        self.per = per
        self.text = text

    def run(self, g, ctx):
        from .ir import Triggered
        from ..cards.triggers import Step
        ids = tuple((ctx.get("vars") or {}).get("exiled") or [])
        per = self.per

        def amt(g2, c2, ids=ids, per=per):
            return per * sum(1 for x in ids if x in g2.s.objs and g2.s.objs[x].zone == "exile")
        eff = Damage(Amount(amt, "per card still exiled"), opponent(), text=self.text)
        ab = Triggered(Step("End", "you"), eff, text=self.text)
        g.s.delayed.append({"trigger": ab.trigger, "ability": ab, "controller": ctx["controller"],
                            "source": ctx.get("source"), "source_chars": ctx.get("source_chars"),
                            "once": True, "data": {}})



_LAND_NAMES = None


def land_card_names():
    """Every land card name in the Oracle card reference (CR 201.4, 201.4a: a name of a card whose
    Oracle characteristics match; 201.4d: a land back face's name may be chosen; not a token's
    name, 201.4)."""
    global _LAND_NAMES
    if _LAND_NAMES is None:
        DB._load_full()
        out = set()
        for name, rec in (DB._full or {}).items():
            if rec.get("layout") in ("token", "double_faced_token") or "Token" in (rec.get("type_line") or ""):
                continue
            faces = rec.get("card_faces") or []
            if faces and rec.get("layout") not in ("normal", "saga", "class", "case"):
                for f in faces:
                    left = (f.get("type_line") or "").split("—")[0]
                    if "Land" in left.split():
                        out.add(f["name"])
            else:
                left = (rec.get("type_line") or "").split("—")[0]
                if "Land" in left.split():
                    out.add(name)
        if not out:
            raise Unsupported("choose a land card name: no Oracle card list available")
        _LAND_NAMES = sorted(out)
    return _LAND_NAMES


class ChooseCardName(E):
    """"When this land enters, choose a land card name." (Petrified Hamlet; CR 201.4a). The name is
    remembered by the permanent (its abilities refer to "the chosen name", 607.2d-style); if it
    has left the battlefield, the choice has no effect."""

    def __init__(self, kind="land", text=""):
        self.kind = kind
        self.text = text

    def run(self, g, ctx):
        if self.kind != "land":
            raise Unsupported("choose a card name of kind " + repr(self.kind))
        p = ctx["controller"]
        opts = [Opt("name:" + n, n) for n in land_card_names()]
        n = g.ask(Decision(p, "NameCard", opts, prompt="Choose a land card name."))
        src = ctx.get("source")
        o = g.s.objs.get(src)
        if o is not None and o.zone == "battlefield":
            o.chosen = dict(o.chosen, name=n)
            g.s.touch()


# Keys of a spell's context that aren't decisions made for it (CR 707.10): recomputed for a copy.
_COPY_FRESH = ("source", "controller", "item", "chars", "targets", "paid", "resolving", "resolving_item",
               "legal", "stop", "spell_dest", "vars")


def copy_spell(g, it, controller, new_targets=False, lki=None):
    """Put a copy of the spell of stack item `it` on the stack under `controller` (CR 707.10):
    the copy has the spell's copiable values (707.2) and every decision made for it as it was
    cast: modes, targets, X, kicker, additional and alternative costs (a promised gift, collected
    evidence, ...); it isn't cast (no cast triggers, no mana spent). new_targets: "You may choose
    new targets for the copy" (707.10c: each target may stay or change to another legal one). A
    copy of a permanent spell becomes a token as it resolves (707.10f; zones.move); off the stack
    it ceases to exist (704.5e). Returns the copy's object id. lki: the spell's object as it last
    existed on the stack when it has left it (`it` then its stack item as it left, s.lki_spell):
    the copy uses that last known information (CR 608.2h, 707.10)."""
    s = g.s
    o = lki if lki is not None else s.objs[it.obj]
    n = Obj(s.new_id(), o.card, controller, "stack")
    n.controller = controller
    n.face = o.face
    n.is_copy = True
    n.copy_of_spell = True
    n.flags["copiable"] = g.base_chars(o)  # CR 707.2 copiable values of the spell
    n.x, n.kicked, n.modes = o.x, o.kicked, list(o.modes)
    n.key = g.new_key(g.base_chars(o).name or o.card)
    n.timestamp = s.new_ts()
    s.objs[n.id] = n
    it2 = StackItem("spell", controller, s.new_id())
    it2.obj = n.id
    it2.key = n.key
    ctx2 = {k: (list(v) if isinstance(v, list) else v) for k, v in it.ctx.items() if k not in _COPY_FRESH}
    ctx2.update({"source": n.id, "controller": controller, "item": it2.id})
    it2.ctx = ctx2
    it2.targets = [list(grp) for grp in it.targets]
    s.stack.append(it2)
    s.touch()
    ctx2["chars"] = g.chars(n.id)
    if new_targets and it2.targets:
        f = g.spell_face(n.id, n.face)
        sa = g.spell_ability(f)
        specs = g.target_specs(sa, ctx2) if sa is not None else []
        groups = []
        for spec, grp in zip(specs, it2.targets):
            out = []
            for t in grp:
                cands = [x for x in g.legal_targets(spec, controller, ctx2, out) if x not in out]
                if t not in cands and t not in out:
                    cands.append(t)  # 707.10c: may stay unchanged, even if illegal
                opts = [Opt(f"target:{g.ref(x)}", x, sig=("target", x if isinstance(x, tuple) else g.obj_sig(x))) for x in cands]
                out.append(g.ask(Decision(controller, "Target", opts, subject=g.ref(n.id),
                                          prompt=f"Choose a new target for the copy (or keep it): {spec.text}")))
            groups.append(out)
        it2.targets = groups
    ctx2["targets"] = it2.targets
    g.emit({"type": "spell_copied", "player": controller, "obj": n.id, "item": it2.id})
    if it2.targets:
        g.became_targets(it2, copy=True)
    return n.id
