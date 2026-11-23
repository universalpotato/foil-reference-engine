"""mtg-ref: an independent reference implementation of the Magic: The Gathering rules.

Written from the Comprehensive Rules (cr/MagicCompRules-20260925.txt) and Oracle text only.
Rule numbers in comments refer to that CR version.
"""


class Unsupported(Exception):
    """A card, ability or rule the reference engine doesn't implement. Never misplay silently."""


class IllegalChoice(Exception):
    """A submitted choice is not one of the pending decision's options."""
