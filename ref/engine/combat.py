"""Combat (CR 506-511) and rule-modifying static effects lookups."""
from .decisions import Decision, Opt, AmountOptions
from .. import Unsupported

# Damage-assignment decisions list one option per amount (format: `assign:<ref>:<n>`). Up to
# LIST_ASSIGN_OPTIONS amounts they are a list of Opts; above, a lazily built range
# (decisions.AmountOptions, same option strings). Beyond MAX_ASSIGN_OPTIONS the reference engine
# refuses (Unsupported): the differential harness still walks every option of a sub-decision.
LIST_ASSIGN_OPTIONS = 10000
MAX_ASSIGN_OPTIONS = 1000000


class CombatMixin:
    # ------------------------------------------------------------------ rule effects
    def rule_parts(self, rule):
        """[(RulePart, source oid)] for static rule effects currently in force, plus resolved
        rule effects (ContEffect kind 'rule') as (part, ce)."""
        s = self.s
        out = []
        for oid, ab, parts in self._rule_index().get(rule, ()):
            if ab.condition is not None and not ab.condition(self, oid):
                continue
            for part in parts:
                out.append((part, oid))
        for it in s.stack:
            if it.kind != "spell" or it.obj not in s.objs:
                continue
            c = self.chars(it.obj)
            for ab in c.abilities:
                if ab.kind == "static" and "stack" in ab.zones:
                    for part in ab.parts:
                        if part.kind == "rule" and part.rule == rule:
                            out.append((part, it.obj))
        for ce in s.effects:
            if ce.kind == "rule" and ce.data.get("rule") == rule:
                out.append((ce.data["part"], ce))
        return out

    def _rule_index(self):
        """rule -> [(source oid, static ability, [its parts for that rule])] over the battlefield,
        in battlefield, ability and part order (what rule_parts scanned on every call). The
        abilities come from the layer-computed characteristics, so the index lives as long as
        that cache (s.chars_cache, rebuilt after every touch) and the battlefield list are the
        same; conditions are still evaluated on each lookup. A pure performance measure: with
        ~1,000 permanents (Krenko, Mob Boss) the per-call scan of every permanent's abilities
        dominated a game (block legality asks rule_applies per pair)."""
        s = self.s
        if self._partial is not None:
            return self._rule_index_build()  # computing layers: partial characteristics only
        if s.chars_cache is None:
            self.chars(s.battlefield[0]) if s.battlefield else self._compute_all()
        ri = getattr(s, "rule_index", None)
        if ri is not None and ri[0] is s.chars_cache and ri[1] == s.battlefield:
            return ri[2]
        idx = self._rule_index_build()
        s.rule_index = (s.chars_cache, list(s.battlefield), idx)
        return idx

    def _rule_index_build(self):
        idx = {}
        for oid in self.s.battlefield:
            for ab in self.chars(oid).abilities:
                if ab.kind != "static":
                    continue
                by = {}
                for part in ab.parts:
                    if part.kind == "rule":
                        by.setdefault(part.rule, []).append(part)
                for rule, parts in by.items():
                    idx.setdefault(rule, []).append((oid, ab, parts))
        return idx

    def _affects(self, part, src, oid, ctx=None):
        aff = part.affected
        if aff == "self":
            return src == oid
        if aff == "attached":
            o = self.s.objs.get(src) if isinstance(src, int) else None
            return o is not None and o.attached_to == oid
        if aff == "locked":
            return oid in src.affected
        if callable(aff):
            return aff(self, oid, src)
        if hasattr(aff, "match"):
            return aff.match(self, oid, src if isinstance(src, int) else None, ctx or {})
        return False

    def rule_applies(self, oid, rule, ctx=None, **kw):
        for part, src in self.rule_parts(rule):
            if self._affects(part, src, oid, ctx):
                cond = part.data.get("when")
                if cond is not None and not cond(self, oid, src, kw):
                    continue
                return True
        return False

    def rule_applies_player(self, p, rule, **kw):
        for part, src in self.rule_parts(rule):
            who = part.data.get("who", "opp")
            sc = self.ctrl(src) if isinstance(src, int) else src.controller
            if (who == "opp" and sc != p) or (who == "you" and sc == p) or who == "each":
                f = part.data.get("filter")
                fp = part.data.get("filter_p")  # a rule that depends on the player (High Noon)
                if fp is not None and not fp(self, p, kw.get("chars")):
                    continue
                if f is None or f(self, kw.get("chars")):
                    return True
        return False

    # ------------------------------------------------------------------ attackers (508)
    def can_attack(self, oid):
        s = self.s
        o = s.objs[oid]
        c = self.chars(oid)
        if "Creature" not in c.types or "Battle" in c.types:
            return False
        if o.tapped or o.phased_out:
            return False
        if o.sick and not c.has_kw("haste"):
            return False  # 302.6 / 508.1a
        if c.has_kw("defender") and not self.rule_applies(oid, "attack_despite_defender"):
            return False  # 702.3b
        if self.rule_applies(oid, "cant_attack"):
            return False
        return True

    def defenders(self, p):
        s = self.s
        out = [("player", 1 - p)]
        for oid in s.battlefield:
            c = self.chars(oid)
            if c.controller != p and "Planeswalker" in c.types:
                out.append(oid)
        return out

    def can_attack_defender(self, a, d):
        if isinstance(d, tuple):
            if self.rule_applies(a, "cant_attack_players"):
                return False
        return True

    def declare_attackers(self):
        s = self.s
        p = s.active
        cands = [oid for oid in s.battlefield if self.ctrl(oid) == p and self.can_attack(oid)]
        defs = self.defenders(p)
        chosen = {}
        must = [oid for oid in cands if self.rule_applies(oid, "must_attack")]
        while True:
            opts = []
            if all(m in chosen for m in must):
                opts.append(Opt("done", None))
            for a in cands:
                if a in chosen:
                    continue
                for d in defs:
                    if self.can_attack_defender(a, d):
                        opts.append(Opt(f"attack:{self.ref(a)}->{self.ref(d)}", (a, d),
                                        sig=("attack", self.obj_sig(a), d if isinstance(d, tuple) else self.obj_sig(d))))
            if len(opts) == 1 and opts[0].data is None:
                break
            ans = self.ask(Decision(p, "DeclareAttackers", opts, top=not chosen,
                                    prompt="Declare attackers (one at a time; done when finished)."))
            if ans is None:
                break
            chosen[ans[0]] = ans[1]
        comb = s.combat
        comb.declared = True
        for a, d in chosen.items():
            c = self.chars(a)
            if not c.has_kw("vigilance"):
                self.tap(a)  # 508.1f
            comb.attackers[a] = d
        s.touch()
        for a, d in chosen.items():
            self.emit({"type": "attacks", "obj": a, "defender": d, "player": p})  # 508.1m
        if chosen:
            self.emit({"type": "attackers_declared", "player": p, "attackers": list(chosen)})

    # ------------------------------------------------------------------ blockers (509)
    def can_block(self, b):
        s = self.s
        o = s.objs[b]
        c = self.chars(b)
        if "Creature" not in c.types or "Battle" in c.types or o.tapped or o.phased_out:
            return False
        if self.rule_applies(b, "cant_block"):
            return False
        return True

    def can_block_attacker(self, b, a):
        bc = self.chars(b)
        ac = self.chars(a)
        if ac.has_kw("flying") and not (bc.has_kw("flying") or bc.has_kw("reach")):
            return False  # 702.9b
        if self.rule_applies(a, "unblockable"):
            return False
        if self.rule_applies(b, "can_block_only_flying") and not ac.has_kw("flying"):
            return False
        for part, src in self.rule_parts("can_block_only"):
            # "~ can't block non-Spirit creatures" (Realm of Koh's Spirit token; round2_c2.py)
            if self._affects(part, src, b) and not part.data["filter"](self, a):
                return False
        if self.protected_from(a, b):
            return False  # 702.16f
        for part, src in self.rule_parts("cant_be_blocked_by"):
            if self._affects(part, src, a) and part.data["filter"](self, b, a):
                return False
        if ac.has_kw("skulk") and bc.power is not None and ac.power is not None and bc.power > ac.power:
            return False
        return True

    def _menace_ok(self, blocks, attackers):
        cnt = {}
        for b, a in blocks.items():
            cnt[a] = cnt.get(a, 0) + 1
        for a in attackers:
            n = cnt.get(a, 0)
            if n == 1 and self.chars(a).has_kw("menace"):
                return False  # 702.111b
            if n > 1 and self.rule_applies(a, "cant_be_blocked_by_more_than_one"):
                return False
        return True

    def _completable(self, blocks, attackers, blockers):
        """Can more blocks be added so that the declaration becomes legal? Adding blocks only
        raises each attacker's count of blockers: an attacker that "can't be blocked by more than
        one creature" already blocked twice stays illegal, and each menace attacker blocked by
        one creature needs one more of its own (702.111b). So it's a matching of those menace
        attackers to distinct free blockers that can block them (blocking anything else is never
        needed). (The earlier search tried every block recursively: exponential with many
        blockers, a campaign13 game ran for hours on one declaration, 833262.)"""
        cnt = {}
        for b, a in blocks.items():
            cnt[a] = cnt.get(a, 0) + 1
        need = []
        for a in attackers:
            n = cnt.get(a, 0)
            max_one = (n >= 1 and self.rule_applies(a, "cant_be_blocked_by_more_than_one"))
            if n > 1 and max_one:
                return False
            if n == 1 and self.chars(a).has_kw("menace"):
                if max_one:
                    return False  # menace and "no more than one": it can't be blocked at all
                need.append(a)
        if not need:
            return True
        free = [b for b in blockers if b not in blocks]
        cand = {a: [b for b in free if self.can_block_attacker(b, a)] for a in need}
        match = {}  # free blocker -> menace attacker it joins

        def augment(a, seen):
            for b in cand[a]:
                if b not in seen:
                    seen.add(b)
                    if b not in match or augment(match[b], seen):
                        match[b] = a
                        return True
            return False
        return all(augment(a, set()) for a in need)

    def declare_blockers(self):
        s = self.s
        comb = s.combat
        p = 1 - s.active
        attackers = [a for a in comb.attackers if a in s.objs]
        blockers = [b for b in s.battlefield if self.ctrl(b) == p and self.can_block(b)]
        blocks = {}
        while True:
            opts = []
            if self._menace_ok(blocks, attackers):
                opts.append(Opt("done", None))
            for b in blockers:
                if b in blocks:
                    continue
                for a in attackers:
                    if not self.can_block_attacker(b, a):
                        continue
                    nb = dict(blocks)
                    nb[b] = a
                    if not self._completable(nb, attackers, blockers):
                        continue
                    opts.append(Opt(f"block:{self.ref(b)}->{self.ref(a)}", (b, a),
                                    sig=("block", self.obj_sig(b), self.obj_sig(a))))
            if len(opts) == 1 and opts[0].data is None:
                break
            if not opts:
                break
            dm = Decision(p, "DeclareBlockers", opts, top=not blocks,
                          prompt="Declare blockers (one at a time; done when finished).")
            dm.blocks_so_far = list(blocks.items())  # metadata for the differential harness
            ans = self.ask(dm)
            if ans is None:
                break
            blocks[ans[0]] = ans[1]
        comb.blockers_declared = True
        for b, a in blocks.items():
            comb.blockers[b] = [a]
            comb.blocked.add(a)  # 509.1h
        s.touch()
        for b, a in blocks.items():
            self.emit({"type": "blocks", "obj": b, "attacker": a, "player": p})
        for a in attackers:
            if a in comb.blocked:
                self.emit({"type": "becomes_blocked", "obj": a})
            else:
                self.emit({"type": "unblocked", "obj": a})
        self.emit({"type": "blockers_declared", "player": p})

    # ------------------------------------------------------------------ damage (510)
    def any_first_strike(self):
        s = self.s
        comb = s.combat
        if comb is None:
            return False
        parts = list(comb.attackers) + list(comb.blockers)
        return any(x in s.objs and (self.chars(x).has_kw("first strike") or self.chars(x).has_kw("double strike")) for x in parts)

    def _deals_this_step(self, x, first):
        c = self.chars(x)
        fs = c.has_kw("first strike") or c.has_kw("double strike")
        comb = self.s.combat
        if first:
            return fs
        if not comb.first_strike_step:
            return True
        # 702.7b: remaining creatures that had neither first nor double strike as the first
        # step began, plus those that currently have double strike.
        return (x not in comb.dealt_first and x not in comb.removed) or c.has_kw("double strike")

    def lethal_needed(self, blocker, attacker, already):
        """Lethal damage still needed for trample checks (702.19b, 702.2c)."""
        bc = self.chars(blocker)
        if bc.toughness is None:
            return 0
        need = max(0, bc.toughness - self.s.objs[blocker].damage - already)
        if self.chars(attacker).has_kw("deathtouch"):
            return min(need, 1)
        return need

    def combat_damage(self, first):
        s = self.s
        comb = s.combat
        if comb is None:
            return
        if first:
            comb.first_strike_step = True
        assignments = []  # (source, target, n)
        assigned_to = {}  # blocker -> damage assigned so far this step (for lethal checks)
        ap = s.active
        attackers = [a for a in list(comb.attackers) if a in s.objs and s.objs[a].zone == "battlefield"]
        for a in attackers:
            if not self._deals_this_step(a, first):
                continue
            c = self.chars(a)
            power = c.power or 0
            if power <= 0:
                continue  # 510.1a
            d = comb.attackers[a]
            if a not in comb.blocked:
                if d is None:
                    continue  # 510.1b
                if isinstance(d, int) and d not in s.objs:
                    continue
                assignments.append((a, d, power))
                continue
            blockers = [b for b, atts in comb.blockers.items() if a in atts and b in s.objs]
            blockers.sort(key=lambda b: s.objs[b].timestamp)
            trample = c.has_kw("trample")
            if not blockers:
                if trample and d is not None:
                    assignments.append((a, d, power))  # 702.19d
                continue
            if len(blockers) == 1 and not trample:
                assignments.append((a, blockers[0], power))
                continue
            assignments += self.assign_attacker_damage(ap, a, power, blockers, d if trample else None, assigned_to)
        dp = 1 - ap
        for b, atts in list(comb.blockers.items()):
            if b not in s.objs or s.objs[b].zone != "battlefield":
                continue
            if not self._deals_this_step(b, first):
                continue
            c = self.chars(b)
            power = c.power or 0
            if power <= 0:
                continue
            live = [a for a in atts if a in s.objs and a in comb.attackers]
            if not live:
                continue  # 510.1d
            if len(live) == 1:
                assignments.append((b, live[0], power))
            else:
                assignments += self.assign_attacker_damage(dp, b, power, live, None, {})
        if first:
            for x in list(comb.attackers) + list(comb.blockers):
                if x in s.objs:
                    cc = self.chars(x)
                    if cc.has_kw("first strike") or cc.has_kw("double strike"):
                        comb.dealt_first.add(x)
        # 510.2: all combat damage is dealt simultaneously.
        lki = {src: self.chars(src) for src, _, _ in assignments}
        prev = self.damage_batch_start()
        for src, tgt, n in assignments:
            if isinstance(tgt, int) and (tgt not in s.objs or s.objs[tgt].zone != "battlefield"):
                continue
            self.deal_damage(src, tgt, n, combat=True, source_chars=lki[src])
        self.damage_batch_end(prev)
        self.emit({"type": "combat_damage_step", "first": first})

    def assign_attacker_damage(self, p, a, power, recipients, trample_to, assigned_to):
        """Divide combat damage among blockers (510.1c-d) and, with trample, the defender
        (702.19b): one recipient at a time; the player (trample) gets what's left, which is
        only legal once every blocker has been assigned lethal damage."""
        out = []
        remaining = power
        order = list(recipients)
        lethal = {b: self.lethal_needed(b, a, assigned_to.get(b, 0)) for b in order}
        all_lethal = True
        for i, b in enumerate(order):
            if remaining <= 0:
                break
            last = i == len(order) - 1
            if last and (trample_to is None or not all_lethal):
                n = remaining
            else:
                lo = min(lethal[b], remaining) if (last and trample_to is not None) else 0
                if remaining + 1 - lo > MAX_ASSIGN_OPTIONS:
                    # One option per amount (format §3.1 `assign:<ref>:<n>`) can't be listed for huge
                    # power (campaign8 seed 733703: ~69.5 million; report 0016): fail loudly.
                    raise Unsupported(f"damage assignment with {remaining + 1 - lo} possible amounts "
                                      f"(reference engine limit {MAX_ASSIGN_OPTIONS})")
                if remaining + 1 - lo > LIST_ASSIGN_OPTIONS:
                    opts = AmountOptions(f"assign:{self.ref(b)}:", lo, remaining)
                else:
                    opts = [Opt(f"assign:{self.ref(b)}:{k}", k) for k in range(lo, remaining + 1)]
                n = self.ask(Decision(p, "AssignDamage", opts, subject=self.ref(a),
                                      prompt=f"Assign combat damage of {self.name_of(a)} ({remaining} left)."))
            if n < lethal[b]:
                all_lethal = False
            if n:
                out.append((a, b, n))
                assigned_to[b] = assigned_to.get(b, 0) + n
            remaining -= n
        if remaining > 0 and trample_to is not None:
            out.append((a, trample_to, remaining))
        return out
