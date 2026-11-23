# Shared scenario and canonical-state formats (v0, owned by mtg-ref)

Both engines must speak these formats for differential testing:
- the reference engine (`ref/`) natively;
- the engine under test (EUT) via new `mtg-cli serve` commands that the EUT team will add (§6,
  collected in `eut-requests.md`).

The format is versioned (`"format": "scenario/v0"`). Breaking changes are logged in
`CHANGES.md` in this directory.

## 1. Scenario: a fully specified starting position

```json
{
  "format": "scenario/v0",
  "name": "exile-until-host-already-left",
  "rules": ["CR 610.3a", "CR 610.3b", "CR 603.10a"],
  "rulings": [{"card": "Solitary Cell", "date": "2024-11-08", "text": "..."}],
  "why": "1-3 sentences: the reasoning from the rule text to the expected outcome.",
  "status": "draft",
  "needs": ["scenario"],
  "seed": 1,
  "turn": 3, "active": 0, "step": "Main1", "priority": 0,
  "players": [
    {"life": 20, "poison": 0,
     "library": ["Island", "Plains"],
     "hand": [{"name": "Solitary Cell", "key": "cell"}],
     "graveyard": [], "exile": [],
     "mana_pool": [], "lands_played_this_turn": 0},
    {"life": 20, "library": ["Swamp"], "hand": [], "graveyard": [], "exile": []}
  ],
  "battlefield": [
    {"name": "Pinnacle Starcage", "controller": 1, "key": "starcage"},
    {"name": "Mountain", "controller": 0, "key": "m1"},
    {"name": "Plains", "controller": 0, "key": "p1"}
  ],
  "stack": [],
  "choices": ["cast:key:cell", "pass", "pass"],
  "expect": [ {"after": "end", "state": {"zones": {"key:cell": "battlefield"}}, "terminates": true} ]
}
```

Corpus bookkeeping fields (such as `ref`: the reference engine's result when it disagrees) are
not part of the scenario; runners strip them before `scenario`, and an engine may refuse them as unknown fields.

### 1.1 Position
- `turn` (≥ 1), `active` (0/1), `step` and `priority` (the player who has priority now).
  Step names: `Untap Upkeep Draw Main1 BeginCombat DeclareAttackers DeclareBlockers
  FirstStrikeDamage CombatDamage EndCombat Main2 End Cleanup`.
- `"phase_point"`: `"priority"` (default), `"start"`, or `"mulligan"` (before the game: each
  player draws seven cards from the top of the given library, then mulligan decisions; CR 103.5
  bottoms are part of taking each mulligan).
  - `"priority"`: the step's turn-based actions have already happened (the draw was drawn,
    attackers in `attacking` fields are declared) and player `priority` has priority with the
    given stack. Nobody has passed yet.
  - `"start"`: the game starts at the beginning of `step`, *before* its turn-based actions
    (CR 703.4): e.g. `"step": "DeclareAttackers", "phase_point": "start"` means the active player
    is about to declare attackers; `"step": "Upkeep"` means "beginning of upkeep" triggers
    trigger.
- `turn: 1` with `active: 0` means the starting player's first turn (no draw, CR 103.8a).
  Use `turn ≥ 2` unless the test is about that.
- "This turn" history the scenario can set: `lands_played_this_turn` per player,
  `spells_cast_this_turn` (list of names) per player, `life_lost_this_turn` / `life_gained_this_turn`
  per player, `"creatures_died_this_turn"` (global count), `"descended_this_turn"`.
  Anything else must be reached by playing moves in `choices`.
- v2: `"died_this_turn"`: list of the creatures that died this turn as they last
  existed on the battlefield (CR 608.2h), each `{"name", "owner", "controller", "types",
  "subtypes", "colors", "power", "toughness", "token"}`; it implies `creatures_died_this_turn`
  (its length). For conditions that look at what died ("if a creature with power 4 or greater
  died this turn", "a Rabbit you controlled died").

### 1.2 Players
`life` (default 20), `poison` (0), `library` (top first; **explicit order, no shuffle** unless a
card shuffles), `hand`, `graveyard` (bottom first, i.e. the order cards were put there),
`exile`, `mana_pool` (list of mana symbols such as `"R"`, `"C"`), `lands_played_this_turn`
(0), `counters` (player counters: `{"energy": 2}`), `speed` (0), `mulligans` (0).

A card entry is either a name string or an object `{"name": ..., "key": ..., "face_down":
false, "exiled_with": "<key>", "counters": {...}}`. Give a `key` to anything a choice or an
expectation refers to.

A face-down `exile` entry may say who may look at the card: `"may_look": [0]` (a list of player
indexes; `[]` = nobody). CR 406.3: a card "exiled face down" can't be examined "except when
instructions allow it", and that depends on what exiled it: foretell lets its owner look (702.143a),
hideaway the controller of the exiling permanent (702.75a), some effects nobody. Without
`may_look`, the reference engine's knowledge model assumes the owner and the controller of the
permanent it is `exiled_with` may look (the stronger knowledge). `may_look` is only for face-down
exile entries (anything else is an error). It doesn't change the rules of the game, only who knows
the card (input-fuzzing checks); an engine that loads face-down exile honours it (EUT team,
2026-11-08: once it loads those positions; 6851b5b7 refuses face-down exile entries, and the field).

### 1.3 Permanents (`battlefield`, in timestamp order: earlier entries are older)
| field | default | meaning |
|---|---|---|
| `name` | required | Oracle name; double-faced cards `"Front // Back"` |
| `key` | none | stable handle (see §2) |
| `controller` | required | 0/1 |
| `owner` | controller | 0/1 |
| `tapped` | false | |
| `summoning_sick` | false | true = has not been under its controller's control continuously since their most recent turn began (CR 302.6) |
| `counters` | `{}` | `{"+1/+1": 2, "-1/-1": 0, "loyalty": 3, "stun": 1, "lore": 1, ...}`. A planeswalker without `loyalty` gets its printed loyalty. |
| `damage` | 0 | marked damage |
| `attached_to` | null | key of the permanent (or `"p0"`/`"p1"`) it's attached to |
| `face` | 0 | 1 = transformed / back face up (CR 712) |
| `face_down` | false | face-down 2/2 (CR 708); `"face_down_as"`: `morph`, `megamorph`, `disguise`, `manifest`, `manifest_dread`, `cloak` (default `morph`). It fixes the face-down characteristics (disguise and cloak: ward {2}, CR 702.168a, 701.58a) and which `special:<ref>:turn-face-up` costs exist; the card entry's `name` is the face-up card |
| `token` | false | a token: a predefined token by name (`Treasure`, `Food`, `Clue`, `Map`, `Blood`), a token copy of a real card (its name), or any token described by `token_def` |
| `token_def` | none | `{"types": ["Creature"], "subtypes": ["Rabbit"], "colors": ["W"], "power": 1, "toughness": 1, "text": ""}` (CR 111.4); `name` is then the token's name, e.g. `"Rabbit Token"`. `text` is Oracle-style rules text with `~` or "this token" for itself; an engine that can't compile a given text refuses the scenario (`ok:false`), and tests keep to keyword lines and texts that pool cards' tokens have |
| `chosen` | `{}` | `{"type": "Elf", "name": "Opt", "color": "R", "mode": 1}` choices made as it entered; `mode` is the 1-based number of the chosen mode in Oracle order (as in `mode:<n>`) |
| `attacking` | null | in combat steps: `"p1"` or a planeswalker key |
| `blocking` | null | list of attacker keys it blocks |
| `exiled_cards` | `[]` | keys of cards in exile linked to it ("exiled with", CR 607, 610.3) |

`stack`: list, **bottom first** (so the last entry resolves first), of
`{"kind": "spell", "name": ..., "controller": p, "key": ..., "targets": ["key:x","p1"], "x": 0,
"mode": [1]}` or `{"kind": "triggered"|"activated", "source": "key:x", "ability": n, "controller":
p, "targets": [...]}` (`ability` = 1-based index of that kind of ability on the source, Oracle
order). A target of a preset entry is a ref (§2): a spell on the stack by its `key:`, an earlier
entry that is an activated or triggered ability by `"stack:N"` (N = its 1-based position in this
list, from the bottom).

### 1.4 Determinism
`seed` drives only the engine's own RNG (shuffles, coin flips, random discards). Library order is
explicit. Differential comparison treats library order as hidden after any shuffle (compares the
multiset) until a shared shuffle function is specified here.

## 2. References
- Players: `p0`, `p1`.
- Objects: `key:<k>`. A key names a *card* (or a token) of the scenario and follows it across
  zone changes, although the object it refers to is a new object after each zone change
  (CR 400.7). For a token, the key is gone once the token leaves the battlefield (CR 704.5d,
  111.7–8): its zone then reads `"none"`.
- `name:<Card Name>`: the only object with that name (it's an error in the test if more than
  one object matches in the zones searched: battlefield, stack, hands, graveyards, exile).
- Newly created objects get engine keys `new:<Name>:<n>` (n = 1, 2, … in creation order) and are
  referred to as `new:<Name>:<n>` (without `key:`). Tests should prefer `count` (§4).
- Token names follow CR 111.4: unless the effect names it, a token's name is its subtypes plus
  "Token" ("Treasure Token", "Zombie Token"). A `count` filter `"name": "Treasure"` matches
  "Treasure Token" too.
- Abilities on the stack: `stack:N`, an activated or triggered ability on the stack, N = its
  1-based position from the **bottom** of the stack (state/v0 lists the stack top first; a spell
  being cast is on top and doesn't change N). An ability is an object of its own (CR 113.7a,
  115.1): two abilities of one source, or a spell and its own cast trigger, are different refs,
  and targeting an ability doesn't target its source (no ward, CR 702.21a). `stack:gone` once
  the ability has left the stack (e.g. in a `targets` list of state/v0). Spells on the stack are
  objects with keys (`key:`/`new:` refs).

## 3. Choices and canonical options
Every pending decision has a `kind` (an open enum) and a list of options. Each option has a
**canonical string**, identical in both engines; the scenario `choices` list is a list of
canonical strings (and macros, §3.3). Options are matched by meaning, never by index.

**Forced decisions are auto-submitted** in both engines: a decision with exactly one legal
option never appears in a choice list or in `after` counts. (A "Done"-only set choice and a
priority decision whose only option is `pass` are forced.) Note that `pass` with other
options available is not forced.

**Mana abilities are not Priority options by default** (CR 605.3a allows activating them any
time a player has priority, but that only matters for rare cards; offering them would make
almost every priority decision unforced). A scenario that needs them sets
`"rules_options": {"mana_abilities_at_priority": true}`; then `mana:<ref>:<n>` options appear.
Mana abilities are always available while paying (`PayMana`).

**Canonical pick order.** Set choices (mana sources within one payment, modes, and the
engine-specific orders of discards/sacrifices) are made one pick at a time in a fixed order:
after a pick, only options later in that order are offered, so every set is reachable by exactly
one sequence and a set with a single possible completion is forced. Options are offered only if
the choice can still be completed (e.g. a mana source whose use leaves the rest of the cost
payable, a target that leaves the remaining target words fillable, X no larger than the number of
legal targets for "X target …").

**Identical objects are offered once** (options carry interchangeable alternatives; naming any of
them is accepted). Objects are identical when everything that can matter is equal: name,
controller, tapped, counters, damage, attachments, combat role, chosen values, linked/exiled-with
cards, characteristics, and summoning sickness *where it can matter* (a creature without haste
controlled by the active player, or with a {T}/{Q} ability).

### 3.1 Option strings
| kind | options |
|---|---|
| `Mulligan` | `keep`, `mulligan` |
| `BottomCard` (London mulligan, CR 103.5) | `choose:<ref>` (one at a time) |
| `Priority` | `pass`; `play:<ref>` (land); `cast:<ref>`; `cast:<ref>@<face name>` (adventure/omen/MDFC back/split half, Oracle face name); `cast:<ref>#<method>` (alternative way: `flashback` (a card with more than one flashback ability, its own and one an effect granted (Stingcaster Mage, Flashback), whose costs differ: `cast:<ref>#flashback:<n>` casts it with its n-th one, printed first, then granted ones in timestamp order; `#flashback` is the first; an instance with the same cost as an earlier one is that option (CR 702.34a, 118.9, 601.2b; Flashback ruling 2026-03-20)), `warp`, `evoke`, `impending`, `harmonize`, `plot`, `prepared`, `free`, `web-slinging`, `airbend` (an airbent card its owner casts for {2}, CR 701.65a; an airbent adventurer or omen card cast as its Adventure / Omen for {2}: `cast:<ref>@<face name>#airbend`, CR 715.3, 720.3), `sneak` (CR 702.190a: in its controller's declare blockers step, returning an unblocked creature, a `PayCost` `choose:<ref>` before the mana)); `activate:<ref>:<n>` (n = 1-based index among the object's **non-mana activated abilities**, Oracle order, then granted ones in timestamp order; loyalty abilities count. The count covers every activated ability of the card whatever zone it works from, so cycling, channel or unearth keep the same number in hand, graveyard or on the battlefield; only those that work in the object's current zone are offered); `mana:<ref>:<n>` (activate its n-th mana ability at priority, CR 605.3a; two identical mana abilities of one object, same text and same mana, are one option with the other as its alternative; a mana ability that couldn't add mana now, CR 106.7, isn't one, 605.1a, and isn't offered); `special:<ref>:<what>` (special actions, CR 116: `plot`, `turn-face-up`, `foretell`, `unlock-left` / `unlock-right` (pay a Room's locked half's mana cost to unlock it, CR 709.5e)) |
| `ChooseMode` | `mode:<n>` (1-based, Oracle order), `done` |
| `ChooseX` | `x:<n>` |
| `AdditionalCost` / `OptionalCost` | optional cost: `cost:<keyword>` (`kicker`, `bargain`, `gift`, `blight`) or `no`; an optional {0} alternative to an activated ability's cost (Kíli the Resourceful's first equip each turn, CR 118.9, 602.2b): `cost:free` or `no`; a choice between alternatives ("discard a card or pay 3 life"): `cost:<kind>` with kind `discard`, `life`, `sacrifice`, `mana`, `blight`, `exile` (CR 601.2b: announced before targets) |
| `Target` | `target:<ref>` (players, permanents, cards, the `key:` of a spell on the stack, `stack:N` of an activated or triggered ability on the stack: one option per ability, §2), `done` |
| division of a spell's or ability's effect among its targets (CR 601.2d, "distribute", "divided as you choose") | asked as `ChooseX`, right after the targets: `x:<n>` = the amount for the next target in the order the targets were chosen, every target but the last (which gets the rest); each target gets at least 1; interchangeable targets (identical objects) are divided non-increasingly in that order (both engines, EUT 54ee932f) |
| `PayMana` | `mana:<ref>:<color>` (use that source, producing `<color>` ∈ `W U B R G C`; a source that adds several mana of one color, like {C}{C}, uses that one letter; a source with several {T} mana abilities of one color in different amounts, like Tablet of Discovery's {R} and instant/sorcery-only {R}{R}, is one option: the largest amount this payment may spend, unrestricted on a tie, the rest staying in the pool, CR 106.4, 601.2h; both engines), `pool:<color>` (spend one mana from the pool) |
| `PayCost` (non-mana costs: sacrifice, discard, exile, tap creatures, …) | `choose:<ref>`, `done`, `yes`, `no`; web-slinging's returned creature (CR 702.188a) may also be an untapped creature with a {T}-only mana ability, tapped for that mana right away: `mana:<ref>:<color>` (Spider-Sense ruling 2025-09-19); craft's materials (CR 702.167a) one at a time after the mana, `done` once the minimum of "N or more" is chosen; station's tapped creature (702.184a) |
| `YesNo` / `OptionalEffect` / `PayUnless` | `yes`, `no` |
| `DeclareAttackers` | `attack:<ref>-><p0\|p1\|ref>` (ref = planeswalker or battle), `done` |
| `DeclareBlockers` | `block:<ref>-><ref>` (blocker → attacker), `done` |
| `AssignDamage` | `assign:<ref>:<n>`: n damage to that recipient. Recipients are asked one at a time (blockers in the order they were declared, then the player/planeswalker for trample); the last recipient gets the rest without a decision. Engines may ask in different orders; compare the resulting division. |
| `ChooseReplacement` | `choose:<ref of the effect's source>` (CR 616.1: the affected player or controller chooses which replacement effect applies first) |
| `OrderTriggers` | `trigger:<ref>:<n>` (put the n-th triggered ability of source ref on the stack next. Numbering: the object's triggered abilities as it has them, printed ones in Oracle order, then granted ones in timestamp order; a reflexive "When you do" trigger (CR 603.12) and a delayed triggered ability (603.7, e.g. earthbend's "When that land dies or is put into exile, return it", 701.66a) are numbered after all of those: the source's own triggered abilities + 1, the source as it is on the battlefield or as it last existed there; an emblem's trigger has no source object: `trigger:delayed:<n>`). Two instances of one ability of one source: `trigger:<ref>:<n>@<event ref>`, `/k` where those still agree (see *OrderTriggers occurrence suffix* below) |
| `Scry` | `top`, `bottom` (for the subject card) |
| `Surveil` | `top`, `graveyard` |
| `Discard`, `Sacrifice`, `ChooseCard`, `Search`, `Dig`, `CopyChoice`, `Exile`, `ReturnCard` | `choose:<ref>`, `done` |
| `RemoveCounters` (which kind of counter to remove, CR 608.2d: "remove a counter from ~", a cost of counters of any kind) | `counter:<kind>` (kind as in state/v0 `counters`, e.g. `+1/+1`, `hexproof`), `done` for "up to N" |
| `ChooseType` | `type:<Subtype>` |
| `NameCard` | `name:<Card Name>` |
| `ChooseColor` | `color:<W\|U\|B\|R\|G>` |
| `ChooseNumber` | `number:<n>` |
| `ChoosePlayer` | `player:<p0\|p1>` |

**OrderTriggers occurrence suffix** (agreed with the EUT team 2026-11-08; EUT
`scenario::disambiguate_triggers`). Two instances of one triggered ability of one source that
trigger at the same time are interchangeable only when their events agree: the same event object
(identical objects count as the same), the same amount and the same player; otherwise their
controller orders them (CR 603.3b: two Enduring Tenacity triggers for 2 and 3 life, Enduring
Curiosity's draws for two attackers are separate options). Their option strings:
- **Scope.** Only where two options of the pending `OrderTriggers` decision would share
  `trigger:<ref>:<n>`; everywhere else the plain string, as before.
- **Event ref.** Each such option becomes `trigger:<ref>:<n>@<event ref>`, the ref (`key:`, `new:`,
  `name:` forms, §2) of the object its trigger event names: for "deals damage" triggers the damage
  source (Enduring Curiosity: the creature that dealt the combat damage); for enters and leaves
  triggers the object that moved; for cast triggers the spell; for life gain the source of the
  gain (a lifelink creature for its damage). Other events name the object they are about (the
  attacking or blocking creature, the permanent that got counters or became tapped, the discarded
  card); an event about a player only (life loss, a draw: the drawn card is hidden) names none,
  and then there is no `@`.
- **Fallback.** Options that still share a string (the same event object, or none) each get
  `/k`, every member of the group including `/1`: k is the option's 1-based rank by amount
  ascending, then by the order the events happened (CR 608.2c). Example: `trigger:key:et:1@key:src/1`
  and `trigger:key:et:1@key:src/2`.
- **Plain string.** `trigger:<ref>:<n>` stays an alternative of the first such instance in option
  order, so existing scripts keep working (it names the first of them).
- Alternatives of an option (identical instances) carry their own event ref and the option's `/k`.
- **Runners.** Scripts written before the suffix had no choice for such instances (they were one
  option, so the decision was forced); scripts now order them, with the plain string where the
  order can't matter. A runner treats a scripted plain `trigger:<ref>:<n>` as answered when the
  engine has no `OrderTriggers` decision pending; a suffixed string is never skipped.

Example: Enduring Curiosity (`key:ec`) with Grizzly Bears (`key:b1`) and Hinterland Sanctifier
(`key:hs`) dealing combat damage to a player: `trigger:key:ec:1@key:b1` (alternative
`trigger:key:ec:1`) and `trigger:key:ec:1@key:hs`; once one is on the stack the other is forced.

**Trigger identity** (shared by both engines since 2026-11-08). Which simultaneous triggers are one option:
- Triggers are interchangeable (one option, the others its alternatives) only when they are the
  same ability of the same source or of identical sources (§3 identical objects), and their
  events agree: the same event object or identical ones, or none, and the same amount and player.
- What makes two objects not identical here includes the stack (CR 603.3b): an object that is the
  source of an ability on the stack, the target of a spell or ability on the stack, or the object
  the event of a triggered ability on the stack names (its event ref, or the object it moved or
  affected) differs from an identical object that isn't. That holds **whether or not that
  ability refers to its event object**: Impact Tremors' "deals 1 damage to each opponent" doesn't,
  but once its trigger for one of two identical Soldiers is on the stack, the triggers about that
  Soldier and about the other are separate options. (The reference engine counted such a link
  only when the ability referred to its event, `refers_to_event`, until 2026-11-08.) Ordering
  interchangeable triggers is a choice without consequence, so both conventions are legal; this
  one needs no analysis of what an ability reads.
- The triggers being ordered don't count (none of them is on the stack yet): two identical
  Hinterland Sanctifiers entering together trigger for each other symmetrically, one option.
- Example: Impact Tremors (`key:it`) and Hinterland Sanctifier (`key:hs`), Raise the Alarm makes
  two Soldiers. First stop `trigger:key:it:1`, `trigger:key:hs:1`; after `trigger:key:it:1`:
  `trigger:key:hs:1@new:Soldier Token:1` (alternative `trigger:key:hs:1`), `trigger:key:it:1`,
  `trigger:key:hs:1@new:Soldier Token:2` (test `603-triggers/impact-tremors-trigger-on-the-stack-tells-two-identical-soldiers-apart`).

**Optional choices** ("you may put a card from among them into your hand", "you may search",
CR 608.2d). A "may" choice of a card is asked either as one decision whose `done` declines (a
`Search`: Rapid Rescue, Something Worth Saving and Bristlebud Farmer in the reference engine; the
EUT for Rapid Rescue and Something Worth Saving), or as a yes/no and
then the card (the EUT's Bristlebud Farmer). Outcomes are the same. Runners accept a script
written for either: at a `Search` that offers `done` but no `yes`/`no`, a scripted `yes` is
answered by the pick that follows it and a scripted `no` is `done`. (A script for both engines
scripts the yes/no; a decision expectation placed after it holds in both.)

Set choices (attackers, blockers, discards, sacrifices, …) are sequences of single picks ending
with `done`. The EUT offers picks in a fixed canonical order (after picking k only later options
remain); the reference engine offers every remaining pick. Comparison is on the set of options
offered at the first pick and on the final set (§5).

### 3.2 Mana payment in scenarios
`"auto": {"mana": true}` (the default) lets the runner answer every `PayMana` decision: it pays
with the first source in its own canonical order that leaves the cost payable (a test that
cares which lands are tapped sets `"mana": false` and scripts `mana:` choices).

### 3.3 Macros (runner-level, not engine options)
- `pass-until:<Step>`: answer `pass` to Priority decisions until the game is in `<Step>`
  (stops at the first decision there). A non-Priority decision on the way is an error.
- `pass-until:<Step>@<turn>`: same, but also waiting for that turn number.
- `resolve`: both players pass in turn until the top object of the stack has left it (stops at
  the next decision after that).

## 4. Expectations (edge-case tests)
`expect` is a list of checkpoints (a single object is also accepted):
```json
"expect": [
  {"after": 2,
   "decision": {"player": 0, "kind": "DeclareAttackers",
                "options_include": ["attack:key:beaver1->p1"],
                "options_exclude": ["attack:key:beaver2->p1"]},
   "state": {"zones": {"key:cell": "battlefield"}, "life": [20, 17],
             "counters": {"key:beaver2": {"stun": 1}}}},
  {"after": "end", "terminates": true}
]
```
- `after`: N = at the pending decision reached after applying the first N choices (after
  macros are expanded, N counts *entries of the choices list*); `0` = the initial position (after
  the engine has advanced to its first decision); `"end"` = after all choices. If the game ends,
  the checkpoint is evaluated on the final state.
- `decision`: `player`, `kind`, `options_include`, `options_exclude`, `options_exact` (canonical
  strings, order-insensitive).
- `terminates` (default true): the engine must reach a decision or end the game (no infinite
  loop; the runner caps engine steps).
- `error`: a scripted choice is expected to be **illegal** (`{"choice": 3}` = the 4th entry
  must be rejected). Used for "can't" tests.
- `state` keys (all optional, every listed key must match):
  | key | form |
  |---|---|
  | `zones` | `{ref: "battlefield"\|"hand"\|"library"\|"graveyard"\|"exile"\|"stack"\|"none"}` |
  | `life`, `poison`, `speed` | `[p0, p1]` (`null` = don't check; speed: 0 for a player without speed, CR 702.179f) |
  | `counters` | `{ref: {"+1/+1": 1, ...}}` (absent kinds = 0) |
  | `player_counters` | `{"p0": {"energy": 1}}` |
  | `pt` | `{ref: [power, toughness]}` |
  | `tapped`, `damage`, `controller`, `attached_to`, `face_down` | `{ref: value}` |
  | `keywords_include` / `keywords_exclude` | `{ref: ["flying", ...]}` (lowercase) |
  | `types_include` / `types_exclude` | `{ref: ["Creature", "Elf", ...]}` |
  | `colors` | `{ref: ["R"]}` (exact) |
  | `count` | `[{"zone": "battlefield", "controller": 0, "name": "Treasure", "token": true, "n": 2}]` (filter fields optional) |
  | `hand_count`, `library_count`, `graveyard_count`, `exile_count` | `[p0, p1]` |
  | `stack` | list top first of `{"source": ref, "kind": "spell"\|"activated"\|"triggered"}`; `[]` = empty |
  | `game_over` | `false`, or `{"winner": 0\|1}` or `{"draw": true}` |
  | `step`, `turn`, `active` | value |
  | `mana_pool` | `[[...], [...]]` (multiset of symbols) |

## 5. Canonical state (`state/v0`) and step comparison
```json
{"format": "state/v0",
 "turn": 3, "active": 0, "step": "Main1", "priority": 0,
 "players": [{"life": 20, "poison": 0, "hand": ["Island"], "library_count": 1,
              "graveyard": [], "exile": [], "mana_pool": [], "counters": {}, "speed": 0}, {...}],
 "objects": [ {"zone": "battlefield", "name": "Pinnacle Starcage", "controller": 1, "owner": 1,
               "tapped": false, "sick": false, "damage": 0, "counters": {}, "power": null,
               "toughness": null, "types": ["Artifact"], "subtypes": [], "keywords": [],
               "attached_to": null, "face_down": false, "token": false, "exiled_with": null,
               "attacking": null, "blocking": [], "key": "starcage"} ],
 "stack": [ {"source": "key:cell", "kind": "triggered", "controller": 0, "targets": ["key:x"]} ],
 "pending": {"player": 0, "kind": "Priority",
             "options": ["cast:key:cell", "pass"]}}
```
- `objects` covers the battlefield, spells on the stack (zone `"stack"`; abilities appear only in
  `stack`), exile and graveyards (hands and libraries are listed per player by name; library
  only by count). Sorted by (zone, controller, name, then the remaining fields
  as JSON) so two engines' outputs diff directly. Each engine maps internal ids to scenario keys,
  or to `new:<Name>:<n>` keys for objects created during the game.
- Keywords are lowercase CR 702 names; types/subtypes as printed (CR 205).
- The comparison ignores hidden library order and engine-internal ids. It does not ignore
  timestamps where they decide outcomes: it compares the outcome (P/T, controller), not the
  timestamp value.
- Set choices: compare the final chosen set. Each engine has its own fixed pick order (§3), so the
  options of the first pick, and of any intermediate pick, can differ between engines (an option
  is withheld when no later option could complete the set, e.g. a menace blocker that sorts last).
  Tests therefore assert intermediate options only where every order agrees (`done`, options that
  are illegal in any set, options that end up forced). Runners submit a scripted block of picks for
  one set in the order the engine offers them, and treat a scripted `done` as answered when the
  engine completed the set itself.
- Mana payment (`PayMana`): compare whether the cost is payable; the options themselves are
  compared as sets of sources, not colors (sources are interchangeable within a payment).

## 6. Commands the EUT team will add to `mtg-cli serve`
| Request | Response |
|---|---|
| `{"cmd":"scenario","scenario":{...}}` | Load the position. `ok:false` with a clear reason for anything unsupported, never a silent approximation. |
| `{"cmd":"state"}` | `{"ok": true, "state": <state/v0>}`: god-view canonical state (testing only; never given to agents). |
| `{"cmd":"options"}` | `{"ok": true, "decision": {"player": 0, "kind": "Priority", "options": [{"i": 0, "canon": "pass", "alts": []}, ...]}, "game_over": false}`: the pending decision, canonical strings next to the EUT's indices; `alts` are interchangeable options merged into that one; `decision: null` once the game is over. |
| `{"cmd":"submit_canon","canon":"attack:key:beaver1->p1"}` | Submit by canonical string; `canon` or any of its `alts` is accepted. |

`state`, `options` and `submit_canon` work inside a scenario session (after `scenario`) and
answer `ok:false` otherwise. Decision kinds use the names of §3.1 (e.g. an optional kicker is
`OptionalCost`, the legend rule choice `ChooseCard`). With `auto.mana` (the default) the EUT may
pay with its own first offered source (§3.2); the runner never submits `mana:` choices then.
