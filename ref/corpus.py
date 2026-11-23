"""Run edge-case tests (scenario/v0 + expect) on the reference engine.

    python3 -m ref.corpus [paths...] [-v] [--json out.json]

A test result is one of: pass, fail (an expectation didn't hold), unsupported (the reference
engine refuses a card/rule: fail-loud), error (runner/engine bug or malformed test).
"""
import glob
import json
import os
import sys
import traceback

from . import Unsupported, IllegalChoice
from .engine.decisions import EngineLoop, IllegalAction
from .engine.rules import Rules
from .scenario import load_scenario, find_ref, obj_state, canonical_state

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Fail(Exception):
    pass


def norm_counters(d):
    return {k: v for k, v in d.items() if v}


class Runner:
    """Drives a Game through a scenario's choice script (macros, auto mana)."""

    def __init__(self, scn, step_cap=20000):
        self.scn = scn
        self.auto_mana = (scn.get("auto") or {}).get("mana", True)
        self.game = load_scenario(scn, step_cap=step_cap)
        self.skip_mana = 0       # scripted mana picks already fed as a set
        self.drain()

    def drain(self):
        """Answer PayMana decisions automatically (first option in canonical order)."""
        g = self.game
        n = 0
        while self.auto_mana and g.pending is not None and g.pending.kind == "PayMana":
            g.submit(g.pending.options[0].canon)
            n += 1
            if n > 100:
                raise Fail("auto mana loop")
        # Cards put on the bottom "in any order" (CR 401.4): the order is hidden and the EUT
        # doesn't ask it, so scripts don't either; take the first order.
        while (self.scn.get("auto") or {}).get("order_bottom", True) and g.pending is not None and g.pending.kind == "OrderBottom":
            g.submit(g.pending.options[0].canon)
            n += 1
            if n > 200:
                raise Fail("auto bottom-order loop")

    def apply(self, choice, upcoming=()):
        g = self.game
        if self.skip_mana:
            if choice.startswith("mana:"):
                self.skip_mana -= 1
                return
            self.skip_mana = 0
        if choice.startswith("mana:") and g.pending is not None and g.pending.kind == "PayMana":
            # A run of scripted mana picks is a set (format §3.1): feed it in the engine's own
            # canonical order (earliest offered first), then skip those script entries.
            run = [choice]
            for c in upcoming:
                if not c.startswith("mana:"):
                    break
                run.append(c)
            want = list(run)
            while want and g.pending is not None and g.pending.kind == "PayMana":
                order = [x.canon for o in g.pending.options for x in [o] + o.alts]
                nxt = next((c for c in order if c in want), None)
                if nxt is None:
                    raise IllegalChoice(f"none of the scripted mana picks {want} is offered: {g.pending.canons()}")
                want.remove(nxt)
                g.submit(nxt)
                self.drain()
            self.skip_mana = len(run) - 1
            return
        if choice.startswith("pass-until:"):
            target = choice[len("pass-until:"):]
            turn = None
            if "@" in target:
                target, t = target.split("@")
                turn = int(t)
            guard = 0
            while g.pending is not None:
                if g.view.step == target and (turn is None or g.view.turn == turn):
                    return
                if g.pending.kind != "Priority":
                    raise Fail(f"pass-until:{target}: non-priority decision {g.pending.kind} {g.pending.canons()} in {g.view.step}")
                g.submit("pass")
                self.drain()
                guard += 1
                if guard > 500:
                    raise Fail("pass-until never reached")
            return
        if choice == "resolve":
            st = g.view.stack
            if not st:
                return  # forced passes already resolved it
            top = st[-1].id
            guard = 0
            while g.pending is not None and any(it.id == top for it in g.view.stack):
                if g.pending.kind != "Priority":
                    return  # a choice during resolution: answered by the next scripted choice
                g.submit("pass")
                self.drain()
                guard += 1
                if guard > 50:
                    raise Fail("resolve never finished")
            return
        if g.pending is None:
            raise Fail(f"choice {choice!r} but the game is over: {g.s.result}")
        if choice in ("yes", "no") and g.pending.kind == "Search" and g.pending.find(choice) is None \
                and g.pending.find("done") is not None:
            # A "may" choice asked as one decision whose `done` declines (format
            # §3.1 *Optional choices*): a scripted yes/no for an engine that asks yes/no first
            # (the EUT's Bristlebud Farmer) is "the pick that follows" / `done`. harness.batch's
            # EUT runner maps the other way round.
            if choice == "yes":
                return
            choice = "done"
        g.submit(choice)
        self.drain()


def check_decision(g, exp, errors):
    d = g.pending
    if d is None:
        errors.append(f"expected a decision, game over ({g.s.result})")
        return
    if "player" in exp and d.player != exp["player"]:
        errors.append(f"decision player {d.player} != {exp['player']}")
    if "kind" in exp and d.kind != exp["kind"]:
        errors.append(f"decision kind {d.kind} != {exp['kind']} (options {d.canons()})")
    offered = set()
    for o in d.options:
        offered.add(o.canon)
        for a in o.alts:
            offered.add(a.canon)
    for c in exp.get("options_include", []):
        if c not in offered:
            errors.append(f"option {c!r} not offered (offered {sorted(offered)})")
    for c in exp.get("options_exclude", []):
        if c in offered:
            errors.append(f"option {c!r} offered but must not be")
    if "options_exact" in exp:
        want = set(exp["options_exact"])
        reps = set(d.canons())
        if not (want <= offered and len(reps) == len(want)):
            errors.append(f"options {sorted(reps)} != {sorted(want)}")


def _name_match(n, want):
    return n == want or n == f"{want} Token"


def check_state(g, exp, errors):
    r = Rules(g.view)
    s = r.s

    def obj(ref):
        x = find_ref(r, ref)
        if x is None or isinstance(x, tuple):
            return None
        return x

    for ref, zone in (exp.get("zones") or {}).items():
        x = obj(ref)
        z = "none" if x is None else s.objs[x].zone
        if z != zone:
            errors.append(f"{ref} in {z}, expected {zone}")
    for key in ("life", "poison", "speed"):
        if key in exp:
            for p, v in enumerate(exp[key]):
                if v is None:
                    continue
                have = getattr(s.players[p], key)
                if have != v:
                    errors.append(f"{key}[p{p}] = {have}, expected {v}")
    for ref, want in (exp.get("counters") or {}).items():
        x = obj(ref)
        if x is None:
            errors.append(f"counters: {ref} not found")
            continue
        have = norm_counters(s.objs[x].counters)
        if have != norm_counters(want):
            errors.append(f"counters {ref} = {have}, expected {norm_counters(want)}")
    for p, want in (exp.get("player_counters") or {}).items():
        have = norm_counters(s.players[int(p[1])].counters)
        if have != norm_counters(want):
            errors.append(f"player counters {p} = {have}, expected {want}")
    for ref, want in (exp.get("pt") or {}).items():
        x = obj(ref)
        if x is None:
            errors.append(f"pt: {ref} not found")
            continue
        c = r.chars(x)
        if [c.power, c.toughness] != list(want):
            errors.append(f"pt {ref} = {c.power}/{c.toughness}, expected {want[0]}/{want[1]}")
    simple = {"tapped": "tapped", "damage": "damage", "controller": "controller",
              "attached_to": "attached_to", "face_down": "face_down"}
    for key, field in simple.items():
        for ref, want in (exp.get(key) or {}).items():
            x = obj(ref)
            if x is None:
                errors.append(f"{key}: {ref} not found")
                continue
            st = obj_state(r, x)
            have = st[field]
            if key == "attached_to" and want is not None and not str(want).startswith(("key:", "p")):
                want = f"key:{want}"
            if have != want:
                errors.append(f"{key} {ref} = {have!r}, expected {want!r}")
    for key, inc in (("keywords_include", True), ("keywords_exclude", False)):
        for ref, kws in (exp.get(key) or {}).items():
            x = obj(ref)
            if x is None:
                errors.append(f"{key}: {ref} not found")
                continue
            have = set(r.chars(x).keywords())
            for k in kws:
                if (k in have) != inc:
                    errors.append(f"{ref} keyword {k!r} {'missing' if inc else 'present'} (has {sorted(have)})")
    for key, inc in (("types_include", True), ("types_exclude", False)):
        for ref, ts in (exp.get(key) or {}).items():
            x = obj(ref)
            if x is None:
                errors.append(f"{key}: {ref} not found")
                continue
            c = r.chars(x)
            have = c.types | c.subtypes | c.supertypes
            for t in ts:
                if (t in have) != inc:
                    errors.append(f"{ref} type {t!r} {'missing' if inc else 'present'} (has {sorted(have)})")
    for ref, cols in (exp.get("colors") or {}).items():
        x = obj(ref)
        if x is None:
            errors.append(f"colors: {ref} not found")
            continue
        have = sorted(r.chars(x).colors)
        if have != sorted(cols):
            errors.append(f"colors {ref} = {have}, expected {sorted(cols)}")
    for flt in exp.get("count", []):
        n = 0
        for oid, o in s.objs.items():
            if "zone" in flt and o.zone != flt["zone"]:
                continue
            if o.zone == "library" and "zone" not in flt:
                continue
            c = r.chars(oid)
            ctrl = c.controller if o.zone in ("battlefield", "stack") else o.owner
            if "controller" in flt and ctrl != flt["controller"]:
                continue
            if "name" in flt and not _name_match(c.name, flt["name"]):
                continue
            if "token" in flt and o.token != flt["token"]:
                continue
            if "type" in flt and flt["type"] not in (c.types | c.subtypes):
                continue
            n += 1
        if n != flt["n"]:
            errors.append(f"count {flt} = {n}")
    for key, zname in (("hand_count", "hand"), ("library_count", "library"),
                       ("graveyard_count", "graveyard"), ("exile_count", "exile")):
        if key in exp:
            for p, v in enumerate(exp[key]):
                if v is None:
                    continue
                have = len(s.players[p].zone(zname))
                if have != v:
                    errors.append(f"{key}[p{p}] = {have}, expected {v}")
    if "stack" in exp:
        want = exp["stack"]
        have = []
        for it in reversed(s.stack):
            src = it.obj if it.kind == "spell" else it.source
            have.append({"source": r.ref(src) if src is not None else None, "kind": it.kind})
        if len(have) != len(want):
            errors.append(f"stack {have} != {want}")
        else:
            for h, w in zip(have, want):
                for k, v in w.items():
                    if k == "source":
                        x = find_ref(r, v)
                        if r.ref(x) if x is not None else v != h["source"]:
                            if (r.ref(x) if x is not None else v) != h["source"]:
                                errors.append(f"stack item {h} != {w}")
                    elif h.get(k) != v:
                        errors.append(f"stack item {h} != {w}")
    if "stack_size" in exp and len(s.stack) != exp["stack_size"]:
        errors.append(f"stack size {len(s.stack)} != {exp['stack_size']}")
    if "game_over" in exp:
        want = exp["game_over"]
        res = s.result
        if want is False:
            if res is not None:
                errors.append(f"game over {res}, expected running")
        elif res != want:
            errors.append(f"result {res}, expected {want}")
    for key in ("step", "turn", "active"):
        if key in exp:
            have = getattr(s, key)
            if have != exp[key]:
                errors.append(f"{key} = {have}, expected {exp[key]}")
    if "mana_pool" in exp:
        for p, want in enumerate(exp["mana_pool"]):
            have = sorted(u.color for u in s.players[p].pool)
            if have != sorted(want):
                errors.append(f"mana pool p{p} = {have}, expected {sorted(want)}")


def run_test(scn, step_cap=20000):
    """Returns (status, details)."""
    exps = scn.get("expect") or []
    if isinstance(exps, dict):
        exps = [exps]
    choices = list(scn.get("choices") or [])
    errors = []
    try:
        runner = Runner(scn, step_cap=step_cap)
    except Unsupported as e:
        return "unsupported", str(e)
    except EngineLoop as e:
        if any(x.get("terminates") is False for x in exps):
            return "pass", "loop expected"
        return "fail", f"engine loop at start: {e}"

    def checkpoint(after):
        for x in exps:
            a = x.get("after", "end")
            if a == after or (after == len(choices) and a == "end"):
                g = runner.game
                if "decision" in x:
                    check_decision(g, x["decision"], errors)
                if "state" in x:
                    check_state(g, x["state"], errors)

    error_exp = {x["error"]["choice"]: x for x in exps if "error" in x}
    try:
        checkpoint(0)
        for i, ch in enumerate(choices):
            if i in error_exp:
                try:
                    runner.apply(ch)
                except (IllegalChoice, IllegalAction):
                    checkpoint(i + 1)
                    continue
                errors.append(f"choice #{i} {ch!r} was accepted but must be illegal")
                break
            try:
                runner.apply(ch, choices[i + 1:])
            except IllegalChoice as e:
                errors.append(f"choice #{i} {ch!r} rejected: {e}")
                break
            checkpoint(i + 1)
    except Unsupported as e:
        return "unsupported", str(e)
    except EngineLoop as e:
        if any(x.get("terminates") is False for x in exps):
            return "pass", "loop expected"
        return "fail", f"engine loop (no progress): {e}"
    except Fail as e:
        errors.append(str(e))
    if errors:
        return "fail", "; ".join(errors)
    return "pass", ""


def collect(paths):
    files = []
    for p in paths or [os.path.join(ROOT, "edge-cases")]:
        if os.path.isdir(p):
            files += sorted(glob.glob(os.path.join(p, "**", "*.json"), recursive=True))
        else:
            files.append(p)
    return files


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--json")
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args(argv)
    res = {}
    counts = {}
    for f in collect(a.paths):
        scn = json.load(open(f))
        try:
            st, det = run_test(scn)
        except Exception as e:  # noqa: BLE001
            st, det = "error", f"{type(e).__name__}: {e}"
            if a.trace:
                traceback.print_exc()
        rel = os.path.relpath(f, ROOT)
        res[rel] = {"status": st, "detail": det, "rules": scn.get("rules", [])}
        counts[st] = counts.get(st, 0) + 1
        if a.v or st in ("fail", "error"):
            print(f"{st.upper():12s} {rel}  {', '.join(scn.get('rules', [])[:3])}" + (f"\n             {det[:600]}" if det else ""))
    print(" ".join(f"{k}={v}" for k, v in sorted(counts.items())), f"total={sum(counts.values())}")
    if a.json:
        json.dump(res, open(a.json, "w"), indent=1, sort_keys=True)
    return 0 if not counts.get("fail") and not counts.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
