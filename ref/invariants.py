"""Engine invariants that need no second engine (README goal 3):

- every object is in exactly one zone, and zone lists agree with object zones;
- no object id appears twice; object ids on the stack refer to stack objects;
- life, poison and counters are integers; counters are positive;
- every pending decision has at least one option, and options are distinct;
- the game makes progress (the engine's step cap raises EngineLoop otherwise).

Also a self-fuzzer: random legal play on the reference engine alone.

    python3 -m ref.invariants --games 200 --seed 0
"""
import random
import sys
import time
import traceback

from . import Unsupported, IllegalChoice
from .engine.decisions import EngineLoop, IllegalAction
from .engine.rules import Rules


class InvariantError(Exception):
    pass


def check(game):
    s = game.view
    seen = {}

    def put(oid, where):
        if oid in seen:
            raise InvariantError(f"object {oid} in {seen[oid]} and {where}")
        seen[oid] = where
        o = s.objs.get(oid)
        if o is None:
            raise InvariantError(f"{where} lists unknown object {oid}")
        z = where.split(":")[-1]
        if o.zone != z:
            raise InvariantError(f"object {oid} has zone {o.zone} but is listed in {where}")

    for oid in s.battlefield:
        put(oid, "battlefield")
    for pl in s.players:
        for z in ("library", "hand", "graveyard", "exile"):
            for oid in pl.zone(z):
                put(oid, f"p{pl.index}:{z}")
        if not isinstance(pl.life, int) or not isinstance(pl.poison, int):
            raise InvariantError("non-integer life/poison")
        for k, v in pl.counters.items():
            if not isinstance(v, int) or v < 0:
                raise InvariantError(f"player counter {k}={v}")
    for it in s.stack:
        if it.kind == "spell":
            put(it.obj, "stack")
    for oid in s.command:
        put(oid, "command")
    for oid, o in s.objs.items():
        if oid not in seen:
            raise InvariantError(f"object {oid} ({o.card}) is in no zone list (zone {o.zone})")
        for k, v in o.counters.items():
            if not isinstance(v, int) or v <= 0:
                raise InvariantError(f"{o.card} counter {k}={v}")
        if not isinstance(o.damage, int) or o.damage < 0:
            raise InvariantError(f"{o.card} damage {o.damage}")
    d = game.pending
    if d is not None:
        if not d.options:
            raise InvariantError(f"decision {d.kind} without options")
        canons = d.canons()
        if len(set(canons)) != len(canons):
            raise InvariantError(f"duplicate options in {d.kind}: {canons}")


def self_fuzz(games, seed, max_steps=1500, decks=None, verbose=False):
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harness"))
    from harness import decks as D
    from .decklist import game_from_decks
    pool = D.ref_supported_pool() if hasattr(D, "ref_supported_pool") else sorted(D.ref_supported())
    stats = {}
    t0 = time.time()
    for k in range(games):
        rng = random.Random(seed + k)
        if decks:
            dl = decks
        else:
            cols = [rng.choice(["WU", "UB", "BR", "RG", "GW", "WB", "UR", "BG", "RW", "GU"]) for _ in (0, 1)]
            dl = [D.make_deck(rng, pool, c) for c in cols]
        try:
            g = game_from_decks(dl, seed + k)
            n = 0
            while g.pending is not None and n < max_steps:
                check(g)
                opts = g.pending.canons()
                c = rng.choice(opts)
                if g.pending.kind == "Priority" and "pass" in opts and rng.random() < 0.45:
                    c = "pass"
                try:
                    g.submit(c)
                except IllegalChoice as e:
                    raise InvariantError(f"offered option {c!r} rejected: {e}")
                n += 1
            st = "over" if g.pending is None else "max_steps"
        except Unsupported as e:
            st = "unsupported"
            if verbose:
                print("unsupported:", e)
        except EngineLoop as e:
            st = "loop"
            print(f"game {seed + k}: no progress: {e}")
        except InvariantError as e:
            st = "invariant"
            print(f"game {seed + k}: invariant: {e}")
        except Exception as e:  # noqa: BLE001
            st = "crash"
            print(f"game {seed + k}: crash {type(e).__name__}: {e}")
            if verbose:
                traceback.print_exc()
        stats[st] = stats.get(st, 0) + 1
    print(stats, f"{time.time() - t0:.0f}s")
    return stats


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=50)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-steps", type=int, default=1500)
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    self_fuzz(a.games, a.seed, a.max_steps, verbose=a.v)


if __name__ == "__main__":
    main()
