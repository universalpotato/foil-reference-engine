"""Game flow: start of game and mulligans (CR 103), turn structure (500-514), priority (117),
state-based actions (704)."""
from .decisions import Decision, Opt, EngineLoop
from .objects import Combat
from .. import Unsupported

STEPS = ["Untap", "Upkeep", "Draw", "Main1", "BeginCombat", "DeclareAttackers",
         "DeclareBlockers", "FirstStrikeDamage", "CombatDamage", "EndCombat", "Main2", "End",
         "Cleanup"]
COMBAT_STEPS = {"BeginCombat", "DeclareAttackers", "DeclareBlockers", "FirstStrikeDamage",
                "CombatDamage", "EndCombat"}

_REFERS_TO_EVENT = {}


def refers_to_event(ab):
    """Whether a triggered ability's effect, targets or condition may refer to its triggering
    event ("it", "that creature", "that much"): a selector of the event's object, or any
    compiled step that reads the event. Conservative: unknown structure counts as yes."""
    if ab is None:
        return True
    hit = _REFERS_TO_EVENT.get(id(ab))
    if hit is not None and hit[0] is ab:
        return hit[1]
    import types
    from .effects import Sel
    seen, todo, res = set(), [getattr(ab, k, None) for k in ("effect", "targets", "condition", "modes")], False
    while todo and not res:
        x = todo.pop()
        if id(x) in seen or x is None or isinstance(x, (str, int, float, bool)):
            continue
        seen.add(id(x))
        if len(seen) > 20000:
            res = True
        elif isinstance(x, Sel):
            res = (x.text or "").startswith("event")
            todo.append(x.fn)
        elif isinstance(x, types.FunctionType):
            codes = [x.__code__]
            while codes and not res:
                c = codes.pop()
                res = "event" in c.co_consts
                codes += [k for k in c.co_consts if isinstance(k, types.CodeType)]
            for cell in x.__closure__ or ():
                try:
                    todo.append(cell.cell_contents)
                except ValueError:
                    pass
            todo.extend(x.__defaults__ or ())
        elif isinstance(x, types.MethodType):
            todo += [x.__func__, x.__self__]
        elif isinstance(x, (list, tuple, set, frozenset)):
            todo.extend(x)
        elif isinstance(x, dict):
            todo.extend(x.values())
        elif type(x).__module__.startswith("ref.") and type(x).__module__ != "ref.cards.model":
            todo.extend(vars(x).values() if hasattr(x, "__dict__") else
                        (getattr(x, k, None) for k in getattr(type(x), "__slots__", ())))
    _REFERS_TO_EVENT[id(ab)] = (ab, res)
    return res


class TurnMixin:
    step_cap = 20000

    def run(self):
        s = self.s
        while s.result is None:
            self.tick()
            pt = s.point[0]
            if pt == "start":
                self.start_game()
            elif pt == "mulligan":
                self.mulligan_round()
            elif pt == "begin_game":
                self.begin_game_actions()
            elif pt == "step":
                self.begin_step()
            elif pt == "priority":
                self.priority()
            elif pt == "end_step":
                self.end_of_step()
            else:
                raise Unsupported(f"resume point {s.point}")

    def tick(self):
        self.steps_without_decision += 1
        if self.steps_without_decision > self.step_cap:
            raise EngineLoop(f"no decision after {self.step_cap} engine steps")

    # ------------------------------------------------------------------ start of game
    def start_game(self):
        """CR 103: shuffle (103.3, done by the caller for decklist games), draw 7, mulligans."""
        s = self.s
        for p in (0, 1):
            self.draw(p, 7)
        s.point = ("mulligan",)
        s.mulligan_done = False

    def mulligan_round(self):
        """CR 103.5 (London mulligan): starting player declares first, then the other; all who
        mulligan do so at the same time: shuffle hand into library, draw seven, put N on the
        bottom (N = number of mulligans taken)."""
        s = self.s
        order = [s.first_player, 1 - s.first_player]
        decl = s.turn_flags.setdefault("mull_decl", {})
        for p in order:
            pl = s.players[p]
            if pl.kept or str(p) in decl:
                continue
            if not pl.hand:
                # CR 103.5: "A player can take mulligans until their opening hand would be zero
                # cards, after which they may not take further mulligans." (Also an empty library.)
                decl[str(p)] = "keep"
                continue
            opts = [Opt("keep", "keep"), Opt("mulligan", "mulligan")]
            a = self.ask(Decision(p, "Mulligan", opts, top=True, prompt="Keep this hand or mulligan?"))
            decl[str(p)] = a
        mull = [p for p in order if decl.get(str(p)) == "mulligan"]
        for p in order:
            if decl.get(str(p)) == "keep":
                s.players[p].kept = True
        for p in mull:
            pl = s.players[p]
            self.move_batch([(oid, "library", {"pos": "bottom"}) for oid in list(pl.hand)])
            self.shuffle(p)
            pl.mulligans += 1
            self.draw(p, 7)
        for p in mull:
            pl = s.players[p]
            n = min(pl.mulligans, len(pl.hand))
            picked = []
            for _ in range(n):
                opts = [Opt(f"choose:{self.ref(o)}", o, sig=("bottom", self.obj_sig(o))) for o in pl.hand]
                o = self.ask(Decision(p, "BottomCard", opts, prompt="Put a card on the bottom of your library."))
                picked.append(self.move(o, "library", pos="bottom"))
            self._mulligan_bottom_order(pl, picked)
        s.turn_flags["mull_decl"] = {}
        if all(pl.kept for pl in s.players):
            s.mulligan_done = True
            s.turn_flags.pop("mull_decl", None)
            s.step = "Untap"
            s.point = ("begin_game",)

    def _mulligan_bottom_order(self, pl, picked):
        """The order of a mulligan's bottom cards. CR 103.5
        lets the player put them on the bottom "in any order" (401.4: the order is theirs); like the
        EUT (QV1, corrected 2026-11-08) the order is a fixed one: the chosen cards by card name
        (the EUT's canonical pick order, its card ids being name-sorted), the last deepest; cards of
        one name keep their pick order. The picks themselves are unchanged (any card first), so the
        same set gives the same library in both engines whatever order it was picked in."""
        if len(picked) < 2 or None in picked:
            return
        lib = pl.library
        k = len(picked)
        if sorted(lib[-k:]) != sorted(picked):
            return  # not all at the bottom (a replacement effect moved one elsewhere)
        lib[-k:] = sorted(picked, key=lambda x: self.s.objs[x].card.split(" // ")[0])
        self.s.touch()

    def begin_game_actions(self):
        """CR 103.6: "Once the mulligan process is complete, the starting player may take any
        such actions in any order. Then each other player in turn order may do the same." The
        actions here: "If this card is in your opening hand, you may begin the game with it on
        the battlefield" (the leylines; 103.6a). One card at a time (OptionalEffect:
        `choose:<ref>` or `done`, identical cards offered once); a player's chosen cards are put
        onto the battlefield, in the order chosen, once that player is done (no player receives
        priority in between, so this is the same game; the EUT shows them in hand until then).
        Each choice is a commit point, so the progress lives in the state."""
        s = self.s
        done = s.turn_flags.setdefault("begin_game_done", [])
        chosen = s.turn_flags.setdefault("begin_game_chosen", {})
        for p in (s.first_player, 1 - s.first_player):
            if p in done:
                continue
            mine = chosen.setdefault(str(p), [])
            cands = [o for o in s.players[p].hand if o not in mine and
                     any(a.kind == "keyword" and a.keyword == "begin_on_battlefield" for a in self.chars(o).abilities)]
            a = None
            if cands:
                opts = [Opt("done", None)] + [Opt(f"choose:{self.ref(o)}", o, sig=("begin", self.obj_sig(o))) for o in cands]
                a = self.ask(Decision(p, "OptionalEffect", opts, top=True,
                                      prompt="Begin the game with a card from your opening hand on the battlefield?"))
            if a is not None:
                mine.append(a)
                return  # this player's next action is asked from here
            for o in mine:
                self.move(o, "battlefield", cause="begin_game")
            done.append(p)
        s.turn_flags.pop("begin_game_done", None)
        s.turn_flags.pop("begin_game_chosen", None)
        s.point = ("step",)

    # ------------------------------------------------------------------ steps
    def begin_step(self):
        s = self.s
        st = s.step
        s.passes = 0
        s.priority = s.active
        if st == "Untap":
            self.untap_step()
            s.point = ("end_step",)
            return
        if st == "Upkeep":
            self.emit({"type": "step", "step": st, "active": s.active})
        elif st == "Draw":
            if not (s.turn == 1 and s.active == s.first_player and s.skip_draw_first):
                self.draw(s.active, 1)  # CR 504.1; 103.8a skip
            self.emit({"type": "step", "step": st, "active": s.active})
        elif st in ("Main1", "Main2"):
            if st == "Main1":
                self.saga_lore()  # CR 714.3c
            self.emit({"type": "step", "step": st, "active": s.active})
        elif st == "BeginCombat":
            s.combat = Combat()
            self.emit({"type": "step", "step": st, "active": s.active})
        elif st == "DeclareAttackers":
            if s.combat is None:
                s.combat = Combat()
            if not s.combat.declared:
                self.declare_attackers()
        elif st == "DeclareBlockers":
            if not s.combat.blockers_declared:
                self.declare_blockers()
        elif st == "FirstStrikeDamage":
            self.combat_damage(first=True)
        elif st == "CombatDamage":
            self.combat_damage(first=False)
        elif st == "EndCombat":
            self.emit({"type": "step", "step": st, "active": s.active})
        elif st == "End":
            self.emit({"type": "step", "step": st, "active": s.active})
        elif st == "Cleanup":
            self.cleanup()
            return
        s.point = ("priority",)

    def untap_step(self):
        """CR 502: phasing (unsupported), day/night (none), untap; turn-start bookkeeping."""
        s = self.s
        p = s.active
        for oid in list(s.battlefield):
            o = s.objs[oid]
            if self.ctrl(oid) == p:
                o.sick = False  # CR 302.6: controlled continuously since this turn began
        for oid in list(s.battlefield):
            o = s.objs.get(oid)
            if o is not None and self.ctrl(oid) == p and o.tapped:
                if self.rule_applies(oid, "doesnt_untap"):
                    continue
                self.untap(oid)

    def saga_lore(self):
        """CR 714.3c: "that player puts a lore counter on each Saga they control with one or more
        chapter abilities" (not one that lost them: The Wondrous Wasp's Sting on a Summon: Saga
        creature)."""
        s = self.s
        for oid in list(s.battlefield):
            c = self.chars(oid)
            if "Saga" in c.subtypes and c.controller == s.active and self.saga_final_chapter(oid):
                self.add_counters(oid, "lore", 1)

    def cleanup(self):
        """CR 514."""
        s = self.s
        p = s.active
        pl = s.players[p]
        excess = len(pl.hand) - self.max_hand_size(p)
        for _ in range(max(0, excess)):
            opts = [Opt(f"choose:{self.ref(o)}", o, sig=("discard", self.obj_sig(o))) for o in pl.hand]
            o = self.ask(Decision(p, "Discard", opts, prompt="Discard down to your maximum hand size."))
            self.discard(p, o, cause="cleanup")
        # 514.2: remove damage, end "until end of turn" effects (simultaneous).
        for oid in s.battlefield:
            s.objs[oid].damage = 0
        self.end_effects("eot")
        # A card's flashback granted "until end of
        # turn" (round2_b2.GainFlashback) ends here too, and only here (514.2): one granted after
        # this, during the cleanup step (514.3a), lasts until the next cleanup step.
        for pl_ in s.players:
            for oid in pl_.graveyard:
                o = s.objs.get(oid)
                if o is not None and "gains_flashback" in o.flags:
                    del o.flags["gains_flashback"]
        s.touch()
        acted = self.settle()
        if s.result is not None:
            return
        if acted or s.stack:
            s.turn_flags["cleanup_again"] = True
            s.point = ("priority",)  # 514.3a
        else:
            s.point = ("end_step",)

    def max_hand_size(self, p):
        n = self.s.players[p].max_hand
        for part, src in self.rule_parts("no_max_hand_size"):
            if self.ctrl(src) == p:
                return 10 ** 6
        return n

    def end_effects(self, duration):
        s = self.s
        keep = []
        for ce in s.effects:
            if ce.duration == duration:
                continue
            keep.append(ce)
        s.effects = keep
        s.touch()

    def end_of_step(self):
        s = self.s
        st = s.step
        # CR 702.189a, 500.5a: firebending mana isn't lost as steps end until end of combat (the
        # combat phase ends as its end of combat step ends); ref/cards/batch9a.py FireUnit.
        keep = st in ("DeclareAttackers", "DeclareBlockers", "FirstStrikeDamage", "CombatDamage")
        for pl in s.players:
            pl.pool = [u for u in pl.pool if keep and getattr(u, "until_eoc", False)]  # CR 106.4 (all mana empties; no mana burn)
        if st == "EndCombat":
            # CR 511.3: creatures stop being attacking/blocking.
            s.combat = None
            self.end_effects("eoc")
        if st == "Cleanup":
            if s.turn_flags.pop("cleanup_again", False):
                s.step = "Cleanup"
                s.point = ("step",)
                return
            self.next_turn()
            return
        nxt = STEPS[STEPS.index(st) + 1]
        if st == "DeclareAttackers" and s.combat is not None and not s.combat.attackers:
            nxt = "EndCombat"  # CR 508.8
        if nxt == "FirstStrikeDamage":
            if not self.any_first_strike():
                nxt = "CombatDamage"  # CR 510.4: no first-strike step
        s.step = nxt
        s.point = ("step",)

    def next_turn(self):
        s = self.s
        self.end_effects("your_next_turn_start_never")
        s.turn += 1
        s.active = 1 - s.active
        # CR 614.10: a skipped turn doesn't happen ("Instead of doing it, do nothing"); the next
        # player in turn order takes the next turn (Ral Zarek, Guest Lecturer: ref/cards/phase2_p3.py).
        for _ in range(2 * len(s.players) + len(s.extra_turns)):
            if ("skip", s.active) not in s.extra_turns:
                break
            rest = list(s.extra_turns)
            rest.remove(("skip", s.active))
            s.extra_turns = rest
            s.active = 1 - s.active
        s.step = "Untap"
        for pl in s.players:
            pl.lands_played = 0
            pl.spells_cast = []
            pl.life_lost = 0
            pl.life_gained = 0
            pl.turn_stats = {}
        s.turn_flags = {}
        self.prune_lki()
        # Effects that last "until your next turn" end as that turn begins (611.2b-ish).
        keep = []
        for ce in s.effects:
            if ce.duration == "until_your_next_turn" and ce.controller == s.active:
                continue
            keep.append(ce)
        s.effects = keep
        s.touch()
        s.point = ("step",)

    def prune_lki(self):
        """Last known information is only needed while something can still refer to the
        object (stack items, delayed triggers, effects)."""
        s = self.s
        keep = set()
        for it in s.stack:
            keep.add(it.source)
            keep.add(it.obj)
            for g in it.targets:
                keep.update(x for x in g if isinstance(x, int))
            # The objects a triggered ability's data
            # names (Leyline of Resonance's "copy that spell" after the spell was countered).
            keep.update(v for v in ((getattr(it, "ctx", None) or {}).get("data") or {}).values() if isinstance(v, int))
        for d in s.delayed:
            keep.add(d.get("source"))
            keep.update(v for v in (d.get("data") or {}).values() if isinstance(v, int))
        for ce in s.effects:
            keep.add(ce.source)
        s.lki = {k: v for k, v in s.lki.items() if k in keep}
        s.lki_obj = {k: v for k, v in s.lki_obj.items() if k in keep}
        s.lki_spell = {k: v for k, v in s.lki_spell.items() if k in keep}

    # ------------------------------------------------------------------ priority (117)
    def priority(self):
        s = self.s
        self.settle()
        if s.result is not None:
            return
        p = s.priority
        opts = self.priority_options(p)
        a = self.ask(Decision(p, "Priority", opts, top=True))
        if a == "pass":
            s.passes += 1
            if s.passes >= 2:
                s.passes = 0
                if s.stack:
                    self.resolve_top()
                    s.priority = s.active  # 117.3b
                else:
                    s.point = ("end_step",)  # 500.2
            else:
                s.priority = 1 - p
        else:
            s.passes = 0
            self.take_action(p, a)
            if s.result is None:
                s.priority = p  # 117.3c

    def settle(self):
        """CR 117.5 / 704.3: SBAs until none, then put triggers on the stack; repeat."""
        s = self.s
        acted = False
        while True:
            self.tick()
            if self.sba():
                acted = True
                if s.result is not None:
                    return acted
                continue
            self.check_state_triggers()
            if s.pending:
                self.put_triggers_on_stack()
                acted = True
                continue
            return acted

    def check_state_triggers(self):
        """CR 603.8: a state trigger triggers when its condition is true, and not again until
        that ability has left the stack."""
        s = self.s
        live = {it.ctx.get("state_key") for it in s.stack if it.ctx.get("state_key")}
        s.state_triggers_on_stack = {k for k in s.state_triggers_on_stack if k in live}
        from .triggers import PendingTrigger
        for oid in list(s.battlefield):
            c = self.chars(oid)
            for i, ab in enumerate(self._triggered(c)):
                if getattr(ab.trigger, "state", False) and ab.trigger.holds(self, oid):
                    key = (oid, i + 1)
                    if key in s.state_triggers_on_stack:
                        continue
                    s.state_triggers_on_stack.add(key)
                    s.pending.append(PendingTrigger(ab, oid, c, c.controller, {"type": "state"}, {"state_key": key}, i + 1))

    # ------------------------------------------------------------------ state-based actions
    def sba(self):
        s = self.s
        performed = False
        losers = []
        for pl in s.players:
            if pl.life <= 0 or pl.drew_from_empty or pl.poison >= 10:
                losers.append(pl.index)  # 704.5a-c
        for pl in s.players:
            pl.drew_from_empty = False
        if losers:
            if len(losers) == 2:
                s.result = {"draw": True}  # CR 104.4a
            else:
                s.result = {"winner": 1 - losers[0]}
            for p in losers:
                s.players[p].lost = True
            return True
        # 704.5d: tokens off the battlefield cease to exist; 704.5e copies of spells/cards.
        for pl in s.players:
            for zname in ("hand", "graveyard", "exile", "library"):
                z = pl.zone(zname)
                for oid in list(z):
                    o = s.objs[oid]
                    if o.token or o.is_copy:
                        z.remove(oid)
                        del s.objs[oid]
                        performed = True
                        # Characteristics that count objects in that zone change with it
                        # (Tarmogoyf and a sacrificed Treasure token in a graveyard).
                        s.touch()
        moves = []
        destroys = []
        for oid in list(s.battlefield):
            o = s.objs[oid]
            c = self.chars(oid)
            if "Creature" in c.types and c.toughness is not None:
                if c.toughness <= 0:
                    moves.append((oid, "graveyard", {"cause": "sba"}))  # 704.5f
                    continue
                if o.damage >= c.toughness or o.deathtouched:  # 704.5g-h
                    if not c.has_kw("indestructible"):
                        destroys.append(oid)
                        continue
            if "Planeswalker" in c.types and o.counters.get("loyalty", 0) <= 0 and not self.rule_applies(oid, "zero_loyalty_stays"):
                moves.append((oid, "graveyard", {"cause": "sba"}))  # 704.5i (Sanctum Lurker: an exception)
                continue
            if "Aura" in c.subtypes and not self.aura_attach_legal(oid):
                moves.append((oid, "graveyard", {"cause": "sba"}))  # 704.5m
                continue
            if "Battle" in c.types and o.counters.get("defense", 0) <= 0 and isinstance(c.defense, int):
                moves.append((oid, "graveyard", {"cause": "sba"}))  # 704.5v-w
                continue
            if ("Equipment" in c.subtypes or "Fortification" in c.subtypes) and o.attached_to is not None:
                if not self.equip_attach_legal(oid):
                    o.attached_to = None  # 704.5n
                    s.touch()
                    performed = True
            if ("Creature" in c.types or "Battle" in c.types) and o.attached_to is not None:
                o.attached_to = None  # 704.5p
                s.touch()
                performed = True
            if o.counters.get("+1/+1", 0) > 0 and o.counters.get("-1/-1", 0) > 0:
                n = min(o.counters["+1/+1"], o.counters["-1/-1"])  # 704.5q
                o.counters["+1/+1"] -= n
                o.counters["-1/-1"] -= n
                for k in ("+1/+1", "-1/-1"):
                    if o.counters[k] == 0:
                        del o.counters[k]
                s.touch()
                performed = True
            if "Saga" in c.subtypes:
                final = self.saga_final_chapter(oid)
                if final and o.counters.get("lore", 0) >= final and not any(
                        it.kind == "triggered" and it.source == oid and getattr(it.ability, "chapter", None)
                        for it in s.stack) and not any(
                        # 714.4: "triggered but not yet left the stack" includes a chapter ability
                        # waiting to be put on the stack.
                        t.source == oid and getattr(t.ability, "chapter", None) for t in s.pending):
                    moves.append((oid, "graveyard", {"cause": "sacrifice"}))  # 704.5s
        # 704.5z: of the Roles one player controls attached to one permanent, all but the newest
        # (CR 303.7a) are put into their owners' graveyards.
        roles = {}
        for oid in s.battlefield:
            o = s.objs[oid]
            if o.attached_to is None or any(m[0] == oid for m in moves):
                continue
            c = self.chars(oid)
            if "Role" in c.subtypes:
                roles.setdefault((o.attached_to, c.controller), []).append(oid)
        for ids in roles.values():
            if len(ids) > 1:
                newest = max(ids, key=lambda i: s.objs[i].timestamp)
                moves += [(i, "graveyard", {"cause": "sba"}) for i in ids if i != newest]
        # 704.5j legend rule.
        # : "the same name" is at least one name in common
        # (CR 201.2a): a split permanent has both halves' names (709.4a; a Room with both doors
        # unlocked, 709.5), so a fully unlocked legendary Room token and one with only its left
        # door unlocked share that door's name. Grouped per name; a permanent already put into
        # the graveyard for one name isn't counted again for another.
        groups = {}
        for oid in s.battlefield:
            c = self.chars(oid)
            if "Legendary" in c.supertypes and c.name:
                if any(m[0] == oid for m in moves) or oid in destroys:
                    continue
                for nm in c.name.split(" // "):
                    groups.setdefault((c.controller, nm), []).append(oid)
        gone = set()
        for (p, name), ids in sorted(groups.items(), key=lambda kv: (kv[0][0] != s.active, kv[0])):
            ids = [i for i in ids if i not in gone]
            if len(ids) < 2:
                continue
            opts = [Opt(f"choose:{self.ref(i)}", i, sig=("legend", self.obj_sig(i))) for i in ids]
            keep = self.ask(Decision(p, "ChooseCard", opts, prompt=f"Legend rule: choose the {name} to keep (CR 704.5j)."))
            for i in ids:
                if i != keep:
                    moves.append((i, "graveyard", {"cause": "sba"}))
                    gone.add(i)
        for oid in s.battlefield:
            s.objs[oid].deathtouched = False
        if speed := self._speed_sba():
            performed = True
        if moves or destroys:
            batch = list(moves)
            for oid in destroys:
                # "sba": destroyed by a state-based action, not as the result of an effect
                # (704.5g-h): a shield counter doesn't replace it (CR 122.1c); regeneration does.
                ev = self.replace("destroy", {"type": "destroy", "obj": oid, "applied": set(), "sba": True})
                if not ev.get("cancel"):
                    batch.append((oid, "graveyard", {"cause": "destroy"}))
            self.move_batch(batch)
            performed = True
        return performed

    def _speed_sba(self):
        """CR 702.179a: a player who controls a permanent with start your engines! and has no
        speed gets speed 1 (a state-based action; "no speed" is 0 here: nothing sets a speed to
        0, 702.179b, 702.179f). Players with speed have the inherent trigger of 702.179d
        (ref/cards/round2_c2.py). The storied designation (702.195a) is checked here too."""
        from ..cards.round2_c2 import designations_sba
        return designations_sba(self)

    def saga_final_chapter(self, oid):
        c = self.chars(oid)
        chs = [getattr(a, "chapter", None) for a in c.abilities]
        chs = [max(x) for x in chs if x]
        return max(chs) if chs else None

    def aura_attach_legal(self, oid):
        o = self.s.objs[oid]
        t = o.attached_to
        if t is None:
            return False
        if isinstance(t, tuple):
            return False  # no Auras enchanting players in scope
        if t not in self.s.objs or self.s.objs[t].zone != "battlefield":
            return False
        c = self.chars(oid)
        ench = c.kw("enchant")
        if ench is None:
            return False
        f = ench.value
        if f is not None and not f.match(self, t, oid):
            return False
        if self.protected_from(t, oid):
            return False
        return True

    def equip_attach_legal(self, oid):
        o = self.s.objs[oid]
        t = o.attached_to
        if isinstance(t, tuple) or t not in self.s.objs or self.s.objs[t].zone != "battlefield":
            return False
        tc = self.chars(t)
        if "Creature" not in tc.types:
            return False
        if self.protected_from(t, oid):
            return False
        return True

    # ------------------------------------------------------------------ object signatures
    def _may_play_live(self, o, mp):
        """A play permission record (flags['may_play'], effects.play_permission_valid) that can
        still matter: its duration hasn't ended (CR 611.2a: "this turn" ends with the turn) and
        the card is in the zone it covers. An ended one is no part of the object's identity: a
        land Tablet of Discovery milled on an earlier turn and an identical land in the same
        graveyard, both playable through Icetill Explorer, are one option (format §3; lockstep
        recheck-l3 829612)."""
        if not isinstance(mp, dict) or o.zone != mp.get("zone", "exile"):
            return False
        u = mp.get("until")
        if u is None:
            return True
        t, st = u
        if self.s.turn != t:
            return self.s.turn < t
        if st is None:
            return True
        return STEPS.index(self.s.step) < STEPS.index(st)

    def obj_sig(self, oid):
        """Everything that distinguishes an object for choices (identical objects are offered
        once): name, zone, controller, tapped, sickness, damage, counters, attachments, combat
        role, chosen values, links, face, characteristics."""
        s = self.s
        o = s.objs.get(oid)
        if o is None:
            return ("gone", oid)
        if o.zone in ("library", "hand"):
            return (o.card, o.zone, o.owner, o.face_down)
        c = self.chars(oid)
        att = tuple(sorted(self.obj_sig(a) for a in s.battlefield if s.objs[a].attached_to == oid)) if o.zone == "battlefield" else ()
        comb = None
        if s.combat is not None:
            comb = (repr(s.combat.attackers.get(oid, "-")), tuple(sorted(s.combat.blockers.get(oid, []))),
                    oid in s.combat.blocked)
            if oid in s.combat.attackers or oid in s.combat.blockers:
                comb = comb + (oid,)
        def lsig(x):
            lo = s.objs.get(x)
            return (lo.card, lo.owner, lo.zone, lo.face_down) if lo is not None else ("gone",)
        linked = tuple(sorted((k[1], tuple(sorted(lsig(x) for x in v))) for k, v in s.linked.items() if k[0] == oid))
        holds = tuple(sorted(tuple(sorted(lsig(x) for x in r["objs"])) for r in s.until_leaves if r["host"] == oid))
        # The host of an effect that lasts "for as long as ~ remains on the battlefield" (611.2b)
        # differs from an identical non-host: keeping one or the other (legend rule, 704.5j)
        # decides whether the effect continues (The Wondrous Wasp).
        # An effect whose affected objects have all ceased to exist (they left their zone and are
        # new objects, CR 400.7) applies to nothing: hosting it can't matter (two Tishana's
        # Tidebinders, one hosting "loses all abilities" of a creature that has since died, are
        # interchangeable attackers; campaign15 866089 and 7 more, the EUT's merge is right).
        hosts = tuple(sorted(tuple(sorted(lsig(x) for x in (ce.affected or []))) for ce in s.effects
                             if (ce.data or {}).get("host") == oid
                             and not (ce.affected and all(x not in s.objs for x in ce.affected))))
        # Summoning sickness only matters for a creature its controller could attack with or
        # tap/untap for a cost (CR 302.6); otherwise two such objects are interchangeable.
        # Objects targeted by something on the stack differ from identical untargeted ones
        # (a second "exile target card" must be able to pick the other copy).
        targeted = tuple(sorted(it.id for it in s.stack for grp in (it.targets or []) for t in grp if t == oid))
        # So does the source of an ability on the stack (Resplendent Angel's "this creature gets
        # +2/+2": tapping it or an identical other Angel for Springleaf Drum's cost differ).
        targeted += tuple(sorted(("src", it.id) for it in s.stack if it.kind != "spell" and it.source == oid))
        # Likewise the source of a triggered ability waiting to be put on the stack: the legend
        # rule's choice between a new The Wondrous Wasp whose "for as long as The Wondrous Wasp
        # remains on the battlefield" trigger is pending and an identical older one decides
        # whether that effect lasts (CR 704.5j, 603.3, 611.2b).
        pend = list(s.pending) + list(getattr(self, "_unstacked_triggers", None) or ())
        targeted += tuple(sorted(("psrc", getattr(t.ability, "text", "") or "", t.index) for t in pend if t.source == oid))
        # And the object a triggered ability's event names ("it" of "whenever another creature
        # enters, it gets +2/+0"), triggered or on the stack: the legend rule's choice between a
        # new legend that triggered Enduring Courage and the old one matters (CR 704.5j, 603.2).
        # Off the battlefield only if the ability refers to that object at all: Vengeful
        # Bloodwitch's "whenever ~ or another creature you control dies, target opponent loses 1
        # life" doesn't, so the card that died and an identical one already in the graveyard stay
        # interchangeable. (A permanent named by an event, e.g. the land a landfall trigger saw
        # enter, stays apart conservatively: the harness's mapping of copies that entered on
        # different turns relies on it, campaign11 794554.)
        def names_it(ev, ab):
            return isinstance(ev, dict) and oid in (ev.get("new"), ev.get("obj")) and (
                o.zone == "battlefield" or refers_to_event(ab))
        targeted += tuple(sorted(("ev", repr(t.source), t.index) for t in pend if names_it(t.event, t.ability)))
        targeted += tuple(sorted(("ev", it.id) for it in s.stack
                                 if it.kind != "spell" and names_it((getattr(it, "ctx", None) or {}).get("event"), it.ability)))
        # The creature tapped to pay a station ability on
        # the stack ("charge counters equal to the tapped creature's power", CR 702.184a, read as
        # the ability resolves) differs from an identical tapped creature: a +1/+1 counter on one
        # or the other changes the count (rt6-t 1105624, two Floodpits Drowners and Warg Tactics).
        targeted += tuple(sorted(("paid", it.id) for it in s.stack
                                 if it.kind != "spell" and getattr(it.ability, "keyword", None) == "station"
                                 and oid in (((getattr(it, "ctx", None) or {}).get("paid") or {}).get("tapped") or ())))
        # And an object a delayed triggered ability will act on (CR 603.7c: "sacrifice it at the
        # beginning of the next end step" of The Fire Crystal's token copy) differs from an
        # identical one it won't touch.
        def delayed_on_it(d):
            eff = getattr(d.get("ability"), "effect", None)
            ids = list(getattr(eff, "ids", None) or ())
            ids += [v for v in (d.get("data") or {}).values() if isinstance(v, int)]
            # Warp's "exile it at the beginning of the next end step" (702.185a, altcast._WarpExile):
            # a warped creature isn't interchangeable with an identical one (campaign12 814834).
            ids += [getattr(eff, "oid", None)]
            return oid in ids
        targeted += tuple(sorted(("delayed", getattr(d.get("ability"), "text", "") or "") for d in s.delayed if delayed_on_it(d)))
        # The new object a delayed trigger's event names
        # (earthbend's "When it dies or is put into exile, return it to the battlefield tapped",
        # CR 701.66a: the land's card in the graveyard; 603.7c, 400.7) differs from an identical
        # card the trigger won't return, while that trigger is waiting (its event not yet checked
        # during a resolution, or triggered and not yet on the stack, 603.3) or on the stack:
        # exiling one or the other (Deadly Cover-Up's "exile a card from an opponent's
        # graveyard") decides whether the land comes back (rt4-f 1007009).
        if o.zone != "battlefield":
            watched = {getattr(d.get("trigger"), "oid", None): getattr(d.get("ability"), "text", "") or ""
                       for d in s.delayed if getattr(d.get("trigger"), "oid", None) is not None}
            targeted += tuple(sorted(("dnew", watched[ev.get("old")]) for ev in s.events
                                     if isinstance(ev, dict) and ev.get("type") == "zone" and ev.get("new") == oid
                                     and ev.get("old") in watched))
            # (: `pend` also holds the triggers of the batch being
            # put on the stack, which s.pending no longer lists: Carnivorous Cultivator's "return
            # target land card" put before the earthbend return of the Forest that died in the same
            # combat damage step, rt4-f 1017455, 1258394.)
            targeted += tuple(sorted(("dnew", getattr(t.ability, "text", "") or "") for t in pend
                                     if t.delayed is not None and isinstance(t.data, dict) and t.data.get("obj") == oid))
            targeted += tuple(sorted(("dnew", it.id) for it in s.stack if it.kind != "spell"
                                     and ((getattr(it, "ctx", None) or {}).get("data") or {}).get("obj") == oid))
        # Per-turn use of its abilities (a planeswalker that already activated a loyalty ability
        # this turn, CR 606.3; "activate only once each turn" limits) distinguishes it too.
        used = tuple(sorted((str(k[0]), str(k[2:]) if len(k) > 2 else "", repr(v)) for k, v in s.turn_flags.items()
                            if isinstance(k, tuple) and len(k) >= 2 and k[1] == oid))
        sick = o.sick and "Creature" in c.types and not c.has_kw("haste") and (
            c.controller == s.active or any(a.kind == "activated" and (a.cost.tap or a.cost.untap) for a in c.abilities))
        # An object with an ability that depends on whether
        # "~ entered this turn" (Dark Fortress's "{T}: Add {B} or {R}. Activate only if this land
        # entered this turn or if you control a basic land", CR 602.5b) differs from an identical
        # one that entered earlier: tapping one or the other for {C} leaves different mana
        # (campaign21 1310992: ours tapped the old Dark Fortress's {C} as "the same" as the new one's,
        # and could still make {B} for Starscape Cleric; the EUT couldn't).
        entered_now = o.zone == "battlefield" and getattr(o, "entered_turn", None) == s.turn and any(
            "entered this turn" in (getattr(a, "text", "") or "") for a in c.abilities)
        return (o.card, c.name, o.zone, c.controller, o.owner, o.tapped,
                sick, o.damage, tuple(sorted(o.counters.items())),
                repr(o.attached_to) if o.attached_to is not None else None, att, comb,
                tuple(sorted(o.chosen.items())), o.face, o.face_down, o.token, c.power,
                c.toughness, tuple(c.keywords()), tuple(sorted(c.types)),
                tuple(sorted(c.subtypes)), linked, holds, o.phased_out,
                # A warped card's turn matters only until that turn ends (CR 702.185a: castable
                # after it): two warped same-named cards castable now are interchangeable. So for
                # a plotted card (702.170d: cast on a later turn than the one it became plotted;
                # campaign12 805978: two Slickshot Show-Offs plotted on turns 3 and 5, on turn 7).
                tuple(sorted((k, str(v) if k not in ("warped", "plotted") else str(s.turn > v)) for k, v in o.flags.items()
                             if k != "copiable" and not (k == "may_play" and not self._may_play_live(o, v)))),
                # Whether it is a copy isn't a characteristic: a token copy of a token with the
                # same characteristics is interchangeable with it; its abilities still count.
                tuple(sorted(getattr(a, "text", "") or repr(a) for a in c.abilities)), targeted, used, hosts,
                # A copy of a spell on the stack and the spell it copies aren't interchangeable: the
                # copy countered ceases to exist, the card goes to its owner's graveyard (CR 707.10,
                # 704.5e, 701.6a; campaign13 839296: Countersculpt on Sage of the Skies or its copy).
                bool(getattr(o, "copy_of_spell", False)) and o.zone == "stack",
                # Two spells of one name on the stack differ by the choices made as they were cast
                # (CR 601.2b: X, kicker, optional additional costs such as blight or bargain, a
                # promised gift, the alternative cost): returning or countering the Requiting Hex
                # whose blight cost was paid isn't the same as the other one (campaign17 1022302).
                self._cast_sig(o) if o.zone == "stack" else None, entered_now)

    @staticmethod
    def _cast_sig(o):
        c = o.cast or {}
        return (o.x, o.kicked, c.get("how"), tuple(sorted(map(str, c.get("paid_additional") or ()))),
                c.get("gift_to"))
