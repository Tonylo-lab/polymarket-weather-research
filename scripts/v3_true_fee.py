"""Research-only execution adapter: realized fills at TRUE per-level book prices.

Fixes two backtest conservatism leaks the static audit found, WITHOUT touching
the real strategy, fee_cap reservation, accounts, or tasks:

  1. Fee charged as worst-case (fee_cap at p=0.5, price-independent) is replaced
     by the ACTUAL per-consumed-level fee (src.paper_execution.fee at each real
     fill price). The worst-case value is retained separately as `reserved_fee`.
  2. gross/vwap taken from the worst consumed ask band (limit) is replaced by the
     TRUE multi-level VWAP of the realized fill. The worst-limit principal is
     retained separately as `reserved_cost`.

Everything else is unchanged and faithful to the live planner:
  * FOK integer quantity grid (quantity_step/amounts), tick legality, min order
    shares, min cost, and the per-price depth-band enumeration are identical to
    live_strategy.planner.executable_quote.
  * Budget feasibility is tested against TRUE realized cost (so sizing reflects
    real economics -- the full strategy path re-runs, not a post-hoc fee add-back).
  * Cash-sufficiency reservation stays conservative: `reserved_cost`/`reserved_fee`
    are exposed for audit; the engine books realized cost/fee into P&L.

No network, no financial side effects. Import-only reuse of live planner helpers.
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

def true_executable_quote(book, *, cost=None, shares=None, side='BUY'):
    """Same FOK quantity selection as planner.executable_quote, but the returned
    economics (gross/fee/cost/net/vwap/consumed) come from the TRUE per-level
    fill. Budget is tested against true cost. reserved_* keep the worst-case."""
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

    def realized(q_raw):
        """True fill economics for q_raw shares (per-level price + per-level fee)."""
        f = ORIGINAL_QUOTE(book, shares=q_raw / UNIT, side=side)
        return f

    selected = None   # (q_raw, true_fill, reserved_principal, reserved_fee, limit_ppm)
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
            fill = realized(q)
            return q, fill, reserved_principal, reserved_fee, price

        hi = ceiling // step
        maximum = candidate(hi)
        if maximum and maximum[0] > previous and (full is None or maximum[0] > full[0]):
            full = maximum
        lo = 0
        while lo < hi:
            mid = (lo + hi + 1) // 2
            value = candidate(mid)
            if value is None:
                hi = mid - 1
                continue
            # Budget test against TRUE realized cost (principal+fee on BUY).
            true_cost = value[1]['cost'] if side == 'BUY' else value[1]['gross']
            if cost is None or true_cost <= float(cost) + 1e-12:
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
                    reserved_cost=0., reserved_fee=0.)
        return base
    q, fill, reserved_principal, reserved_fee, limit_ppm = selected
    reserved_cost = (reserved_principal + (reserved_fee if side == 'BUY' else 0)) / UNIT
    # Return the TRUE realized fill, keeping FOK-exact share count and limit.
    out = dict(fill)
    out['shares'] = q / UNIT
    out['vwap'] = fill['vwap'] if fill['vwap'] is not None else limit_ppm / UNIT
    out['depth_shares'] = base['depth_shares']
    out['depth_cost'] = base['depth_cost']
    out['reserved_cost'] = reserved_cost
    out['reserved_fee'] = reserved_fee / UNIT
    out['limit_price'] = limit_ppm / UNIT
    out['limited'] = ((cost is not None and (fill['cost'] if side == 'BUY' else fill['gross']) < float(cost) - 1e-9)
                      or (shares is not None and q < raw(shares)))
    return out

def true_cost_for_shares(book, shares):
    """TRUE least realized cost to reach a continuous share target (lower bound).
    Mirrors planner.executable_cost_for_shares grid, but costs at real prices."""
    requested = Decimal(str(shares)) * UNIT
    probe.require(requested.is_finite() and requested >= 0, 'INVALID_TARGET_SHARES')
    if not requested:
        return 0.
    required = int(requested.to_integral_value(rounding='ROUND_CEILING'))
    schedule = (book or {}).get('_paper_fee')
    if not isinstance(schedule, dict) or not {'rate', 'exponent'} <= schedule.keys():
        return float('inf')
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
            fill = ORIGINAL_QUOTE(book, shares=quantity / UNIT, side='BUY')
            cost = fill['cost']  # true principal + true per-level fee
            if best is None or cost < best:
                best = cost
        previous = available
    return best if best is not None else float('inf')

def true_executable_minimax(books, names, held, current_cost, cash_available):
    """planner.executable_minimax but with the true per-price fee in the optimizer
    reserve and true realized legs. Keeps the accepted C-gate objective."""
    def true_reserve_fee(quantity, price, schedule):
        return pe.fee(quantity, price, schedule)
    ORIGINAL_SOLVE = planner.ORIGINAL_SOLVE
    with patch.object(pe, 'quote', ORIGINAL_QUOTE), \
         patch.object(pe, 'fee', pe.fee), \
         patch.object(pe, 'ask_cost', side_effect=lambda p, s: p + true_reserve_fee(1, p, s)):
        plan = ORIGINAL_SOLVE(books, names, held, current_cost, cash_available)
    total = 0.
    for name in names:
        amount = plan['allocation'][name]['cost']
        q = true_executable_quote(books[name], cost=amount) if amount > 0 else {'cost': 0., 'shares': 0., 'vwap': None}
        if q['shares'] + 1e-9 < float(books[name].get('min_order_size') or 5):
            q = {'cost': 0., 'shares': 0., 'vwap': None}
        plan['allocation'][name] = {k: q[k] for k in ('cost', 'shares', 'vwap')}
        total += q['cost']
    after = {name: held.get(name, 0) + plan['allocation'][name]['shares'] - current_cost - total for name in names}
    plan.update(after=after, z=min(after.values()), added_capital=total, cash_unused=cash_available - total,
                break_even_feasible=plan['break_even_feasible'] and min(after.values()) >= -1e-7)
    return plan

from src import paired_cash_policy as policy_mod

from paper_strategy import profit_floor

_ORIG_ALLOCATE_ORIGINAL = policy_mod.CashPolicy._allocate_original

def _no_main_only_allocate_original(self, conn, state, timestamp, day, prices, probs, books, target, case):
    held, deployed = ladder._portfolio(conn, day)
    main, protection = state['main'], state['protection']
    pending = conn.execute('SELECT * FROM paper_entry_wait WHERE target_date=?', (day,)).fetchone()
    first_entry = case == 0 and deployed <= cash.EPS
    if not first_entry:
        return _ORIG_ALLOCATE_ORIGINAL(self, conn, state, timestamp, day, prices, probs, books, target, case)
    # First entry: require BOTH legs executable together, else wait (no buy).
    available = max(0., target - deployed)
    p_book, m_book = books.get(protection, {}), books.get(main, {})
    desired = max(0., .9 * target - held.get(protection, 0.))
    p_cost = ladder._cost_for_shares(p_book, desired) if desired > cash.EPS else 0.
    import math as _m
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
    both_ok = (price_sum is not None and price_sum <= 1. + cash.EPS
               and actual_p > 0 and m_quote['shares'] + cash.EPS >= float(m_book.get('min_order_size') or ladder.MIN_ORDER_SHARES))
    if both_ok:
        ladder._allocate(conn, state, timestamp, day, prices, probs, books, target, case)
        conn.execute('DELETE FROM paper_entry_wait WHERE target_date=?', (day,))
        return
    ladder._event(conn, timestamp, day, 'ENTRY_BOTH_LEGS_REQUIRED_WAIT',
                  {'main': main, 'protection': protection, 'price_sum': price_sum,
                   'protection_cost': actual_p, 'policy': 'NO_MAIN_ONLY_FIRST_ENTRY_V1',
                   'reason': 'first_entry_requires_both_legs'})

class ablation:
    """Context manager patching in the no-Main-only first entry."""
    def __enter__(self):
        self._p = patch.object(policy_mod.CashPolicy, '_allocate_original', _no_main_only_allocate_original)
        self._p.start()
        return self
    def __exit__(self, *e):
        self._p.stop()
        return False
