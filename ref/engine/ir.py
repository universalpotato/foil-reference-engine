"""Ability IR: what the Oracle compiler produces and the rules engine executes.

Everything here is immutable card data. Effects run as `effect.run(g, ctx)` where `g` is the
rules engine (ref.engine.game.Rules) and `ctx` the resolving spell/ability's context.
"""
from .. import Unsupported


# ----------------------------------------------------------------------------- abilities

class Ability:
    kind = "?"
    text = ""
    keyword = None
    zones = ("battlefield",)

    def __repr__(self):
        return f"{type(self).__name__}({self.text[:40]!r})"


class Keyword(Ability):
    """A keyword ability (CR 702). Most are static rules hooks looked up by name."""
    kind = "keyword"

    def __init__(self, keyword, value=None, text=""):
        self.keyword = keyword
        self.value = value  # e.g. ward cost, equip cost
        self.text = text or keyword

    def __repr__(self):
        return f"Keyword({self.keyword}{'' if self.value is None else ' ' + str(self.value)})"


class SpellAbility(Ability):
    """The effect of an instant or sorcery (CR 113.3a)."""
    kind = "spell"

    def __init__(self, effect, targets=(), modes=None, text=""):
        self.effect = effect
        self.targets = list(targets)
        self.modes = modes  # Modes or None
        self.text = text


class Modes:
    """Modal spell/ability (CR 700.2). choose (min, max); modes: list of (targets, effect, text,
    extra_cost)."""

    def __init__(self, modes, min_n=1, max_n=1, repeat=False, spree=False):
        self.modes = modes
        self.min = min_n
        self.max = max_n
        self.repeat = repeat
        self.spree = spree


class Mode:
    def __init__(self, effect, targets=(), text="", cost=None):
        self.effect = effect
        self.targets = list(targets)
        self.text = text
        self.cost = cost or []  # spree additional mana cost


class Activated(Ability):
    kind = "activated"

    def __init__(self, cost, effect, targets=(), timing="instant", text="", mana=None,
                 zones=("battlefield",), condition=None, limit=None, loyalty=None, modes=None):
        self.cost = cost
        self.effect = effect
        self.targets = list(targets)
        self.timing = timing          # 'instant' | 'sorcery' (CR 602.5d)
        self.text = text
        self.mana = mana              # ManaProduce if a mana ability (CR 605.1a)
        self.zones = zones
        self.condition = condition    # "Activate only if ..." predicate(g, src, ctrl)
        self.limit = limit            # e.g. 1 = once each turn
        self.loyalty = loyalty        # loyalty ability (CR 606)
        self.modes = modes

    @property
    def is_mana(self):
        return self.mana is not None


class Triggered(Ability):
    kind = "triggered"

    def __init__(self, trigger, effect, targets=(), optional=False, condition=None, text="",
                 zones=("battlefield",), modes=None, limit=None):
        self.trigger = trigger
        self.effect = effect
        self.targets = list(targets)
        self.optional = optional      # "you may" at resolution is part of effect; this = "may" trigger
        self.condition = condition    # intervening "if" (CR 603.4): predicate(g, ctx)
        self.text = text
        self.zones = zones
        self.modes = modes
        self.limit = limit


class Static(Ability):
    kind = "static"

    def __init__(self, parts, text="", condition=None, zones=("battlefield",)):
        self.parts = list(parts)      # StaticPart list
        self.text = text
        self.condition = condition    # "as long as ..." predicate(g, src)
        self.zones = zones


# ----------------------------------------------------------------------------- costs

class Cost:
    def __init__(self, mana=(), tap=False, untap=False, life=0, sac_self=False, sacrifice=None,
                 discard=None, exile_self=False, exile_from_gy=None, remove_counters=None,
                 loyalty=None, tap_others=None, return_to_hand=None, discard_self=False,
                 exile_from_hand=None, blight=None, text=""):
        self.mana = list(mana)
        self.tap = tap
        self.untap = untap
        self.life = life
        self.sac_self = sac_self
        self.sacrifice = sacrifice          # (Filter, n)
        self.discard = discard              # (Filter, n)
        self.discard_self = discard_self
        self.exile_self = exile_self
        self.exile_from_gy = exile_from_gy  # (Filter, n)
        self.exile_from_hand = exile_from_hand
        self.remove_counters = remove_counters  # (kind, n)
        self.loyalty = loyalty              # +N / -N
        self.tap_others = tap_others        # (Filter, n): "Tap an untapped creature you control"
        self.return_to_hand = return_to_hand
        self.blight = blight
        self.put_counters = None            # (kind, n): "Put a page counter on ~"
        self.text = text

    def has_x(self):
        return "X" in self.mana


class ManaProduce:
    """What a mana ability adds (CR 106.1). choices: list of tuples of colors (one tuple = one
    way to activate, e.g. [('R',), ('G',)] for "Add {R} or {G}"); restrict/rider for special
    mana (CR 106.6)."""

    def __init__(self, choices, restrict=None, rider=None, any_color=False, amount=None):
        self.choices = choices
        self.restrict = restrict
        self.rider = rider
        self.any_color = any_color
        self.amount = amount  # Expr for "Add X mana of ..." (unsupported for now)


# ----------------------------------------------------------------------------- triggers

class Trigger:
    """Matches events. `lookback`: leaves-the-battlefield style (CR 603.10a): checked against
    the game state before the event."""
    event = None
    lookback = False

    def match(self, g, ev, src, chars, ctrl):
        """Return a dict of trigger data (truthy) if the event triggers this ability."""
        raise NotImplementedError


# ----------------------------------------------------------------------------- statics

class StaticPart:
    """One effect of a static ability. Subclasses: LayerPart, RulePart, ReplacementPart."""
    kind = "?"


class LayerPart(StaticPart):
    """A characteristic-changing effect in a layer (CR 613). affected: 'self' | 'attached' |
    Filter. fn(g, obj, chars, src) mutates chars."""
    kind = "layer"

    def __init__(self, layer, affected, fn, text=""):
        self.layer = layer
        self.affected = affected
        self.fn = fn
        self.text = text


class RulePart(StaticPart):
    """Rule-modifying effect looked up by name (e.g. 'cant_block', 'cost_mod',
    'cant_be_countered', 'max_hand_size', 'may_play_from', 'skip_untap')."""
    kind = "rule"

    def __init__(self, rule, affected="self", **data):
        self.rule = rule
        self.affected = affected
        self.data = data


class ReplacementPart(StaticPart):
    """A replacement effect (CR 614) on a kind of event. applies(g, ev, src) -> bool;
    apply(g, ev, src) modifies the event in place (or returns 'prevent')."""
    kind = "replacement"
    copy_effect = False  # "enter as a copy" (CR 616.1c: applied before the others)

    def __init__(self, event, applies, apply, self_replacement=False, text="", once_key=None):
        self.event = event
        self.applies = applies
        self.apply = apply
        self.self_replacement = self_replacement
        self.text = text


# ----------------------------------------------------------------------------- effects

class Effect:
    def run(self, g, ctx):
        raise NotImplementedError

    def __repr__(self):
        return type(self).__name__


class Seq(Effect):
    def __init__(self, *effects):
        self.effects = [e for e in effects if e is not None]

    def run(self, g, ctx):
        for e in self.effects:
            if ctx.get("stop"):
                break
            e.run(g, ctx)

    def __repr__(self):
        return "Seq(" + ", ".join(map(repr, self.effects)) + ")"


class Fn(Effect):
    """An effect written directly as Python (for hand-written card definitions)."""

    def __init__(self, fn, name="fn"):
        self.fn = fn
        self.name = name

    def run(self, g, ctx):
        self.fn(g, ctx)

    def __repr__(self):
        return f"Fn({self.name})"


class Noop(Effect):
    def run(self, g, ctx):
        pass


class UnsupportedEffect(Effect):
    def __init__(self, why):
        self.why = why

    def run(self, g, ctx):
        raise Unsupported(self.why)


# ----------------------------------------------------------------------------- targets

class TargetSpec:
    """One "target" word (CR 115.1). what: 'any' | 'creature' | 'player' | 'permanent' |
    'spell' | 'card' | 'opponent' | 'creature_or_pw' | 'object' ... filt: Filter for objects;
    zone: where objects are; n: (min, max)."""

    def __init__(self, what, filt=None, zone="battlefield", n=(1, 1), player_filter=None,
                 text="", distinct_from=None):
        self.what = what
        self.filt = filt
        self.zone = zone
        self.n = n
        self.player_filter = player_filter
        self.text = text
        self.distinct_from = distinct_from  # "another target creature"

    def __repr__(self):
        return f"Target({self.what} {self.text!r})"


def _immutable(*classes):
    """IR is immutable card data: game-state copies share it (cheap clones)."""
    for cls in classes:
        cls.__deepcopy__ = lambda self, memo: self
        cls.__copy__ = lambda self: self


_immutable(Ability, Modes, Mode, Cost, ManaProduce, Trigger, StaticPart, Effect, TargetSpec)
