"""The reference engine as a service.

JSON lines over stdin/stdout (same command names the EUT is asked to add, see
interfaces/scenario-and-state.md §6 and interfaces/eut-requests.md):

    python3 -m ref.service serve

    {"cmd":"scenario","scenario":{...}}        load a scenario/v0 position -> state
    {"cmd":"new","decks":[pathA,pathB],"seed":n} start a game from decklists -> state
    {"cmd":"state"}                              canonical state/v0
    {"cmd":"options"}                            pending decision with canonical options
    {"cmd":"submit_canon","canon":"pass"}        submit a canonical option -> state
    {"cmd":"clone"} / {"cmd":"restore","id":k}   snapshot handles (cheap, in-process)
    {"cmd":"history"}                            submitted canonical choices
    {"cmd":"quit"}

Every response has "ok"; failures carry "error" ("unsupported: ..." when the reference engine
refuses a card or rule).

CLI:
    python3 -m ref.service run SCENARIO.json [--choices c1 c2 ...]   print states/options per step
    python3 -m ref.service check SCENARIO.json                        run its expectations (like ref.corpus)
"""
import json
import sys

from . import Unsupported, IllegalChoice
from .engine.decisions import EngineLoop
from .scenario import load_scenario, canonical_state
from .decklist import game_from_decks, parse_decklist


class Service:
    def __init__(self):
        self.game = None
        self.snaps = {}

    def handle(self, req):
        cmd = req.get("cmd")
        try:
            if cmd == "scenario":
                self.game = load_scenario(req["scenario"])
                return self.state_resp()
            if cmd == "new":
                decks = [parse_decklist(p) for p in req["decks"]]
                self.game = game_from_decks(decks, int(req.get("seed", 0)))
                return self.state_resp()
            if cmd == "quit":
                return {}
            if self.game is None:
                return {"ok": False, "error": "no game: send scenario or new first"}
            if cmd == "state":
                return {"state": canonical_state(self.game)}
            if cmd == "options":
                d = self.game.pending
                return {"decision": d.to_json() if d else None}
            if cmd in ("submit_canon", "submit"):
                self.game.submit(req["canon"])
                return self.state_resp()
            if cmd == "history":
                return {"choices": list(self.game.history)}
            if cmd == "clone":
                k = len(self.snaps) + 1
                self.snaps[k] = self.game.clone()
                return {"id": k}
            if cmd == "restore":
                self.game = self.snaps[int(req["id"])].clone()
                return self.state_resp()
            return {"ok": False, "error": f"unknown cmd {cmd!r}"}
        except Unsupported as e:
            return {"ok": False, "error": f"unsupported: {e}"}
        except IllegalChoice as e:
            return {"ok": False, "error": f"illegal choice: {e}"}
        except EngineLoop as e:
            return {"ok": False, "error": f"no progress: {e}"}

    def state_resp(self):
        g = self.game
        return {"state": canonical_state(g), "game_over": g.pending is None,
                "result": g.s.result}


def serve():
    svc = Service()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            print(json.dumps({"ok": False, "error": f"bad JSON: {e}"}), flush=True)
            continue
        resp = svc.handle(req)
        resp.setdefault("ok", True)
        print(json.dumps(resp, default=str), flush=True)
        if req.get("cmd") == "quit":
            break


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] == "serve":
        return serve()
    if argv[0] == "run":
        scn = json.load(open(argv[1]))
        choices = scn.get("choices", [])
        if "--choices" in argv:
            choices = argv[argv.index("--choices") + 1:]
        from .corpus import Runner
        r = Runner(scn)
        print(json.dumps(canonical_state(r.game)))
        for c in choices:
            r.apply(c)
            print(json.dumps({"choice": c, "state": canonical_state(r.game)}))
        return 0
    if argv[0] == "check":
        from .corpus import run_test
        st, det = run_test(json.load(open(argv[1])))
        print(st, det)
        return 0 if st == "pass" else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
