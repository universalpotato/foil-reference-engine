# Foil reference engine

A second rules engine for Magic: The Gathering (Standard), written in Python and used for differential testing of the Foil engine.

Main engine: https://github.com/universalpotato/foil-engine

It was written from the Comprehensive Rules and Oracle card text without reading the main engine's source, so that disagreements between the two point at bugs.

## Layout

- `ref/`: the engine (Python 3.12, no dependencies), its Oracle-text card compiler, the scenario runner and JSON-lines service.
- `interfaces/`: the scenario, state and option formats.
- `cards/`: the card scope, coverage and mechanics reports.

## Quick start

    # save the Comprehensive Rules of 2026-09-25 (text, UTF-8) as cr/MagicCompRules-20260925.txt
    python3 -m ref.service run SCENARIO.json         # one scenario/v0 file, see interfaces/
    python3 -m ref.service serve                     # JSON-lines service on stdin/stdout

## Notes

Code and docs call the main engine "the EUT" (engine under test). `ref.coverage`, `ref.mechanics` and the self-play part of `ref.invariants` need deck pool data externally.
