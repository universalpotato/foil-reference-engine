"""Most frequent mechanics in the pool's main decks, and how well the reference engine covers them.

    python3 -m ref.mechanics [--top 60] [--out cards/mechanics.md]

A "mechanic" is a keyword (Scryfall keyword list) or a rules template recognised by a regex over
the Oracle text (ETB triggers, mana abilities, enters tapped, ...). Frequency = main-deck copies
across the pool lists. Coverage = share of the pool cards with that mechanic whose whole text
compiles (runs/coverage.json from `python3 -m ref.coverage`), plus whether the engine implements
the mechanic itself (rules support, independent of every card that uses it).
"""
import collections
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

TEMPLATES = {
    "mana ability ({T}: Add)": r"\{T\}[^:]*: Add",
    "activated ability with {T}": r"\{T\}(,|:)",
    "ETB trigger (When ~ enters)": r"\bwhen(ever)? (this|~|[A-Z][^,.]*?) enters\b",
    "enters tapped (conditional)": r"enters tapped",
    "draw cards": r"draws? (a|two|three|\w+) cards?",
    "pay life (shock lands, costs)": r"pay \d+ life",
    "create tokens": r"\bcreate\b",
    "sacrifice": r"\bsacrifice",
    "legendary (legend rule)": None,
    "gain life": r"gain \d+ life|gains? (that much )?life",
    "+1/+1 counters": r"\+1/\+1 counter",
    "discard": r"\bdiscard",
    "destroy": r"\bdestroy\b",
    "beginning-of-step triggers": r"at the beginning of (your|each|the)",
    "mana value checks": r"mana value",
    "damage to targets": r"deals? (\d+|X|that much) damage",
    "look at top / dig": r"look at the top",
    "search library": r"search your library",
    "replacement (instead)": r"\binstead\b",
    "reveal": r"\breveal",
    "attack triggers": r"whenever .* attacks",
    "additional costs": r"as an additional cost",
    "any target": r"any target",
    "lose life": r"loses? \d+ life",
    "modal spells": r"choose (one|two|one or more|one or both|any number)",
    "counterspells": r"counter target",
    "copy effects": r"\bcopy\b",
    "hexproof / protection / ward": r"hexproof|protection from|\bward\b",
    "dies triggers": r"\bdies\b",
    "cost reduction": r"costs? \{\d\} less|costs? less",
    "pump until end of turn": r"gets? [+-]\d+/[+-]\d+ until end of turn",
    "return from graveyard": r"return .* from your graveyard",
    "loyalty abilities": r"^[+−-]?\d+:",
    "exile target": r"exile target",
    "X costs": r"\{X\}",
    "cast triggers": r"whenever you cast",
    "-1/-1 counters": r"-1/-1 counter",
    "tap / untap target": r"(tap|untap) target",
    "until ~ leaves the battlefield": r"until .* leaves the battlefield",
    "anthems (static P/T)": r"(creatures|other) .* get \+\d+/\+\d+",
    "Equipment / Auras": r"equipped creature|enchanted creature",
    "bounce": r"return target .* to (its|their) owner'?s'? hand",
    "can't block / attack": r"can't (block|attack)",
    "Sagas": r"\(As this Saga",
}

# Keywords/templates the engine implements as rules (independent of whether every card using
# them compiles).
ENGINE = {
    "Flying", "Flash", "Trample", "Vigilance", "Haste", "Lifelink", "Menace", "Deathtouch",
    "Reach", "First strike", "Double strike", "Indestructible", "Hexproof", "Defender", "Ward",
    "Prowess", "Equip", "Enchant", "Kicker", "Flashback", "Scry", "Surveil", "Mill", "Treasure",
    "Food", "Clue", "Explore", "Earthbend", "Blight", "Spree", "Fight", "Changeling",
    "Manifest dread", "Investigate", "Landfall", "Delirium", "Threshold", "Raid", "Valiant",
    "Infusion", "Converge", "Protection",
    "mana ability ({T}: Add)", "activated ability with {T}", "ETB trigger (When ~ enters)",
    "enters tapped (conditional)", "draw cards", "pay life (shock lands, costs)",
    "create tokens", "sacrifice", "legendary (legend rule)", "gain life", "+1/+1 counters",
    "discard", "destroy", "beginning-of-step triggers", "mana value checks",
    "damage to targets", "look at top / dig", "search library", "replacement (instead)",
    "attack triggers", "additional costs", "any target", "lose life", "modal spells",
    "counterspells", "copy effects", "hexproof / protection / ward", "dies triggers",
    "cost reduction", "pump until end of turn", "return from graveyard", "loyalty abilities",
    "exile target", "X costs", "cast triggers", "-1/-1 counters", "tap / untap target",
    "until ~ leaves the battlefield", "anthems (static P/T)", "Equipment / Auras", "bounce",
    "can't block / attack", "Sagas",
}


def main():
    import argparse
    from carddata import pool_names, load_oracle, main_deck_counts, faces, full_text
    from ref.cards.model import norm_text
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--out", default=os.path.join(ROOT, "cards", "mechanics.md"))
    a = ap.parse_args()
    names = pool_names()
    cards = load_oracle(names)
    copies, _ = main_deck_counts()
    cov = json.load(open(os.path.join(ROOT, "runs", "coverage.json")))
    freq = collections.Counter()
    users = collections.defaultdict(list)
    for n in names:
        c = cards[n]
        mech = set(c.get("keywords") or [])
        text = norm_text(full_text(c), n)
        tl = " ".join(f.get("type_line", "") for f in faces(c))
        for k, pat in TEMPLATES.items():
            if pat is None:
                if "Legendary" in tl:
                    mech.add(k)
            elif re.search(pat, text, re.I | re.M):
                mech.add(k)
        for m in mech:
            freq[m] += copies.get(n, 0)
            users[m].append(n)
    rows = []
    for m, f in freq.most_common(a.top):
        us = users[m]
        ok = [u for u in us if cov.get(u, {}).get("supported")]
        okw = sum(copies.get(u, 0) for u in ok)
        rows.append((m, f, len(us), len(ok), 100 * okw / max(1, f), m in ENGINE))
    impl = sum(1 for r in rows if r[5])
    lines = ["# Most frequent mechanics in the pool's main decks", "",
             "Generated by `python3 -m ref.mechanics`. *copies* = main-deck copies of cards with the "
             "mechanic across the pool lists; *cards ok* = pool cards with it whose whole Oracle text "
             "compiles; *engine* = the rules engine implements the mechanic itself.", "",
             f"Top {len(rows)} mechanics: engine implements **{impl}**.", "",
             "| # | mechanic | copies | cards | cards ok | copies ok % | engine |", "|---:|---|---:|---:|---:|---:|:---:|"]
    for i, (m, f, nu, nok, pct, e) in enumerate(rows, 1):
        lines.append(f"| {i} | {m} | {f} | {nu} | {nok} | {pct:.0f}% | {'yes' if e else 'no'} |")
    open(a.out, "w").write("\n".join(lines) + "\n")
    print(f"top {len(rows)}: engine implements {impl}")


if __name__ == "__main__":
    main()
