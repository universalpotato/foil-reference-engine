"""Game: a committed State plus the choice tape since the last commit (see DESIGN.md).

Each submit re-runs the current unit of play on a scratch copy of the committed state with the
answers so far. A decision raised at a top-level point (priority, mulligan, declarations) is a
commit point: the scratch state becomes the committed state. A decision raised inside an action
(targets, mana, modes, ...) leaves the committed state alone and extends the tape.
Deterministic: the RNG lives in the state.
"""
import copy

from .decisions import NeedDecision, IllegalAction, EngineLoop
from .rules import Rules
from .. import IllegalChoice


class Game:
    def __init__(self, state, hooks=None, step_cap=20000):
        self.s = state
        self.tape = []
        self.pending = None
        self.history = []   # every submitted canonical choice, in order
        self.hooks = hooks
        self.step_cap = step_cap
        self.error = None
        self.forced = []    # forced decisions of the last run, after its last answer (Rules.forced_new)
        self._advance()

    def _scratch(self):
        self.s.touch()
        return copy.deepcopy(self.s)

    def _advance(self, tape=None):
        tape = self.tape if tape is None else tape
        s = self._scratch()
        r = Rules(s, tape, hooks=self.hooks)
        r.step_cap = self.step_cap
        try:
            r.run()
        except NeedDecision as nd:
            self.forced = r.forced_new
            if r.tape_pos < len(tape):
                raise RuntimeError("tape not fully consumed before a new decision")
            if nd.decision.top:
                s.touch()
                self.s = s
                self.tape = []
            else:
                self.tape = list(tape)
            self.view = s
            self.pending = nd.decision
            return
        # Game over.
        self.forced = r.forced_new
        self.s = s
        self.view = s
        self.tape = []
        self.pending = None

    @property
    def over(self):
        return self.s.result is not None and self.pending is None

    def options(self):
        return [] if self.pending is None else self.pending.canons()

    def submit(self, canon):
        if self.pending is None:
            raise IllegalChoice("no pending decision")
        if self.pending.find(canon) is None:
            raise IllegalChoice(f"{canon!r} is not an option: {self.pending.canons()}")
        saved = (self.s, list(self.tape), self.pending, self.view)
        try:
            self._advance(self.tape + [canon])
        except IllegalAction as e:
            self.s, self.tape, self.pending, self.view = saved
            raise IllegalChoice(f"{canon!r} can't be completed: {e}")
        self.history.append(canon)

    def clone(self):
        g = Game.__new__(Game)
        g.s = self._scratch()
        g.tape = list(self.tape)
        g.pending = self.pending
        g.view = self.view
        g.history = list(self.history)
        g.hooks = self.hooks
        g.step_cap = self.step_cap
        g.error = self.error
        g.forced = list(self.forced)
        return g

    def rules(self):
        """A Rules view of the current (in-progress) state, for read-only queries."""
        return Rules(self.view)
