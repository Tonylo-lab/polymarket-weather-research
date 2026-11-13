"""Finite legal-quantity search inside the actual available budget.

Prices and fees can make returns nonmonotone across depth/limit-price bands.
No whole-book feasibility rejection or global binary search is used.
"""

from bisect import bisect_left

from dataclasses import dataclass, asdict

from decimal import Decimal, ROUND_CEILING

import math

from live_strategy.planner import raw

from live_test.fok_precision import quantity_step, amounts

from live_test.live_gateway import fee_cap

from src.paper_execution import fee

from src.polymarket_client import orderbook_levels

EPS = 1e-8

UNIT = 1_000_000

MIN_BUY_PRINCIPAL_RAW = UNIT

@dataclass(frozen=True)
class Leg:
    shares: float = 0.
    cost: float = 0.
    price: float = 0.

def schedule(book):
    value = (book or {}).get('_paper_fee')
    if not isinstance(value, dict):
        raise ValueError('missing_fee_schedule')
    rate, exponent = float(value['rate']), float(value['exponent'])
    if not (math.isfinite(rate+exponent) and 0 <= rate <= 1 and 1 <= exponent <= 10):
        raise ValueError('invalid_fee_schedule')
    return value

def exit_net(shares, book, exit_price=.998):
    if shares <= EPS:
        return 0.
    return shares*exit_price-fee(shares, exit_price, schedule(book))

def candidates(book, budget):
    """Every legal quantity in each depth band, with worst-limit BUY reserves."""
    terms = schedule(book)
    if not math.isfinite(budget) or budget < 0:
        raise ValueError('invalid_budget')
    rate, exponent = Decimal(str(terms['rate'])), Decimal(str(terms['exponent']))
    tick = raw(book.get('tick_size') or '.001')
    if tick <= 0:
        raise ValueError('invalid_tick')
    minimum = int((Decimal(str(book.get('min_order_size') or 5))*UNIT).to_integral_value(rounding=ROUND_CEILING))
    cap = raw(budget)
    result = [Leg()]
    previous, cumulative = 0, Decimal(0)
    for price, size in orderbook_levels(book, 'BUY'):
        if not (math.isfinite(price+size) and 0 < price < 1 and size > 0):
            continue
        cumulative += Decimal(str(size))
        available, limit = raw(cumulative), raw(price)
        if limit % tick:
            previous = available
            continue
        step = quantity_step('BUY', limit)
        notional_minimum = (MIN_BUY_PRINCIPAL_RAW*UNIT+limit-1)//limit
        start = (max(minimum, previous+1, notional_minimum)+step-1)//step
        principal_step = step*limit//UNIT
        end = min(available//step, cap//max(1, principal_step))
        for n in range(start, end+1):
            quantity = n*step
            principal, _ = amounts('BUY', quantity, limit)
            total = principal+(fee_cap(quantity, rate, exponent) if rate else 0)
            if total > cap:
                break
            result.append(Leg(quantity/UNIT, total/UNIT, limit/UNIT))
            if len(result) > 200_000:
                raise ValueError('candidate_search_limit')
        previous = available
    return result

def minimum_main_spend(book, held, cost, budget, *, recovered=0., exit_price=.998):
    """Smallest affordable legal BUY attaining net zero at the exit assumption."""
    options = candidates(book, budget)
    valid = [x.cost for x in options if recovered+exit_net(held+x.shares, book, exit_price)-cost-x.cost >= -EPS]
    return min(valid) if valid else math.inf

def solve(books, main, protection, held, cost, recovered, budget, bankroll, *,
          main_floor, protection_loss=.02, legacy_loss=.05, exit_price=.998,
          recovery=False, allow_main_reduction=False):
    """Maximize Main net return subject to Protection and legacy scenario floors.

For a fixed Main quantity, the cheapest Protection satisfying its floor also
maximizes Main PnL. A prefix maximum over Protection's exit-value-minus-cost
finds that quantity without assuming its returns are monotone across bands.
Recovery first selects the cheapest full repair, then a strictly improving
partial repair if full repair is impossible inside the same risk constraints.
"""
    if main == protection or bankroll <= 0 or not all(math.isfinite(x) for x in (cost,recovered,budget,bankroll,main_floor)):
        raise ValueError('invalid_scenario_inputs')
    mbook, pbook = books[main], books[protection]
    ml, pl = candidates(mbook, budget), candidates(pbook, budget)
    mheld, pheld = held.get(main, 0.), held.get(protection, 0.)
    before_m = recovered+exit_net(mheld, mbook, exit_price)-cost
    before_p = recovered+exit_net(pheld, pbook, exit_price)-cost
    pminimum = float(pbook.get('min_order_size') or 5)
    mminimum = float(mbook.get('min_order_size') or 5)
    pl = sorted((x for x in pl if pheld+x.shares+EPS >= pminimum), key=lambda x:(x.cost,-x.shares))
    gains, prefix, best_gain = [], [], -math.inf
    for leg in pl:
        gain = exit_net(pheld+leg.shares, pbook, exit_price)-leg.cost
        gains.append(gain)
        best_gain = max(best_gain, gain)
        prefix.append(best_gain)
    # Former protections remain real claims. Repairs/switches may not spend
    # through their original 5%-of-opening-bankroll scenario limit either.
    total_cap = math.inf
    for name, quantity in held.items():
        if quantity > EPS and name not in (main, protection):
            total_cap = min(total_cap, recovered+exit_net(quantity, books.get(name), exit_price)+legacy_loss*bankroll)
    full, partial = None, None
    for m in ml:
        if mheld+m.shares+EPS < mminimum:
            continue
        required = cost+m.cost-recovered-protection_loss*bankroll
        index = bisect_left(prefix, required-EPS)
        if index >= len(pl):
            continue
        p = pl[index]
        spend, total = m.cost+p.cost, cost+m.cost+p.cost
        if spend <= EPS or spend > budget+EPS or total > total_cap+EPS:
            continue
        main_pnl = recovered+exit_net(mheld+m.shares, mbook, exit_price)-total
        protection_pnl = recovered+exit_net(pheld+p.shares, pbook, exit_price)-total
        if protection_pnl < -protection_loss*bankroll-EPS:
            continue
        plan = {'main':asdict(m), 'protection':asdict(p), 'added_cost':spend,
                'main_pnl':main_pnl, 'protection_pnl':protection_pnl,
                'before_main_pnl':before_m, 'before_protection_pnl':before_p,
                'main_floor':main_floor, 'protection_floor':-protection_loss*bankroll,
                'legacy_total_cost_cap':total_cap if math.isfinite(total_cap) else None,
                'budget':budget, 'partial':False, 'exit_price':exit_price}
        if main_pnl >= main_floor-EPS:
            if not recovery and not allow_main_reduction and main_pnl <= before_m+EPS:
                continue
            rank = (-spend,main_pnl) if recovery else (main_pnl,-spend)
            if full is None or rank > full[0]:
                full = rank, plan
        elif recovery and main_pnl > before_m+EPS and min(main_pnl, protection_pnl) > min(before_m,before_p)+EPS:
            plan['partial'] = True
            rank = (main_pnl,min(main_pnl,protection_pnl),-spend)
            if partial is None or rank > partial[0]:
                partial = rank, plan
    selected = full or partial
    return selected[1] if selected else None
