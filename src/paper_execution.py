"""Fee-aware paper fills from observed depth; never signs or submits orders."""

from __future__ import annotations

import math

from src.polymarket_client import orderbook_levels

EPS = 1e-9

def fee(shares, price, schedule):
    rate = float(schedule['rate'])
    exponent = float(schedule['exponent'])
    if not (math.isfinite(rate) and 0 <= rate <= 1 and
            math.isfinite(exponent) and exponent >= 1):
        raise ValueError('Invalid or unsupported market fee schedule')
    return round(shares * rate * (price * (1 - price)) ** exponent, 5)

def ask_cost(price, schedule):
    return price + float(schedule['rate']) * (price * (1 - price)) ** float(schedule['exponent'])

def quote(book, *, cost=None, shares=None, side='BUY'):
    schedule = book['_paper_fee']
    bids = [(p, q) for p, q in orderbook_levels(book, 'BID') if math.isfinite(p + q) and p <= 1]
    asks = [(p, q) for p, q in orderbook_levels(book, 'BUY') if math.isfinite(p + q) and p <= 1]
    levels = asks if side == 'BUY' else bids
    remaining_cost = max(0., cost) if cost is not None else math.inf
    remaining_shares = max(0., shares) if shares is not None else math.inf
    gross = fees = quantity = 0.
    consumed = []
    for price, depth in levels:
        take = min(depth, remaining_shares, remaining_cost / price)
        if side == 'BUY' and cost is not None and take*price+fee(take, price, schedule) > remaining_cost+1e-12:
            lo, hi = 0., take
            for _ in range(45):
                mid = (lo+hi)/2
                if mid*price+fee(mid, price, schedule) <= remaining_cost+1e-12:
                    lo = mid
                else:
                    hi = mid
            take = lo
        charge = fee(take, price, schedule)
        spent = take * price + (charge if side == 'BUY' else 0.)
        if take > EPS:
            consumed.append((price, take))
            gross += take * price
            fees += charge
            quantity += take
        remaining_cost -= spent
        remaining_shares -= take
        if remaining_cost <= EPS or remaining_shares <= EPS:
            break
    bid, ask = (bids[0][0] if bids else None), (asks[0][0] if asks else None)
    return {'best_bid': bid, 'best_ask': ask,
            'spread': ask - bid if bid is not None and ask is not None else None,
            'depth_shares': sum(q for _, q in levels),
            'depth_cost': sum(p*q + (fee(q, p, schedule) if side == 'BUY' else 0.) for p, q in levels),
            'cost': gross + fees if side == 'BUY' else gross,
            'gross': gross, 'fee': fees, 'net': gross - fees,
            'shares': quantity, 'vwap': gross / quantity if quantity > EPS else None,
            'limited': ((cost is not None and remaining_cost > .00001) or
                        (shares is not None and remaining_shares > EPS)),
            'consumed': consumed}
