"""Three-outcome, depth-aware BUY-only handover optimization.

For a proposed worst settlement PnL z and total new cost t, each leg needs
max(0, C0 + t + z - held_shares) new shares. The cheapest executable cost of
those shares is piecewise linear in t. A z is feasible exactly when the sum
of these three costs is at most t for some t within the cash limit. We scan
all orderbook breakpoints, including minimum-order boundaries, then bisect z.
"""

from __future__ import annotations

import math

from src.market_only import execution

from src.polymarket_client import orderbook_levels

TOL = 1e-7

def _leg(book):
    levels = orderbook_levels(book or {}, "BUY")
    raw_levels = levels
    if book and '_paper_fee' in book:
        from src.paper_execution import ask_cost
        levels = [(ask_cost(p, book['_paper_fee']), q) for p, q in levels]
    minimum = float((book or {}).get("min_order_size") or 5.0)
    depth = sum(q for _, q in levels)
    return {"levels": levels, "minimum_shares": minimum, "depth_shares": depth,
            "best_ask": levels[0][0] if levels else None,
            "depth_cost": sum(p*q for p, q in levels),
            **({'raw_levels': raw_levels, 'fee_schedule': book['_paper_fee']}
               if book and '_paper_fee' in book else {})}

def _cost(leg, required):
    if required <= TOL:
        return 0.0
    required = max(required, leg["minimum_shares"])
    if required > leg["depth_shares"] + TOL:
        return math.inf
    remaining = required
    cost = 0.0
    for price, size in leg.get('raw_levels', leg['levels']):
        take = min(size, remaining)
        cost += price*take
        if 'fee_schedule' in leg:
            from src.paper_execution import fee
            cost += fee(take, price, leg['fee_schedule'])
        remaining -= take
        if remaining <= TOL:
            break
    return cost

def solve(books: dict, names: tuple[str, str, str], held: dict[str, float],
          current_cost: float, cash: float) -> dict:
    """Return minimum-cost break-even, else maximin with minimum-cost tie break."""
    cash = max(0.0, cash)
    legs = {name: _leg(books.get(name)) for name in names}
    before = {name: held.get(name, 0.0)-current_cost for name in names}

    def required_cost(t, z):
        return {name: _cost(legs[name], current_cost+t+z-held.get(name, 0.0))
                for name in names}

    def feasible(z):
        points = {0.0, cash}
        for name in names:
            leg = legs[name]
            shift = held.get(name, 0.0)-current_cost-z
            cumulative = 0.0
            for share_boundary in (0.0, leg["minimum_shares"]):
                p = shift+share_boundary
                if 0 < p < cash:
                    points.add(p)
            for _, size in leg["levels"]:
                cumulative += size
                p = shift+cumulative
                if 0 < p < cash:
                    points.add(p)
        ordered = sorted(points)
        candidates = set(ordered)
        for left, right in zip(ordered, ordered[1:]):
            if right-left <= 1e-10:
                continue
            a = left+(right-left)*1e-5
            b = right-(right-left)*1e-5
            fa = sum(required_cost(a, z).values())-a
            fb = sum(required_cost(b, z).values())-b
            if math.isfinite(fa) and math.isfinite(fb):
                if abs(fb-fa) > 1e-12:
                    root = a-fa*(b-a)/(fb-fa)
                    if left <= root <= right:
                        candidates.add(root)
                if fa <= TOL:
                    candidates.add(a)
                if fb <= TOL:
                    candidates.add(b)
        for t in sorted(candidates):
            costs = required_cost(t, z)
            actual = sum(costs.values())
            if math.isfinite(actual) and actual <= t+TOL and actual <= cash+TOL:
                return costs
        return None

    breakeven = feasible(0.0)
    if breakeven is not None:
        costs = breakeven
        objective = "MIN_CAPITAL_BREAK_EVEN"
        minimum_capital = sum(costs.values())
    else:
        lower = min(before.values())
        # No outcome can receive more than all displayed shares, and all costs
        # are nonnegative. This is a finite upper bound on achievable Z.
        upper = min(held.get(name, 0.0)+legs[name]["depth_shares"]-current_cost
                    for name in names)
        best = {name: 0.0 for name in names}
        for _ in range(43):
            mid = (lower+upper)/2
            candidate = feasible(mid)
            if candidate is None:
                upper = mid
            else:
                lower, best = mid, candidate
        costs = best
        objective = "MINIMAX"
        minimum_capital = None
    proposed = {}
    total = 0.0
    for name in names:
        cost = costs[name]
        book = books.get(name) or {}
        if '_paper_fee' in book:
            from src.paper_execution import quote as paper_quote
            quote = paper_quote(book, cost=cost)
        else:
            quote = execution(book, requested_cost=cost)
        if cost > TOL and (quote["cost"]+TOL < cost or
                           quote["shares"]+TOL < legs[name]["minimum_shares"]):
            raise AssertionError("Optimizer proposed an unexecutable order")
        proposed[name] = {"cost": quote["cost"], "shares": quote["shares"],
                          "vwap": quote["vwap"]}
        total += quote["cost"]
    after = {name: held.get(name, 0.0)+proposed[name]["shares"]-current_cost-total
             for name in names}
    return {"objective": objective, "break_even_feasible": breakeven is not None,
            "minimum_break_even_capital": minimum_capital,
            "z": min(after.values()), "before": before, "after": after,
            "allocation": proposed, "added_capital": total,
            "cash_unused": cash-total, "orderbooks": legs}
