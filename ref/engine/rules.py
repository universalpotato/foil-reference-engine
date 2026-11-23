"""The rules engine: one object bound to a State, combining the mixins."""
from .decisions import NeedDecision, IllegalAction
from .layers import LayersMixin
from .zones import ZonesMixin
from .triggers import TriggersMixin
from .turn import TurnMixin
from .casting import CastingMixin
from .combat import CombatMixin
from .altcast import AltCastMixin
from .. import IllegalChoice


class Rules(LayersMixin, ZonesMixin, TriggersMixin, TurnMixin, CastingMixin, CombatMixin, AltCastMixin):
    def __init__(self, state, tape=None, hooks=None):
        self.s = state
        self.tape = list(tape or [])
        self.tape_pos = 0
        self.steps_without_decision = 0
        self._excluded = set()
        # Permanents whose own {T} cost is still to be paid (601.2h) while the mana of that
        # activation is being paid (601.2g): they can't be tapped as a mana ability's helper.
        self._keep_untapped = set()
        self._base_cost = []
        self._cost_with = None
        self._pending_extra = []
        self.hooks = hooks
        # Forced decisions made after the last tape answer (new in this run): harness bookkeeping
        # only (a forced choice among identical objects may be the counterpart of a recorded one).
        self.forced_new = []

    def ask(self, decision):
        """Return the chosen option's data. Single-option decisions are forced and answered
        without consuming the tape (and are never recorded)."""
        self.steps_without_decision = 0
        if not decision.options:
            raise IllegalAction(f"decision {decision.kind} with no options")
        if len(decision.options) == 1:
            if self.tape_pos == len(self.tape):
                self.forced_new.append(decision)
            return decision.options[0].data
        if self.tape_pos < len(self.tape):
            ans = self.tape[self.tape_pos]
            o = decision.find(ans)
            if o is None:
                raise IllegalChoice(f"{ans!r} is not an option of {decision.kind}: {decision.canons()}")
            self.tape_pos += 1
            return o.data
        raise NeedDecision(decision)

    # Hooks for the differential harness (library alignment).
    def draw_hook(self, p, top):
        if self.hooks is not None and hasattr(self.hooks, "before_draw"):
            self.hooks.before_draw(self, p)

    def look_hook(self, p, i):
        """Before the i-th card from the top of p's library is looked at (scry, surveil, ...)."""
        if self.hooks is not None and hasattr(self.hooks, "before_look"):
            self.hooks.before_look(self, p, i)
        lib = self.s.players[p].library
        if i < len(lib):
            self.s.know.looks(self, p, [lib[i]])  # CR 701.20e (scry 701.22a, surveil 701.25a)

    def explore_hook(self, p):
        if self.hooks is not None and hasattr(self.hooks, "before_explore"):
            self.hooks.before_explore(self, p)
        self.s.know.reveal(self, self.s.players[p].library[:1])  # CR 701.44a: reveal the top card

    def dig_hook(self, p, n, rest_to=None, pick_to=None):
        """Before a "look at the top N cards" effect looks at p's library (Dig). `rest_to`: the
        public zone the cards not chosen go to ("the rest into your graveyard"), if any;
        `pick_to`: where the chosen card goes when not to the hand (manifest dread: battlefield)."""
        if self.hooks is not None and hasattr(self.hooks, "before_dig"):
            if rest_to is None:
                self.hooks.before_dig(self, p, n)
            elif pick_to is None:
                self.hooks.before_dig(self, p, n, rest_to=rest_to)
            else:
                self.hooks.before_dig(self, p, n, rest_to=rest_to, pick_to=pick_to)
        self.s.know.looks(self, p, self.s.players[p].library[:n])  # "look at the top N", 701.20e

    def top_hook(self, p, n, zone):
        """Before the top n cards of p's library are put into a public zone (mill: graveyard;
        "exile the top N cards": exile)."""
        if self.hooks is not None and hasattr(self.hooks, "before_top"):
            self.hooks.before_top(self, p, n, zone)

    def shuffle_hook(self, p):
        if self.hooks is not None and hasattr(self.hooks, "after_shuffle"):
            self.hooks.after_shuffle(self, p)

    def reveal_top_hook(self, p):
        """Before the top card of p's library is revealed (Summon: Esper Maduin's chapter I)."""
        if self.hooks is not None and hasattr(self.hooks, "before_reveal_top"):
            self.hooks.before_reveal_top(self, p)
        self.s.know.reveal(self, self.s.players[p].library[:1])  # CR 701.20a

    def random_hook(self, p, cands, zone):
        """A card of p's chosen at random among public cards `cands`, about to be put into `zone`
        (Tersa Lightshatter: "exile a card at random from your graveyard"). The differential
        harness names the card the EUT's random choice moved (hooks.pick_random); None: the
        engine's own RNG decides."""
        if self.hooks is not None and hasattr(self.hooks, "pick_random"):
            return self.hooks.pick_random(self, p, cands, zone)
        return None
