"""The accepted entry and Main-flip limits for the separate cash experiment."""

import json

import math

from copy import deepcopy

from src import paired_ladder as ladder, paper_execution

from src.market_only import EPS

SWITCH_POLICY = 'PHASED_PROTECTION_SWITCH_V1'

PROFIT_MAIN_MIN = .80

PROFIT_KEEP_FRACTION = .50

NORMAL_BUY_POLICY = 'NORMAL_MAIN_NET_GUARD_V1'

NORMAL_BUY_TOLERANCE = 1e-8

def normal_buy_rules(exit_price):
    return {'scope': 'case_0_first_deferred_and_ordinary_allocation',
            'exit_price': exit_price, 'minimum_main_net_pnl': 0.,
            'include_buy_and_exit_fees': True, 'include_past_sell_and_redemption': True,
            'rejected_action': 'skip_entire_allocation_retry_current_tier',
            'reallocate_or_shrink_protection': False, 'pnl_tolerance': NORMAL_BUY_TOLERANCE}

class CashPolicy:
    def __init__(self, *, exit_price=.998):
        if not math.isfinite(exit_price) or not 0 < exit_price <= 1:
            raise ValueError('Invalid normal BUY exit price')
        self.exit_price = exit_price

    def opening_bankroll(self, conn, day):
        return conn.execute('SELECT cash FROM paper_cash_account WHERE id=1').fetchone()[0]

    def select_protection(self, conn, state, timestamp, day, prices, probs, books):
        main = max(probs, key=probs.get)
        proposed = ladder._protection(probs, main)
        old = state['protection']
        if (state['main'] != main or state['handover_completed'] or not old
                or proposed == old):
            return proposed
        held, _ = ladder._portfolio(conn, day)
        if held.get(old, 0.) <= EPS or conn.execute(
                'SELECT 1 FROM paper_entry_wait WHERE target_date=?', (day,)).fetchone():
            return proposed
        details = {'policy': SWITCH_POLICY, 'main': main, 'old_protection': old,
                   'new_protection': proposed, 'main_probability': probs[main],
                   'new_probability': probs[proposed], 'gap': probs[proposed]-probs.get(old, 0.),
                   'new_min': ladder.HANDOVER_C_NEW_MIN, 'gap_min': ladder.HANDOVER_C_GAP,
                   'profit_main_min': PROFIT_MAIN_MIN, 'profit_keep_fraction': PROFIT_KEEP_FRACTION,
                   'tail_min': ladder.HANDOVER_C_TAIL_MIN}
        if old not in probs:
            accepted, reason = False, 'old_probability_missing'
        elif probs[proposed]+EPS < ladder.HANDOVER_C_NEW_MIN:
            accepted, reason = False, 'new_probability_below_minimum'
        elif details['gap']+EPS < ladder.HANDOVER_C_GAP:
            accepted, reason = False, 'gap_below_minimum'
        elif probs[main] >= ladder.HANDOVER_C_TAIL_MIN:
            accepted, reason = False, 'tail_handover_disabled'
        else:
            accepted, preview = self._preview_switch(
                conn, state, timestamp, day, main, proposed, prices, probs, books)
            details.update(preview)
            reason = preview['reason']
        details.update(accepted=accepted, reason=reason,
                       selected_protection=proposed if accepted else old)
        ladder._event(conn, timestamp, day, 'PROTECTION_SWITCH_CHECK', details)
        if reason == 'old_probability_missing':
            return None
        return proposed if accepted else old

    def _preview_switch(self, conn, state, timestamp, day, main, proposed, prices, probs, books):
        # Roll back both cash and depth consumption; the real cycle executes once.
        held, deployed = ladder._portfolio(conn, day)
        before = held.get(main, 0.)-deployed
        conn.execute('SAVEPOINT protection_switch_preview')
        try:
            decision = ladder._handover_c_gate(
                conn, state, timestamp, day, main, proposed, probs, deepcopy(books))
            if decision:
                after = decision['gate']['projected_after'][main]
                return True, {'reason': 'handover_allowed',
                              'before_main_profit': before, 'projected_main_profit': after}
            tier = ladder._tier(probs[main])
            coverage = probs[main]+probs[proposed]
            case = (2 if state['mode'] == ladder.MODES[2] else
                    1 if state['mode'] == ladder.MODES[1] else
                    3 if state['mode'] == ladder.MODES[3] else 0)
            simulated_state = dict(state, main=main, protection=proposed)
            count = conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0]
            if case == 2:
                self.recovery(conn, simulated_state, timestamp, day, prices, probs, deepcopy(books), tier)
            elif tier and coverage+EPS >= .80 and tier > state['highest_tier']:
                target = state['bankroll_before']*ladder.TIERS[tier-1][1]
                if target > deployed+EPS:
                    self.allocate(conn, simulated_state, timestamp, day, prices, probs, deepcopy(books), target, case)
            after_held, after_cost = ladder._portfolio(conn, day)
            after = after_held.get(main, 0.)-after_cost
            details = {'before_main_profit': before, 'projected_main_profit': after,
                       'required_main_profit': PROFIT_KEEP_FRACTION*before if before > EPS else None}
            if (probs[main] >= PROFIT_MAIN_MIN and before > EPS
                    and after+1e-7 < PROFIT_KEEP_FRACTION*before):
                return False, dict(details, reason='main_profit_keep_below_minimum')
            bought = conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0] > count
            return bought, dict(details, reason='ordinary_switch_allowed' if bought else 'no_executable_switch_keep_old')
        finally:
            conn.execute('ROLLBACK TO SAVEPOINT protection_switch_preview')
            conn.execute('RELEASE SAVEPOINT protection_switch_preview')

    def handover_gate(self, conn, state, timestamp, day, main, protection, probs, books):
        if probs[main] >= ladder.HANDOVER_C_TAIL_MIN:
            if state['handover_pending'] != '{}':
                conn.execute("UPDATE paired_state SET handover_pending='{}' WHERE target_date=?", (day,))
            return None
        return ladder._handover_c_gate(conn, state, timestamp, day, main, protection, probs, books)

    def allocate(self, conn, state, timestamp, day, prices, probs, books, target, case):
        if case != 0:
            return self._allocate_original(conn, state, timestamp, day, prices, probs, books, target, case)
        before_id = conn.execute('SELECT COALESCE(MAX(id),0) FROM fills').fetchone()[0]
        conn.execute('SAVEPOINT normal_buy_preview')
        try:
            self._allocate_original(conn, state, timestamp, day, prices, probs, deepcopy(books), target, case)
            planned = [dict(row) for row in conn.execute(
                'SELECT outcome,cost,shares FROM fills WHERE id>? ORDER BY id', (before_id,))]
            held, cost = ladder._portfolio(conn, day)
            quantity = held.get(state['main'], 0.)
            recovered = conn.execute("SELECT COALESCE(SUM(amount),0) FROM paper_cash_movements "
                                     "WHERE target_date=? AND kind IN ('SELL','REDEEM')", (day,)).fetchone()[0]
            schedule = books.get(state['main'], {}).get('_paper_fee')
            try:
                charge = paper_execution.fee(quantity, self.exit_price, schedule)
                score = recovered + quantity*self.exit_price - charge - cost
                if not math.isfinite(score):
                    score = None
            except (KeyError, TypeError, ValueError, OverflowError):
                charge, score = None, None
        finally:
            conn.execute('ROLLBACK TO SAVEPOINT normal_buy_preview')
            conn.execute('RELEASE SAVEPOINT normal_buy_preview')
        if planned:
            accepted = score is not None and score >= -NORMAL_BUY_TOLERANCE
            ladder._event(conn, timestamp, day, 'NORMAL_BUY_GUARD_CHECK',
                          {'policy': NORMAL_BUY_POLICY, 'accepted': accepted, 'case': case,
                           'main': state['main'], 'protection': state['protection'],
                           'target_cost': target, 'exit_price': self.exit_price,
                           'projected_main_net_pnl': score, 'projected_buy_cost': cost,
                           'projected_main_shares': quantity, 'projected_exit_fee': charge,
                           'recovered_net': recovered, 'planned_fills': planned,
                           'reason': 'net_main_nonnegative' if accepted else (
                               'exit_fee_missing_or_invalid' if score is None else 'net_main_negative')})
            if not accepted:
                return
        return self._allocate_original(conn, state, timestamp, day, prices, probs, books, target, case)

    def _allocate_original(self, conn, state, timestamp, day, prices, probs, books, target, case):
        held, deployed = ladder._portfolio(conn, day)
        main, protection = state['main'], state['protection']
        pending = conn.execute('SELECT * FROM paper_entry_wait WHERE target_date=?', (day,)).fetchone()
        if pending and (pending['main'] != main or held.get(protection, 0.) > EPS):
            conn.execute('DELETE FROM paper_entry_wait WHERE target_date=?', (day,))
            pending = None
        if case != 0 or (deployed > EPS and pending is None):
            return ladder._allocate(conn, state, timestamp, day, prices, probs, books, target, case)
        available = max(0., target-deployed)
        p_book, m_book = books.get(protection, {}), books.get(main, {})
        desired = max(0., .9*target-held.get(protection, 0.))
        p_cost = ladder._cost_for_shares(p_book, desired) if desired > EPS else 0.
        if not math.isfinite(p_cost):
            p_cost = ladder._quote(p_book)['depth_cost']
        if 0 < p_cost < ladder._min_cost(p_book) <= available+EPS:
            p_cost = ladder._min_cost(p_book)
        p_quote = ladder._quote(p_book, cost=min(available, p_cost))
        p_min = float(p_book.get('min_order_size') or ladder.MIN_ORDER_SHARES)
        actual_p = p_quote['cost'] if p_quote['shares']+EPS >= p_min else 0.
        m_quote = ladder._quote(m_book, cost=max(0., available-actual_p))
        if m_quote['vwap'] is None:
            m_quote = ladder._quote(m_book, shares=float(m_book.get('min_order_size') or ladder.MIN_ORDER_SHARES))
        m_price, p_price = m_quote['vwap'], p_quote['vwap']
        price_sum = m_price+p_price if m_price is not None and p_price is not None else None
        blocked = price_sum is None or price_sum > 1.+EPS
        if not blocked:
            ladder._allocate(conn, state, timestamp, day, prices, probs, books, target, case)
            after, _ = ladder._portfolio(conn, day)
            if after.get(protection, 0.) > EPS:
                conn.execute('DELETE FROM paper_entry_wait WHERE target_date=?', (day,))
            return
        reserved = max(pending['reserved_cash'] if pending else 0., p_quote['cost'])
        conn.execute('INSERT INTO paper_entry_wait VALUES (?,?,?) ON CONFLICT(target_date) '
                     'DO UPDATE SET main=excluded.main,reserved_cash=excluded.reserved_cash',
                     (day, main, reserved))
        budget = min(max(0., available-reserved) if price_sum is not None else 0.,
                     max(0., .05*state['bankroll_before']-deployed))
        ladder._event(conn, timestamp, day, 'ENTRY_PROTECTION_DEFERRED',
                      {'main': main, 'protection': protection, 'price_sum': price_sum,
                       'reserved_cash': reserved, 'main_budget': budget,
                       'unprotected_cost_cap': .05*state['bankroll_before']})
        if budget+EPS >= ladder._min_cost(m_book):
            ladder._buy(conn, timestamp, day, main, budget, books, prices, probs, 'ENTRY_MAIN_ONLY')

    def recovery(self, conn, state, timestamp, day, prices, probs, books, tier):
        held, deployed = ladder._portfolio(conn, day)
        main, protection, bankroll = state['main'], state['protection'], state['bankroll_before']
        total_cap = bankroll*(ladder.TIERS[tier-1][2] if tier else .15)
        cash = self.opening_bankroll(conn, day)
        available = min(max(0., total_cap-deployed), max(0., .10*bankroll-state['recovery_used']), cash)
        book = books.get(main, {})
        needed = ladder._main_requirement(book, held.get(main, 0.), deployed, 0.)
        minimum = ladder._min_cost(book)
        feasible = needed <= available+EPS and (needed <= EPS or minimum <= available+EPS)
        hole = max(0., deployed-held.get(main, 0.))
        requested = min(available, max(needed, minimum)) if hole > EPS and feasible else 0.
        quote = ladder._quote(book, cost=requested)
        projected = {n: held.get(n, 0.)-deployed-quote['cost'] for n in probs}
        projected[main] += quote['shares']
        coverage = probs.get(main, 0.)+probs.get(protection, 0.)
        reason = 'break_even_feasible' if feasible else 'break_even_infeasible_wait'
        if requested > EPS:
            if held.get(protection, 0.) <= EPS or coverage+EPS < .80:
                requested, reason = 0., 'held_protection_or_pair_coverage_missing'
            elif projected[main] < -1e-7:
                requested, reason = 0., 'main_break_even_not_executable'
            elif any(projected[n] < -.05*bankroll-1e-7 for n, q in held.items() if q > EPS):
                requested, reason = 0., 'held_leg_loss_limit'
        spent = 0.
        if requested > EPS and requested+EPS >= minimum:
            spent = ladder._buy(conn, timestamp, day, main, requested, books, prices, probs, 'BOUNDED_FULL_RECOVERY')
        after = ladder._map(conn, day, probs)
        info = {'reason': reason, 'settlement_hole': hole,
                'break_even_required_capital': needed if math.isfinite(needed) else None,
                'available_recovery_capital': available, 'requested_cost': requested,
                'actual_cost': spent, 'projected_main_pnl': after[main],
                'break_even_feasible': feasible, 'pair_coverage': coverage,
                'additional_cap': .10*bankroll, 'held_leg_loss_floor': -.05*bankroll,
                'settlement_map_after': after}
        ladder._event(conn, timestamp, day, 'BOUNDED_RECOVERY_CHECK', info)
        conn.execute('UPDATE paired_state SET recovery_used=recovery_used+?,break_even_feasible=?, '
                     'recovery_info=? WHERE target_date=?', (spent, int(feasible), json.dumps(info), day))
