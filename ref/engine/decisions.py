"""Decisions, options and canonical references (interfaces/scenario-and-state.md §2-3)."""


class Opt:
    """One option of a decision. `canon` is the canonical option string (shared with the EUT);
    `data` is engine payload; `alts` are canonical strings of interchangeable equivalents
    (identical objects are offered once, but naming any of them is accepted)."""
    __slots__ = ("canon", "data", "alts", "sig")

    def __init__(self, canon, data=None, sig=None):
        self.canon = canon
        self.data = data
        self.alts = []
        self.sig = sig if sig is not None else canon

    def __repr__(self):
        return f"Opt({self.canon})"


class AmountOptions:
    """The options `<prefix><k>` for every integer k in lo..hi, data k, built on demand: a
    damage division (AssignDamage `assign:<ref>:<n>`, interfaces/scenario-and-state.md) of a
    huge power has one option per amount (CR 510.1c), too many to hold as Opt objects. Behaves
    as a read-only list of distinct Opts (len, indexing, iteration); Decision.find parses the
    amount instead of scanning. The option strings are the same as a plain list's."""
    __slots__ = ("prefix", "lo", "hi")

    def __init__(self, prefix, lo, hi):
        self.prefix, self.lo, self.hi = prefix, lo, hi

    def __len__(self):
        return max(0, self.hi - self.lo + 1)

    def _opt(self, k):
        return Opt(f"{self.prefix}{k}", k)

    def __getitem__(self, i):
        n = len(self)
        if isinstance(i, slice):
            return [self._opt(self.lo + j) for j in range(*i.indices(n))]
        if i < 0:
            i += n
        if not 0 <= i < n:
            raise IndexError(i)
        return self._opt(self.lo + i)

    def __iter__(self):
        for k in range(self.lo, self.hi + 1):
            yield self._opt(k)

    def find(self, canon):
        if not canon.startswith(self.prefix):
            return None
        rest = canon[len(self.prefix):]
        if not rest.isdigit() or (len(rest) > 1 and rest[0] == "0"):
            return None
        k = int(rest)
        return self._opt(k) if self.lo <= k <= self.hi else None


class Decision:
    def __init__(self, player, kind, options, subject=None, prompt="", top=False, dedup=True):
        self.player = player
        self.kind = kind
        self.subject = subject
        self.prompt = prompt
        self.top = top
        if dedup and not isinstance(options, AmountOptions):  # distinct amounts: nothing to merge
            out, by_sig = [], {}
            for o in options:
                if o.sig in by_sig:
                    by_sig[o.sig].alts.append(o)
                else:
                    by_sig[o.sig] = o
                    out.append(o)
            options = out
        self.options = options

    def find(self, canon):
        if isinstance(self.options, AmountOptions):
            return self.options.find(canon)
        for o in self.options:
            if o.canon == canon:
                return o
        for o in self.options:
            for a in o.alts:
                if a.canon == canon:
                    return a
        return None

    def canons(self):
        return [o.canon for o in self.options]

    def to_json(self):
        return {"player": self.player, "kind": self.kind, "subject": self.subject,
                "prompt": self.prompt,
                "options": [{"canon": o.canon, "alts": [a.canon for a in o.alts]} for o in self.options]}

    def __repr__(self):
        return f"Decision(P{self.player} {self.kind} {self.canons()})"


class NeedDecision(Exception):
    def __init__(self, decision):
        super().__init__(decision.kind)
        self.decision = decision


class EngineLoop(Exception):
    """The engine ran too many internal steps without a decision (no progress)."""


class IllegalAction(Exception):
    """An action turned out to be illegal while being performed (CR 733): roll back."""
