"""v4 research execution adapter: LIVE reservation gate + TRUE realized economics.

Supersedes scripts/v3_true_fee.py. The v3 adapter fixed the fee/VWAP bug but
introduced a NEW modeling error the user flagged: it chose the order quantity by
testing the TRUE realized cost against the budget. Live `executable_quote`
instead gates on the RESERVED cost -- worst-consumed-limit principal + fee_cap
(worst-case fee at p=0.5). Realized cost is lower, so the v3 adapter could submit
orders the live budget rule would REJECT, overstating participation/feasibility.

v4 separates the two phases faithfully:

  PRE-TRADE (feasibility, identical to live planner.executable_quote):
    choose the largest FOK-legal quantity whose RESERVED cost
    (reserved_principal at the worst consumed limit + fee_cap) fits the budget,
    honoring tick legality, min order shares, min cost and per-price depth bands.
    An order whose reservation does not fit is NOT submitted -- even if its true
    realized cost would.

  POST-TRADE (economics booked into P&L):
    gross / vwap / fee / cost / net / consumed come from the TRUE per-level fill
    (src.paper_execution.quote at real prices). The unused reservation
    (reserved_cost - realized_cost) is surfaced as `reservation_released` so the
    replay can return it to available cash after the fill.

So: feasibility uses the conservative reservation live uses; P&L uses the real
fill; the gap is released, never booked as cost. reserved_* are audit fields.

No network, no financial side effects. Does NOT touch live fee_cap, the real
strategy, accounts, or tasks. Import-only reuse of live planner helpers.
"""

from copy import deepcopy

from decimal import Decimal

from unittest.mock import patch

from live_strategy import planner

from live_test import probe, live_gateway as lg

from live_test.fok_precision import quantity_step, amounts

from src import paired_cash as cash, paired_ladder as ladder, paper_execution as pe

from src.polymarket_client import orderbook_levels

UNIT = planner.UNIT

raw = planner.raw

ORIGINAL_QUOTE = pe.quote  # true per-level fill + true per-level fee

def gated_executable_quote(book, *, cost=None, shares=None, side='BUY'):
    """Live reserved-cost feasibility gate, true realized economics booked.

    Selection is byte-for-byte the live `planner.executable_quote` rule: the
    budget test is `reserved_principal + fee_cap <= raw(cost)` (BUY). The fill
    returned carries TRUE per-level gross/fee/vwap/cost/net/consumed, plus
    reserved_cost/reserved_fee (what live reserved) and reservation_released
    (reserved_cost - realized_cost) for the caller to credit back to cash.
    """
    book = deepcopy(book)
    schedule = book['_paper_fee']
    levels = orderbook_levels(book, 'BUY' if side == 'BUY' else 'BID')
    levels = [(p, q) for p, q in levels if 0 < p < 1 or (side == 'SELL' and p == 1)]
    tick = Decimal(str(book.get('tick_size') or '.001'))
    tick_raw = raw(tick)
    probe.require(tick_raw > 0, 'INVALID_TICK')
    book['asks' if side == 'BUY' else 'bids'] = [{'price': str(p), 'size': str(q)} for p, q in levels]
    base = ORIGINAL_QUOTE(book, shares=0, side=side)
    depth = sum(q for p, q in levels)
    rate, exponent = Decimal(str(schedule['rate'])), Decimal(str(schedule['exponent']))

    selected = None   # (q_raw, limit_ppm, reserved_principal, reserved_fee)
    full = None
    cumulative = Decimal(0)
    previous = 0
    for p, size in levels:
        cumulative += Decimal(str(size))
        available = raw(cumulative)
        price = raw(p)
        if price == UNIT:
            price = UNIT - tick_raw
        if not 0 < price < UNIT or price % tick_raw:
            previous = available
            continue
        step = quantity_step(side, price)
        ceiling = min(available, raw(shares)) if shares is not None else available

        def candidate(n):
            q = n * step
            if not q:
                return None
            maker, taker = amounts(side, q, price)
            reserved_principal = maker if side == 'BUY' else taker
            reserved_fee = lg.fee_cap(q, rate, exponent) if rate else 0
            return q, price, reserved_principal, reserved_fee

        hi = ceiling // step
        maximum = candidate(hi)
        if maximum and maximum[0] > previous and (full is None or maximum[0] > full[0]):
            full = maximum
        lo = 0
        while lo < hi:
            mid = (lo + hi + 1) // 2
            value = candidate(mid)
            # LIVE RULE: gate on reserved principal + fee_cap, not realized cost.
            if cost is None or value[2] + (value[3] if side == 'BUY' else 0) <= raw(cost):
                lo = mid
            else:
                hi = mid - 1
        value = candidate(lo)
        if value and value[0] > previous and (selected is None or value[0] > selected[0]):
            selected = value
        previous = available

    base.update(depth_shares=depth,
                depth_cost=(full[2] + (full[3] if side == 'BUY' else 0)) / UNIT if full else 0.)
    if selected is None:
        base.update(limited=bool((cost or 0) > 0 or (shares or 0) > 0),
                    reserved_cost=0., reserved_fee=0., reservation_released=0.)
        return base
    q, limit_ppm, reserved_principal, reserved_fee = selected
    # Realized fill at TRUE per-level prices for the SAME FOK quantity.
    fill = ORIGINAL_QUOTE(book, shares=q / UNIT, side=side)
    probe.require(fill['shares'] + 1e-8 >= q / UNIT, 'FOK_QUANTITY_EXCEEDS_DEPTH')
    reserved_cost = (reserved_principal + (reserved_fee if side == 'BUY' else 0)) / UNIT
    realized_cost = fill['cost'] if side == 'BUY' else fill['gross']
    out = dict(fill)
    out['shares'] = q / UNIT
    out['vwap'] = fill['vwap'] if fill['vwap'] is not None else limit_ppm / UNIT
    out['depth_shares'] = base['depth_shares']
    out['depth_cost'] = base['depth_cost']
    out['limit_price'] = limit_ppm / UNIT
    out['reserved_cost'] = reserved_cost
    out['reserved_fee'] = reserved_fee / UNIT
    out['reservation_released'] = max(0., reserved_cost - realized_cost) if side == 'BUY' else 0.
    # `limited` reflects the LIVE feasibility test (reservation vs budget).
    out['limited'] = ((cost is not None and reserved_principal + (reserved_fee if side == 'BUY' else 0) < raw(cost) - 10)
                      or (shares is not None and q < raw(shares)))
    return out

def gated_cost_for_shares(book, shares):
    """Least RESERVED cost to reach a continuous share target -- identical to
    live planner.executable_cost_for_shares (reserved principal + fee_cap). The
    strategy sizes against reservation; realized cost is applied at fill time."""
    requested = Decimal(str(shares)) * UNIT
    probe.require(requested.is_finite() and requested >= 0, 'INVALID_TARGET_SHARES')
    if not requested:
        return 0.
    required = int(requested.to_integral_value(rounding='ROUND_CEILING'))
    schedule = (book or {}).get('_paper_fee')
    if not isinstance(schedule, dict) or not {'rate', 'exponent'} <= schedule.keys():
        return float('inf')
    rate = Decimal(str(schedule['rate']))
    exponent = Decimal(str(schedule['exponent']))
    tick = raw(book.get('tick_size') or '.001')
    probe.require(tick > 0, 'INVALID_TICK')
    cumulative = Decimal(0)
    previous = 0
    best = None
    for price, size in orderbook_levels(book, 'BUY'):
        if not 0 < price < 1:
            continue
        cumulative += Decimal(str(size))
        available = raw(cumulative)
        limit = raw(price)
        if not 0 < limit < UNIT or limit % tick:
            previous = available
            continue
        step = quantity_step('BUY', limit)
        quantity = ((max(required, previous + 1) + step - 1) // step) * step
        if quantity <= available:
            principal, _ = amounts('BUY', quantity, limit)
            cost = principal + (lg.fee_cap(quantity, rate, exponent) if rate else 0)
            if best is None or cost < best:
                best = cost
        previous = available
    return best / UNIT if best is not None else float('inf')

def gated_executable_minimax(books, names, held, current_cost, cash_available):
    """planner.executable_minimax, but realized legs use the gated quote (live
    reservation gate + true realized economics). Optimizer reserve stays the
    live worst-case so minimax sizes against the same reservation live uses."""
    def reserve_fee(quantity, price, schedule):
        return quantity * float(schedule['rate']) * (.25 ** float(schedule['exponent']))
    ORIGINAL_SOLVE = planner.ORIGINAL_SOLVE
    with patch.object(pe, 'quote', ORIGINAL_QUOTE), \
         patch.object(pe, 'fee', reserve_fee), \
         patch.object(pe, 'ask_cost', side_effect=lambda p, s: p + reserve_fee(1, p, s)):
        plan = ORIGINAL_SOLVE(books, names, held, current_cost, cash_available)
    total = 0.
    for name in names:
        amount = plan['allocation'][name]['cost']
        q = gated_executable_quote(books[name], cost=amount) if amount > 0 else {'cost': 0., 'shares': 0., 'vwap': None}
        if q['shares'] + 1e-9 < float(books[name].get('min_order_size') or 5):
            q = {'cost': 0., 'shares': 0., 'vwap': None}
        plan['allocation'][name] = {k: q[k] for k in ('cost', 'shares', 'vwap')}
        total += q['cost']
    after = {name: held.get(name, 0) + plan['allocation'][name]['shares'] - current_cost - total for name in names}
    plan.update(after=after, z=min(after.values()), added_capital=total, cash_unused=cash_available - total,
                break_even_feasible=plan['break_even_feasible'] and min(after.values()) >= -1e-7)
    return plan

from src import paired_cash_policy as policy_mod

_ORIG_ALLOCATE_ORIGINAL = policy_mod.CashPolicy._allocate_original

def _no_main_only_allocate_original(self, conn, state, timestamp, day, prices, probs, books, target, case):
    """First entry requires BOTH legs executable under the LIVE reservation gate,
    else buy nothing and wait. Uses gated_cost_for_shares/_quote so feasibility
    matches live. Everything else (ladders, flips, recovery, exits) unchanged."""
    held, deployed = ladder._portfolio(conn, day)
    main, protection = state['main'], state['protection']
    first_entry = case == 0 and deployed <= cash.EPS
    if not first_entry:
        return _ORIG_ALLOCATE_ORIGINAL(self, conn, state, timestamp, day, prices, probs, books, target, case)
    available = max(0., target - deployed)
    p_book, m_book = books.get(protection, {}), books.get(main, {})
    import math as _m
    desired = max(0., .9 * target - held.get(protection, 0.))
    p_cost = ladder._cost_for_shares(p_book, desired) if desired > cash.EPS else 0.
    if not _m.isfinite(p_cost):
        p_cost = ladder._quote(p_book)['depth_cost']
    if 0 < p_cost < ladder._min_cost(p_book) <= available + cash.EPS:
        p_cost = ladder._min_cost(p_book)
    p_quote = ladder._quote(p_book, cost=min(available, p_cost))
    p_min = float(p_book.get('min_order_size') or ladder.MIN_ORDER_SHARES)
    actual_p = p_quote['cost'] if p_quote['shares'] + cash.EPS >= p_min else 0.
    m_quote = ladder._quote(m_book, cost=max(0., available - actual_p))
    m_price, p_price = m_quote['vwap'], p_quote['vwap']
    price_sum = m_price + p_price if m_price is not None and p_price is not None else None
    both_ok = (price_sum is not None and price_sum <= 1. + cash.EPS and actual_p > 0
               and m_quote['shares'] + cash.EPS >= float(m_book.get('min_order_size') or ladder.MIN_ORDER_SHARES))
    if both_ok:
        ladder._allocate(conn, state, timestamp, day, prices, probs, books, target, case)
        conn.execute('DELETE FROM paper_entry_wait WHERE target_date=?', (day,))
        return
    ladder._event(conn, timestamp, day, 'ENTRY_BOTH_LEGS_REQUIRED_WAIT',
                  {'main': main, 'protection': protection, 'price_sum': price_sum,
                   'protection_cost': actual_p, 'policy': 'NO_MAIN_ONLY_FIRST_ENTRY_V1'})

class no_main_only:
    def __enter__(self):
        self._p = patch.object(policy_mod.CashPolicy, '_allocate_original', _no_main_only_allocate_original)
        self._p.start(); return self
    def __exit__(self, *e):
        self._p.stop(); return False
