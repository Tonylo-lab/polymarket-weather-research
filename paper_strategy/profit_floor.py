"""Preserve Main scenario profit on ordinary additions to an existing pair.

Only used by the shared legacy adapter. No execution or network access.
"""

from copy import deepcopy

import math

from src import paired_ladder as ladder, paper_execution

from src.paired_cash_policy import CashPolicy as OriginalPolicy

POLICY = 'PAIRED_MAIN_PROFIT_NONDECREASING_V1'

EPS = 1e-8

def rules():
    return {'policy': POLICY, 'scope': 'case_0_with_both_current_legs_held_before',
            'comparison': 'after_main_net_pnl >= before_main_net_pnl',
            'exit_price': .998, 'same_current_exit_fee_schedule': True,
            'include_all_event_buy_costs_and_fees': True,
            'include_past_sell_and_redemption_net': True,
            'tolerance': EPS, 'rejection': 'skip_entire_batch_and_retry',
            'first_protection_and_main_flip_repairs': 'original_rules'}

def main_profit(conn, day, main, price, books):
    held, cost = ladder._portfolio(conn, day)
    quantity = held.get(main, 0.)
    recovered = conn.execute("SELECT COALESCE(SUM(amount),0) FROM paper_cash_movements "
                             "WHERE target_date=? AND kind IN ('SELL','REDEEM')", (day,)).fetchone()[0]
    try:
        charge = paper_execution.fee(quantity, price, books.get(main, {}).get('_paper_fee'))
        value = recovered + quantity*price - charge - cost
        return value if math.isfinite(value) else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None

class CashPolicy(OriginalPolicy):
    def allocate(self, conn, state, timestamp, day, prices, probs, books, target, case):
        held, cost = ladder._portfolio(conn, day)
        main, protection = state['main'], state['protection']
        paired = (case == 0 and main != protection and cost > EPS
                  and held.get(main, 0.) > EPS and held.get(protection, 0.) > EPS)
        if not paired:
            return super().allocate(conn, state, timestamp, day, prices, probs, books, target, case)
        before = main_profit(conn, day, main, self.exit_price, books)
        before_id = conn.execute('SELECT COALESCE(MAX(id),0) FROM fills').fetchone()[0]
        working = deepcopy(books)
        conn.execute('SAVEPOINT paired_main_profit_floor')
        try:
            result = super().allocate(conn, state, timestamp, day, prices, probs, working, target, case)
            planned = [dict(r) for r in conn.execute(
                'SELECT outcome,cost,shares FROM fills WHERE id>? ORDER BY id', (before_id,))]
            if not planned:
                conn.execute('RELEASE SAVEPOINT paired_main_profit_floor')
                return result
            after = main_profit(conn, day, main, self.exit_price, books)
            accepted = before is not None and after is not None and after + EPS >= before
            if not accepted:
                conn.execute('ROLLBACK TO SAVEPOINT paired_main_profit_floor')
            ladder._event(conn, timestamp, day, 'MAIN_PROFIT_FLOOR_CHECK',
                          {'policy': POLICY, 'case': case, 'main': main, 'protection': protection,
                           'exit_price': self.exit_price, 'before_main_net_pnl': before,
                           'after_main_net_pnl': after, 'accepted': accepted,
                           'planned_fills': planned, 'reason': 'profit_preserved' if accepted else
                           'exit_fee_missing_or_invalid' if before is None or after is None else
                           'main_profit_would_decrease'})
        except BaseException:
            conn.execute('ROLLBACK TO SAVEPOINT paired_main_profit_floor')
            conn.execute('RELEASE SAVEPOINT paired_main_profit_floor')
            raise
        conn.execute('RELEASE SAVEPOINT paired_main_profit_floor')
        if accepted:
            books.clear()
            books.update(working)
            return result
