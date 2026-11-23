"""Games from decklists (whole-game play, CR 103): libraries shuffled by the engine's RNG."""
import re

from .engine.objects import State, Obj
from .engine.game import Game
from .cards.model import DB


class Deck(list):
    """A main deck's card names; `sideboard`: the names listed after a "Sideboard" line (cards
    outside the game, CR 400.11a, 100.4)."""
    sideboard = ()


def parse_decklist(path_or_text):
    text = open(path_or_text).read() if "\n" not in path_or_text and not path_or_text.strip()[0].isdigit() else path_or_text
    main, side, cur = Deck(), [], None
    cur = main
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("sideboard"):
            cur = side  # the main deck ends here (was: stop reading)
            continue
        m = re.match(r"(\d+)\s+(.+)", line)
        if m:
            cur += [m.group(2).strip()] * int(m.group(1))
    main.sideboard = tuple(side)
    return main


def state_from_decks(decks, seed=0, shuffle=True):
    s = State(seed)
    counts = {}
    for p, names in enumerate(decks):
        # Cards outside the game (CR 400.11a): a decklist's sideboard. Effects that bring cards
        # in from outside the game refuse loudly when there are any (ref/cards/round2_b2.py).
        s.players[p].flags["sideboard"] = tuple(getattr(names, "sideboard", ()) or ())
        for name in names:
            DB.get(name)
            o = Obj(s.new_id(), name, p, "library")
            n = counts.get(name, 0) + 1
            counts[name] = n
            o.key = f"{name}#{n}"
            o.timestamp = s.new_ts()
            s.objs[o.id] = o
            s.players[p].library.append(o.id)
        if shuffle:
            s.rng.shuffle(s.players[p].library)  # CR 103.3
    s.turn = 1
    s.active = 0
    s.first_player = 0
    s.step = "Untap"
    s.point = ("start",)
    s.mulligan_done = False
    return s


def game_from_decks(decks, seed=0, hooks=None):
    return Game(state_from_decks(decks, seed), hooks=hooks)
