"""Independent rolling-bankroll Market paired ladder paper account."""

from __future__ import annotations

import hashlib

import inspect

import json

import math

import re

from contextlib import closing

from datetime import datetime

from functools import lru_cache

from pathlib import Path

from src import handover_minimax

from src.market_only import (DATA_DIR, EPS, HKT, connect, consume_book, execution,
                             make_schema, position, record_order, save_exposure)

from src.handover_minimax import solve as solve_handover

STRATEGY = "MARKET_PAIRED_LADDER_V2_1"

INITIAL_BANKROLL = 50.0

MIN_ORDER_SHARES = 5.0

TIERS = ((.60, .20, .70), (.70, .35, .80), (.80, .50, .90), (.90, .60, 1.0))

MODES = ("NORMAL_PAIRED", "CASE_1_COVERED_SWAP", "CASE_2_FULL_RECOVERY",
         "CASE_3_PARTIAL_COVERAGE", "PROTECTION_HANDOVER_MINIMAX")

LEGACY_FINGERPRINT = "a565a6888be5fcb575e281f899544f77e5bf67e7ae98df4bf65b65f7504f5ed7"

HANDOVER_DISABLED = "DISABLED"

HANDOVER_C = "C"

HANDOVER_RESEARCH = "UNCONDITIONAL_RESEARCH"

HANDOVER_POLICIES = (HANDOVER_DISABLED, HANDOVER_C, HANDOVER_RESEARCH)

HANDOVER_C_MAIN_CUTOFF = .80

HANDOVER_C_NEW_MIN = .10

HANDOVER_C_GAP = .03

HANDOVER_C_LOSS_LIMIT = .01

HANDOVER_C_TAIL_MIN = .90

def path(directory: Path = DATA_DIR) -> Path:
    return directory / f"{STRATEGY}.db"

def _schema(conn, handover_enabled=False):
    make_schema(conn)
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS paired_state(
        target_date TEXT PRIMARY KEY, bankroll_before REAL NOT NULL,
        main TEXT, protection TEXT, mode TEXT NOT NULL DEFAULT 'NORMAL_PAIRED',
        highest_tier INTEGER NOT NULL DEFAULT 0, pending_tier INTEGER NOT NULL DEFAULT 0,
        tier_probability_seen INTEGER NOT NULL DEFAULT 0,
        main_history TEXT NOT NULL DEFAULT '[]',
        protection_history TEXT NOT NULL DEFAULT '[]', protection_switches INTEGER NOT NULL DEFAULT 0,
        main_flips INTEGER NOT NULL DEFAULT 0, case1_count INTEGER NOT NULL DEFAULT 0,
        case2_count INTEGER NOT NULL DEFAULT 0, case3_count INTEGER NOT NULL DEFAULT 0,
        recovery_used REAL NOT NULL DEFAULT 0, recovery_efficiency REAL,
        break_even_feasible INTEGER, high_price_unrecoverable INTEGER NOT NULL DEFAULT 0,
        recovery_info TEXT NOT NULL DEFAULT '{}');
      CREATE TABLE IF NOT EXISTS paired_events(
        id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, target_date TEXT NOT NULL,
        event TEXT NOT NULL, details TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS settlement_maps(
        id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, target_date TEXT NOT NULL,
        phase TEXT NOT NULL, mode TEXT NOT NULL, map_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS bankroll_ledger(
        target_date TEXT PRIMARY KEY, bankroll_before_target REAL NOT NULL,
        total_deployed REAL NOT NULL, winner TEXT NOT NULL, realized_pnl REAL NOT NULL,
        bankroll_after_target REAL NOT NULL, roi REAL NOT NULL, cumulative_return REAL NOT NULL,
        peak_bankroll REAL NOT NULL, drawdown REAL NOT NULL, max_drawdown REAL NOT NULL,
        max_deployed_pct REAL NOT NULL, metrics_json TEXT NOT NULL);
    """)
    if "tier_probability_seen" not in {row[1] for row in conn.execute("PRAGMA table_info(paired_state)")}:
        conn.execute("ALTER TABLE paired_state ADD COLUMN tier_probability_seen INTEGER NOT NULL DEFAULT 0")
    if handover_enabled:
        conn.execute("""CREATE TABLE IF NOT EXISTS handover_audit(
            id INTEGER PRIMARY KEY, target_date TEXT NOT NULL, timestamp TEXT NOT NULL,
            details TEXT NOT NULL, final_winner TEXT, realized_pnl REAL)""")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(paired_state)")}
        if "handover_completed" not in columns:
            conn.execute("ALTER TABLE paired_state ADD COLUMN handover_completed INTEGER NOT NULL DEFAULT 0")
        if "handover_pending" not in columns:
            conn.execute("ALTER TABLE paired_state ADD COLUMN handover_pending TEXT NOT NULL DEFAULT '{}'")

def _handover_policy(handover_enabled=None, handover_policy=None, existing=None):
    if handover_policy is not None:
        if handover_policy not in HANDOVER_POLICIES:
            raise ValueError(f"Unknown handover policy: {handover_policy}")
        if handover_enabled is not None and bool(handover_enabled) != (handover_policy != HANDOVER_DISABLED):
            raise ValueError("Conflicting handover_enabled and handover_policy")
        return handover_policy
    # Keep the historical experiment flag reproducible; production uses C.
    if handover_enabled is not None:
        return HANDOVER_RESEARCH if handover_enabled else HANDOVER_DISABLED
    if existing is not None:
        return _handover_policy(handover_policy=existing["handover_policy"]) if "handover_policy" in existing else (
            HANDOVER_RESEARCH if existing.get("handover_enabled") else HANDOVER_DISABLED)
    return HANDOVER_C

@lru_cache(maxsize=6)
def logic_fingerprint(handover_enabled=None, *, handover_policy=None) -> str:
    policy = _handover_policy(handover_enabled, handover_policy)
    if policy == HANDOVER_DISABLED:
        return LEGACY_FINGERPRINT
    return hashlib.sha256((inspect.getsource(cycle) + inspect.getsource(_allocate)
                           + inspect.getsource(_recovery) + inspect.getsource(_handover)
                           + inspect.getsource(_handover_c_gate)
                           + inspect.getsource(_executable_handover)
                           + inspect.getsource(_handover_policy)
                           + inspect.getsource(handover_minimax)
                           + repr((TIERS, MIN_ORDER_SHARES, EPS, policy, HANDOVER_C_MAIN_CUTOFF, HANDOVER_C_NEW_MIN,
                                   HANDOVER_C_GAP, HANDOVER_C_LOSS_LIMIT, HANDOVER_C_TAIL_MIN)))
                          .encode()).hexdigest()

def initialize(directory: Path = DATA_DIR, now: datetime | None = None,
               handover_enabled: bool | None = None, handover_policy: str | None = None,
               initial_bankroll: float | None = None) -> dict:
    if initial_bankroll is not None and (not math.isfinite(initial_bankroll) or initial_bankroll <= 0):
        raise ValueError('Initial bankroll must be positive and finite')
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "paired_ladder_epoch.json"
    existing = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else None
    policy = _handover_policy(handover_enabled, handover_policy, existing)
    handover_enabled = policy != HANDOVER_DISABLED
    fingerprint = logic_fingerprint(handover_policy=policy)
    if marker.exists():
        epoch = existing
        if initial_bankroll is not None and epoch['initial_bankroll'] != initial_bankroll:
            raise RuntimeError('Initial bankroll differs from the persisted paired epoch')
        if epoch["fingerprint"] != fingerprint:
            raise RuntimeError("Paired ladder logic changed; create a clean paired epoch")
    else:
        now = (now or datetime.now(HKT)).astimezone(HKT)
        epoch = {"epoch_id": "MARKET_PAIRED_LADDER_V2_1_" + now.strftime("%Y%m%dT%H%M%S%f%z"),
                 "activation_timestamp": now.isoformat(), "fingerprint": fingerprint,
                 "initial_bankroll": INITIAL_BANKROLL if initial_bankroll is None else initial_bankroll, "strategy": STRATEGY,
                 "handover_enabled": handover_enabled, "handover_policy": policy}
        marker.write_text(json.dumps(epoch, indent=2), encoding="utf-8")
    with closing(connect(path(directory))) as conn, conn:
        _schema(conn, handover_enabled)
        conn.execute("INSERT OR IGNORE INTO epoch VALUES (?,?)", (epoch["epoch_id"], json.dumps(epoch)))
    return epoch

def _rank(outcomes: dict[str, float]) -> list[str]:
    def key(name):
        match = re.search(r"\d+(?:\.\d+)?", name)
        number = float(match.group()) if match else 0.0
        return number + (-.5 if "below" in name.lower() else .5 if "above" in name.lower() else 0)
    return sorted(outcomes, key=key)

def _protection(probs: dict[str, float], main: str) -> str:
    names = _rank(probs)
    index = names.index(main)
    adjacent = names[max(0, index - 1):index] + names[index + 1:index + 2]
    return max(adjacent, key=lambda name: probs[name])

def _tier(probability: float) -> int:
    return sum(probability + EPS >= threshold for threshold, _, _ in TIERS)

def _portfolio(conn, day):
    rows = conn.execute("SELECT outcome,shares,total_cost FROM positions WHERE target_date=?", (day,)).fetchall()
    return {r["outcome"]: r["shares"] for r in rows}, sum(r["total_cost"] for r in rows)

def _map(conn, day, outcomes):
    shares, cost = _portfolio(conn, day)
    return {name: shares.get(name, 0.0) - cost for name in outcomes}

def _event(conn, timestamp, day, event, details):
    conn.execute("INSERT INTO paired_events(timestamp,target_date,event,details) VALUES (?,?,?,?)",
                 (timestamp, day, event, json.dumps(details, sort_keys=True)))

def _snapshot_map(conn, timestamp, day, phase, mode, outcomes):
    conn.execute("INSERT INTO settlement_maps(timestamp,target_date,phase,mode,map_json) VALUES (?,?,?,?,?)",
                 (timestamp, day, phase, mode, json.dumps(_map(conn, day, outcomes), sort_keys=True)))

def _quote(book, *, cost=None, shares=None):
    if book and '_paper_fee' in book:
        from src.paper_execution import quote
        return quote(book, cost=cost, shares=shares)
    return execution(book or {}, requested_cost=cost, requested_shares=shares)

def _cost_for_shares(book, shares):
    quote = _quote(book, shares=shares)
    return quote["cost"] if quote["shares"] + EPS >= shares else float("inf")

def _min_cost(book):
    minimum = float((book or {}).get("min_order_size") or MIN_ORDER_SHARES)
    return _cost_for_shares(book, minimum)

def _buy(conn, timestamp, day, name, budget, books, prices, probs, reason, *, shares=None):
    book = books.get(name, {})
    requested = max(0.0, budget)
    if book.get('_paper_cash'):
        from src.paired_cash import balance
        requested = min(requested, balance(conn))
    fill = _quote(book, cost=requested, shares=shares)
    minimum = float(book.get("min_order_size") or MIN_ORDER_SHARES)
    if fill["shares"] + EPS < minimum:
        _event(conn, timestamp, day, "ORDER_SHORTFALL",
               {"outcome": name, "requested_cost": requested, "executable_cost": fill["cost"],
                "reason": "minimum_order_or_depth"})
        return 0.0
    position(conn, day, name)
    record_order(conn, timestamp=timestamp, day=day, outcome=name, tranche=reason,
                 source=STRATEGY, raw_size=requested, requested_cost=requested,
                 fill=fill, raw_price=prices[name], probability=probs[name])
    if book.get('_paper_cash'):
        from src.paired_cash import debit_buy
        debit_buy(conn, timestamp, day, name, fill, book['_paper_fee'])
    consume_book(book, fill)
    if fill["limited"]:
        _event(conn, timestamp, day, "LIQUIDITY_SHORTFALL",
               {"outcome": name, "requested_cost": requested, "executable_cost": fill["cost"],
                "vwap": fill["vwap"], "shares": fill["shares"]})
    return fill["cost"]

def _main_requirement(main_book, main_shares, current_cost, protection_cost):
    """Smallest executable Main spend keeping its settlement PnL nonnegative."""
    if main_shares >= current_cost + protection_cost - EPS:
        return 0.0
    depth = _quote(main_book)["depth_cost"]
    if depth <= EPS:
        return float("inf")
    if main_shares + _quote(main_book, cost=depth)["shares"] < current_cost + protection_cost + depth - EPS:
        return float("inf")
    lo, hi = 0.0, depth
    for _ in range(55):
        mid = (lo + hi) / 2
        q = _quote(main_book, cost=mid)
        if main_shares + q["shares"] >= current_cost + protection_cost + q["cost"]:
            hi = mid
        else:
            lo = mid
    return hi

def _allocate(conn, state, timestamp, day, prices, probs, books, target, case):
    main, protection = state["main"], state["protection"]
    shares, cost = _portfolio(conn, day)
    available = max(0.0, target - cost)
    if available <= EPS:
        return
    p_book, m_book = books.get(protection, {}), books.get(main, {})
    desired_shares = max(0.0, .9 * target - shares.get(protection, 0))
    p_cost = _cost_for_shares(p_book, desired_shares) if desired_shares > EPS else 0.0
    if p_cost == float("inf"):
        quote = _quote(p_book)
        _event(conn, timestamp, day, "LIQUIDITY_SHORTFALL",
               {"outcome": protection, "requested_shares": desired_shares,
                "executable_shares": quote["depth_shares"], "executable_cost": quote["depth_cost"],
                "vwap": quote["vwap"], "reason": "protection_depth"})
        p_cost = _quote(p_book)["depth_cost"]
    if 0 < p_cost < _min_cost(p_book) <= available + EPS:
        p_cost = _min_cost(p_book)
        _event(conn, timestamp, day, "MIN_ORDER_PROTECTION_OVERRIDE", {"cost": p_cost})
    p_cost = min(available, p_cost)
    if case in (1, 3):
        # Select the most Protection we can afford while maintaining Main >= 0.
        lo, hi = 0.0, p_cost
        for _ in range(45):
            mid = (lo + hi) / 2
            needed = _main_requirement(m_book, shares.get(main, 0), cost, mid)
            if mid + needed <= available + EPS:
                lo = mid
            else:
                hi = mid
        p_cost = lo
        if 0 < p_cost < _min_cost(p_book):
            p_cost = 0.0
        main_needed = _main_requirement(m_book, shares.get(main, 0), cost, p_cost)
        if main_needed > available:
            p_cost = 0.0
            main_needed = available
        if main_needed > EPS:
            minimum = _min_cost(m_book)
            _buy(conn, timestamp, day, main, min(available, max(main_needed, minimum)), books,
                 prices, probs, "MAIN_BREAK_EVEN")
        shares, cost = _portfolio(conn, day)
        remaining = max(0.0, target - cost)
        p_cost = min(p_cost, remaining)
        if shares.get(main, 0.0) + EPS < cost + p_cost:
            p_cost = 0.0
        if p_cost >= _min_cost(p_book) - EPS:
            _buy(conn, timestamp, day, protection, p_cost, books, prices, probs, "PROTECTION")
        shares, cost = _portfolio(conn, day)
        remaining = max(0.0, target - cost)
        # A final Main buy also pays for any protection cost and raises Main profit.
        if remaining >= _min_cost(m_book) - EPS:
            _buy(conn, timestamp, day, main, remaining, books, prices, probs, "MAIN_SURPLUS")
        elif remaining > EPS and _min_cost(m_book) == float("inf"):
            _event(conn, timestamp, day, "ORDER_SHORTFALL",
                   {"outcome": main, "requested_cost": remaining, "executable_cost": 0,
                    "reason": "missing_main_depth"})
    else:
        if p_cost >= _min_cost(p_book) - EPS:
            _buy(conn, timestamp, day, protection, p_cost, books, prices, probs, "PROTECTION")
        shares, cost = _portfolio(conn, day)
        remaining = max(0.0, target - cost)
        if remaining >= _min_cost(m_book) - EPS:
            _buy(conn, timestamp, day, main, remaining, books, prices, probs, "MAIN")
        elif remaining > EPS and _min_cost(m_book) == float("inf"):
            _event(conn, timestamp, day, "ORDER_SHORTFALL",
                   {"outcome": main, "requested_cost": remaining, "executable_cost": 0,
                    "reason": "missing_main_depth"})

def _recovery(conn, state, timestamp, day, prices, probs, books, tier):
    main = state["main"]
    shares, cost = _portfolio(conn, day)
    cap = state["bankroll_before"] * TIERS[tier - 1][2] if tier else 0.0
    available = max(0.0, cap - cost)
    hole = max(0.0, cost - shares.get(main, 0.0))
    book = books.get(main, {})
    top = _quote(book)["best_ask"]
    # Binary search through actual VWAP. If displayed depth cannot break even,
    # the required capital is infinite rather than extrapolating the top ask.
    needed = _main_requirement(book, shares.get(main, 0.0), cost, 0.0)
    minimum = _min_cost(book)
    feasible = needed <= available + EPS and (needed <= EPS or minimum <= available + EPS)
    requested = 0.0 if hole <= EPS else (min(available, max(needed, minimum)) if feasible else available)
    quote = _quote(book, cost=requested)
    efficiency = quote["shares"] / quote["cost"] - 1 if quote["cost"] > EPS else 0.0
    spent = 0.0
    if hole > EPS and requested >= _min_cost(book) - EPS:
        spent = _buy(conn, timestamp, day, main, requested, books, prices, probs, "FULL_RECOVERY")
    elif hole > EPS and requested > EPS:
        _event(conn, timestamp, day, "ORDER_SHORTFALL",
               {"outcome": main, "requested_cost": requested, "executable_cost": quote["cost"],
                "vwap": quote["vwap"], "reason": "minimum_order_or_depth"})
    after = _map(conn, day, probs)[main]
    top_efficiency = 1 / top - 1 if top and top < 1 else 0.0
    info = {"settlement_hole": hole, "recovery_efficiency": efficiency,
            "break_even_required_capital": needed if math.isfinite(needed) else None,
            "top_ask_theoretical_break_even_capital": hole/top_efficiency if top_efficiency else None,
            "available_recovery_capital": available,
            "projected_main_pnl": after, "break_even_feasible": feasible,
            "requested_cost": requested, "actual_cost": spent,
            "liquidity_shortfall": max(0.0, requested - spent)}
    high = bool(top and top >= .90 and not feasible)
    if high:
        _event(conn, timestamp, day, "HIGH_PRICE_UNRECOVERABLE_FLIP", info)
    conn.execute("""UPDATE paired_state SET recovery_used=recovery_used+?, recovery_efficiency=?,
        break_even_feasible=?, high_price_unrecoverable=MAX(high_price_unrecoverable,?),
        recovery_info=? WHERE target_date=?""",
        (spent, efficiency, int(feasible), int(high), json.dumps(info), day))

def _executable_handover(books, names, held, current_cost, cash, plan):
    """Check the solver allocation with the paper executor before any BUY."""
    spent, added = 0.0, {}
    for name in names:
        allocation = plan["allocation"][name]
        amount = allocation["cost"]
        if not math.isfinite(amount) or amount < 0:
            return None, "invalid_allocation"
        if amount <= EPS:
            added[name] = 0.0
            continue
        book = books.get(name, {})
        quote = _quote(book, cost=amount)
        minimum = float(book.get("min_order_size") or MIN_ORDER_SHARES)
        if (quote["shares"] + EPS < minimum
                or abs(quote["cost"] - amount) > handover_minimax.TOL
                or abs(quote["shares"] - allocation["shares"]) > handover_minimax.TOL):
            return None, "minimum_order_or_depth"
        spent += quote["cost"]
        added[name] = quote["shares"]
    if spent > cash + handover_minimax.TOL:
        return None, "insufficient_cash"
    after = {name: held.get(name, 0.0) + added[name] - current_cost - spent for name in names}
    if not all(math.isfinite(value) for value in after.values()):
        return None, "invalid_projection"
    return after, None

def _handover_c_gate(conn, state, timestamp, day, main, protection, probs, books):
    """Persist a switch candidate; approve only an executable C repair."""
    pending = json.loads(state["handover_pending"])
    if state["main"] != main or state["handover_completed"]:
        if pending:
            conn.execute("UPDATE paired_state SET handover_pending='{}' WHERE target_date=?", (day,))
        return None
    old = state["protection"]
    if old and old != protection:
        pending = {"main": main, "old": old, "new": protection, "switch_timestamp": timestamp}
        conn.execute("UPDATE paired_state SET handover_pending=? WHERE target_date=?",
                     (json.dumps(pending), day))
    if not pending:
        return None
    if pending["main"] != main or pending["new"] != protection or pending["old"] not in probs:
        conn.execute("UPDATE paired_state SET handover_pending='{}' WHERE target_date=?", (day,))
        return None
    old = pending["old"]
    held, deployed = _portfolio(conn, day)
    bankroll = state["bankroll_before"]
    lead = probs[protection] - probs[old]
    details = {"policy": HANDOVER_C, "main": main, "old_protection": old,
               "new_protection": protection, "switch_timestamp": pending["switch_timestamp"],
               "normalized_probabilities": {name: probs[name] for name in (main, old, protection)},
               "gap": lead, "bankroll": bankroll, "loss_limit": HANDOVER_C_LOSS_LIMIT * bankroll,
               "main_cutoff": HANDOVER_C_MAIN_CUTOFF, "new_min": HANDOVER_C_NEW_MIN,
               "gap_min": HANDOVER_C_GAP, "tail_min": HANDOVER_C_TAIL_MIN,
               "accepted": False}

    def rejected(reason):
        details["reason"] = reason
        _event(conn, timestamp, day, "HANDOVER_C_CHECK", details)
        return None

    if held.get(old, 0.0) <= EPS:
        return rejected("old_protection_not_held")
    tail = probs[main] >= HANDOVER_C_TAIL_MIN
    if not tail:
        if probs[main] >= HANDOVER_C_MAIN_CUTOFF:
            return rejected("main_probability_band")
        if probs[protection] + EPS < HANDOVER_C_NEW_MIN:
            return rejected("new_probability_below_minimum")
        if lead + EPS < HANDOVER_C_GAP:
            return rejected("gap_below_minimum")
    names = (main, old, protection)
    cash = max(0.0, bankroll - deployed)
    plan = solve_handover(books, names, held, deployed, cash)
    after, failure = _executable_handover(books, names, held, deployed, cash, plan)
    details.update(projected_z=plan["z"], before_worst=min(plan["before"].values()),
                   projected_after=after, break_even_feasible=plan["break_even_feasible"],
                   remaining_cash=cash, proposed_allocation=plan["allocation"])
    if failure:
        return rejected(failure)
    z = min(after.values())
    details["executable_z"] = z
    if tail:
        if (not plan["break_even_feasible"] or z < -handover_minimax.TOL
                or after[main] < -handover_minimax.TOL):
            return rejected("tail_not_break_even")
    else:
        if min(plan["z"], z) + handover_minimax.TOL < -HANDOVER_C_LOSS_LIMIT * bankroll:
            return rejected("loss_limit_exceeded")
        if min(plan["z"], z) <= min(plan["before"].values()) + EPS:
            return rejected("no_worst_case_improvement")
    details.update(accepted=True, reason="break_even_tail" if tail else "bounded")
    _event(conn, timestamp, day, "HANDOVER_C_CHECK", details)
    return {"old": old, "plan": plan, "gate": details}

def _handover(conn, state, timestamp, day, old_protection, prices, probs, books, *, plan=None, gate=None):
    names = (state["main"], old_protection, state["protection"])
    held, current_cost = _portfolio(conn, day)
    cash = max(0.0, state["bankroll_before"]-current_cost)
    if plan is None:
        plan = solve_handover(books, names, held, current_cost, cash)
    before = {"total_cost": current_cost,
              "legs": {name: {"shares": held.get(name, 0.0),
                              "pnl": held.get(name, 0.0)-current_cost} for name in names}}
    quotes = {name: _quote(books.get(name, {})) for name in names}
    _snapshot_map(conn, timestamp, day, "HANDOVER_BEFORE", MODES[4], names)
    for name in names:
        amount = plan["allocation"][name]["cost"]
        if amount > EPS:
            _buy(conn, timestamp, day, name, amount, books, prices, probs,
                 "PROTECTION_HANDOVER_MINIMAX")
    after_shares, after_cost = _portfolio(conn, day)
    after = {"total_cost": after_cost,
             "legs": {name: {"shares": after_shares.get(name, 0.0),
                             "pnl": after_shares.get(name, 0.0)-after_cost} for name in names}}
    actual_added = after_cost-current_cost
    audit = {"target_date": day, "timestamp": timestamp, "main": names[0],
             "old_protection": names[1], "new_protection": names[2],
             "normalized_probabilities": {name: probs[name] for name in names},
             "orderbooks": {name: {"best_ask": quotes[name]["best_ask"],
                                   "depth_shares": quotes[name]["depth_shares"],
                                   "depth_cost": quotes[name]["depth_cost"],
                                   "depth_vwap": quotes[name]["vwap"],
                                   "proposed_vwap": plan["allocation"][name]["vwap"]}
                            for name in names},
             "bankroll": state["bankroll_before"], "remaining_cash_before": cash,
             "before": before, "break_even_feasible": plan["break_even_feasible"],
             "minimum_break_even_capital": plan["minimum_break_even_capital"],
             "optimizer_objective": plan["objective"], "z": min(
                 leg["pnl"] for leg in after["legs"].values()),
             "proposed_allocation": plan["allocation"], "after": after,
             "added_capital": actual_added, "cash_left_unused": cash-actual_added,
             "depth_shortfall_shares_for_break_even_lower_bound": {
                 name: max(0.0, current_cost-held.get(name, 0.0)
                           - quotes[name]["depth_shares"]) for name in names},
             "depth_shortfall_shares_at_after_cost": {
                 name: max(0.0, after_cost-held.get(name, 0.0)
                           - quotes[name]["depth_shares"]) for name in names},
             "minimum_order_blocked": {
                 name: quotes[name]["depth_shares"] + EPS < float(
                     (books.get(name) or {}).get("min_order_size") or MIN_ORDER_SHARES)
                 for name in names}}
    if gate is not None:
        audit["policy"] = HANDOVER_C
        audit["gate"] = gate
    conn.execute("INSERT INTO handover_audit(target_date,timestamp,details) VALUES (?,?,?)",
                 (day, timestamp, json.dumps(audit, sort_keys=True)))
    conn.execute("UPDATE paired_state SET handover_completed=1,handover_pending='{}' WHERE target_date=?", (day,))
    _event(conn, timestamp, day, "PROTECTION_HANDOVER_MINIMAX",
           {"old_protection": old_protection, "new_protection": names[2],
            "objective": plan["objective"], "added_capital": actual_added,
            "before_worst": min(leg["pnl"] for leg in before["legs"].values()),
            "after_worst": audit["z"]})
    _snapshot_map(conn, timestamp, day, "HANDOVER_AFTER", MODES[4], names)

def cycle(directory, day, timestamp, prices, probabilities, books, *, handover_enabled=None,
          handover_policy=None, paper_policy=None):
    epoch = initialize(directory, handover_enabled=handover_enabled, handover_policy=handover_policy)
    handover_enabled = bool(epoch.get("handover_enabled"))
    handover_policy = _handover_policy(existing=epoch)
    with closing(connect(path(directory))) as conn, conn:
        latest = conn.execute("SELECT bankroll_after_target FROM bankroll_ledger ORDER BY target_date DESC LIMIT 1").fetchone()
        bankroll = (paper_policy.opening_bankroll(conn, day) if paper_policy is not None else
                    latest[0] if latest else epoch['initial_bankroll'])
        conn.execute("INSERT OR IGNORE INTO paired_state(target_date,bankroll_before) VALUES (?,?)", (day, bankroll))
        state = conn.execute("SELECT * FROM paired_state WHERE target_date=?", (day,)).fetchone()
        if conn.execute("SELECT 1 FROM settlements WHERE target_date=?", (day,)).fetchone():
            return
        main = max(probabilities, key=probabilities.get)
        protection = (paper_policy.select_protection(conn, state, timestamp, day, prices, probabilities, books)
                      if paper_policy is not None and handover_policy == HANDOVER_C else
                      _protection(probabilities, main))
        if protection is None:
            return
        old_main, old_protection = state["main"], state["protection"]
        handover_completed = bool(state["handover_completed"]) if handover_enabled else False
        if handover_completed and old_main == main:
            protection = old_protection
        gate = _handover_c_gate if paper_policy is None else paper_policy.handover_gate
        c_decision = (gate(conn, state, timestamp, day, main, protection, probabilities, books)
                      if handover_policy == HANDOVER_C else None)
        mode = state["mode"]
        tier = _tier(probabilities[main])
        highest = state["highest_tier"]
        if tier > state["tier_probability_seen"]:
            conn.execute("UPDATE paired_state SET tier_probability_seen=? WHERE target_date=?", (tier, day))
        case = 0
        if old_main and old_main != main:
            names = _rank(probabilities)
            adjacent = abs(names.index(old_main) - names.index(main)) == 1
            held, current_cost = _portfolio(conn, day)
            if not adjacent:
                _event(conn, timestamp, day, "NON_ADJACENT_MAIN_FLIP", {"from": old_main, "to": main})
                if current_cost > EPS:
                    return
            if current_cost <= EPS:
                mode = MODES[0]
                _event(conn, timestamp, day, "ZERO_EXPOSURE_MAIN_CHANGE",
                       {"from": old_main, "to": main})
            elif mode == MODES[2]:
                case = 2
            elif held.get(main, 0) <= EPS:
                case, mode = 2, MODES[2]
            elif held.get(protection, 0) > EPS:
                case, mode = 1, MODES[1]
            else:
                case, mode = 3, MODES[3]
            _event(conn, timestamp, day, "MAIN_FLIP", {"from": old_main, "to": main, "case": case})
        elif mode == MODES[2]:
            case = 2
        elif mode == MODES[1]:
            case = 1
        elif mode == MODES[3]:
            case = 3
        if old_main == main and old_protection and old_protection != protection:
            _event(conn, timestamp, day, "PROTECTION_SWITCH", {"from": old_protection, "to": protection})
        held, _ = _portfolio(conn, day)
        handover_trigger = bool(handover_enabled and old_main == main and old_protection
                                and old_protection != protection and not handover_completed
                                and held.get(old_protection, 0.0) > EPS)
        if handover_policy == HANDOVER_C:
            handover_trigger = bool(c_decision)
        if handover_trigger:
            mode = MODES[4]
        if handover_completed and old_main == main:
            mode = MODES[4]
        main_hist = json.loads(state["main_history"])
        protection_hist = json.loads(state["protection_history"])
        if not main_hist or main_hist[-1] != main:
            main_hist.append(main)
        if not protection_hist or protection_hist[-1] != protection:
            protection_hist.append(protection)
        flip = int(bool(old_main and old_main != main))
        switch = int(bool(old_main == main and old_protection and old_protection != protection))
        reset_clause = ",handover_completed=CASE WHEN ? THEN 0 ELSE handover_completed END" if handover_enabled else ""
        conn.execute("""UPDATE paired_state SET main=?,protection=?,mode=?,main_history=?,
            protection_history=?, main_flips=main_flips+?,protection_switches=protection_switches+?,
            case1_count=case1_count+?,case2_count=case2_count+?,case3_count=case3_count+?"""
            + reset_clause + " WHERE target_date=?", (main, protection, mode, json.dumps(main_hist),
            json.dumps(protection_hist), flip, switch, int(flip and case == 1),
            int(flip and case == 2), int(flip and case == 3))
            + ((flip,) if handover_enabled else ()) + (day,))
        state = conn.execute("SELECT * FROM paired_state WHERE target_date=?", (day,)).fetchone()
        _snapshot_map(conn, timestamp, day, "BEFORE", mode, probabilities)
        coverage = probabilities[main] + probabilities[protection]
        if handover_trigger:
            if handover_policy == HANDOVER_C:
                _handover(conn, state, timestamp, day, c_decision["old"], prices, probabilities, books,
                          plan=c_decision["plan"], gate=c_decision["gate"])
            else:
                _handover(conn, state, timestamp, day, old_protection, prices, probabilities, books)
        elif handover_enabled and state["handover_completed"]:
            pass
        elif case == 2:
            recovery = _recovery if paper_policy is None else paper_policy.recovery
            recovery(conn, state, timestamp, day, prices, probabilities, books, tier)
        elif tier and coverage + EPS >= .80:
            target = state["bankroll_before"] * TIERS[tier - 1][1]
            _, deployed = _portfolio(conn, day)
            # The current cumulative target, not a one-tick crossing event,
            # controls entry. A missed higher tier may retry at a lower active
            # tier whenever its own target still exceeds actual deployed cost.
            if target > deployed + EPS and not (switch and deployed > EPS and tier <= highest):
                allocate = _allocate if paper_policy is None else paper_policy.allocate
                allocate(conn, state, timestamp, day, prices, probabilities, books, target, case)
                _, deployed = _portfolio(conn, day)
            if deployed >= target - EPS:
                conn.execute("""UPDATE paired_state SET highest_tier=MAX(highest_tier,?),
                    pending_tier=0 WHERE target_date=?""", (tier, day))
            else:
                conn.execute("UPDATE paired_state SET pending_tier=? WHERE target_date=?", (tier, day))
        _snapshot_map(conn, timestamp, day, "AFTER", mode, probabilities)
        save_exposure(conn, day, timestamp, list(probabilities))

def settle(directory, day, winner, timestamp):
    handover_enabled = bool(initialize(directory).get("handover_enabled"))
    with closing(connect(path(directory))) as conn, conn:
        if conn.execute("SELECT 1 FROM settlements WHERE target_date=?", (day,)).fetchone():
            return
        state = conn.execute("SELECT * FROM paired_state WHERE target_date=?", (day,)).fetchone()
        if state is None:
            last = conn.execute("SELECT bankroll_after_target FROM bankroll_ledger ORDER BY target_date DESC LIMIT 1").fetchone()
            conn.execute("INSERT INTO paired_state(target_date,bankroll_before) VALUES (?,?)",
                         (day, last[0] if last else INITIAL_BANKROLL))
            state = conn.execute("SELECT * FROM paired_state WHERE target_date=?", (day,)).fetchone()
        fills = conn.execute("SELECT * FROM fills WHERE target_date=?", (day,)).fetchall()
        cost = sum(row["cost"] for row in fills)
        value = sum(row["shares"] for row in fills if row["outcome"] == winner)
        pnl = value - cost
        for row in fills:
            payout = row["shares"] if row["outcome"] == winner else 0.0
            conn.execute("UPDATE fills SET winner=?,settlement_value=?,pnl=?,roi=? WHERE id=?",
                         (int(row["outcome"] == winner), payout, payout-row["cost"],
                          (payout-row["cost"])/row["cost"], row["id"]))
        conn.execute("UPDATE positions SET shares=0 WHERE target_date=?", (day,))
        conn.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?)", (day, timestamp, winner, cost, value, pnl))
        before = state["bankroll_before"]
        after = before + pnl
        previous = conn.execute("SELECT peak_bankroll,max_drawdown FROM bankroll_ledger ORDER BY target_date DESC LIMIT 1").fetchone()
        peak = max(INITIAL_BANKROLL, previous[0] if previous else INITIAL_BANKROLL, after)
        drawdown = (peak-after)/peak
        max_drawdown = max(drawdown, previous[1] if previous else 0)
        metrics = {key: state[key] for key in ("main_history", "protection_history", "protection_switches",
            "main_flips", "case1_count", "case2_count", "case3_count", "mode", "break_even_feasible",
            "recovery_used", "recovery_efficiency", "high_price_unrecoverable", "recovery_info")}
        if handover_enabled:
            metrics["handover_completed"] = state["handover_completed"]
        bought = {r[0] for r in conn.execute("SELECT outcome FROM fills WHERE target_date=?",
                                            (day,))}
        legacy = (set(json.loads(state["protection_history"])) - {state["protection"]}) & bought
        handover_legacy = ({json.loads(r[0])["old_protection"] for r in conn.execute(
            "SELECT details FROM handover_audit WHERE target_date=?", (day,))}
            if handover_enabled else set())
        classification = ("NO_POSITION" if cost <= EPS else
                          "MAIN_HIT" if winner == state["main"] else
                          "LEGACY_PROTECTION_HIT" if winner in handover_legacy else
                          "ACTIVE_PROTECTION_HIT" if winner == state["protection"] else
                          "LEGACY_PROTECTION_HIT" if winner in legacy else "FULL_MISS")
        metrics["winner_classification"] = classification
        if handover_enabled:
            conn.execute("UPDATE handover_audit SET final_winner=?,realized_pnl=? WHERE target_date=?",
                         (winner, pnl, day))
            metrics["handovers"] = [dict(row) for row in conn.execute(
                "SELECT timestamp,details,final_winner,realized_pnl FROM handover_audit WHERE target_date=?",
                (day,))]
        metrics["fills"] = [dict(row) for row in fills]
        metrics["settlement_maps"] = [dict(row) for row in conn.execute(
            "SELECT phase,mode,map_json FROM settlement_maps WHERE target_date=? ORDER BY id", (day,))]
        conn.execute("""INSERT INTO bankroll_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (day, before, cost, winner, pnl, after, pnl/cost if cost else 0,
                      (after-INITIAL_BANKROLL)/INITIAL_BANKROLL, peak, drawdown, max_drawdown,
                      cost/before if before else 0, json.dumps(metrics)))

def summary(directory, day, probabilities):
    db = path(directory)
    if not db.exists():
        return f"{STRATEGY}: awaiting first snapshot"
    with closing(connect(db)) as conn, conn:
        state = conn.execute("SELECT * FROM paired_state WHERE target_date=?", (day,)).fetchone()
        last = conn.execute("SELECT * FROM bankroll_ledger ORDER BY target_date DESC LIMIT 1").fetchone()
        if not state:
            bankroll = last["bankroll_after_target"] if last else INITIAL_BANKROLL
            return f"{STRATEGY} | {day} | bankroll {bankroll:.2f}U"
        main, protection = state["main"], state["protection"]
        pnl = _map(conn, day, probabilities)
        _, deployed = _portfolio(conn, day)
        bankroll = state["bankroll_before"]
        parts = [STRATEGY, f"{day} | bankroll {bankroll:.2f}U",
                 f"Main {main} {probabilities.get(main, 0):.1%} | Protection {protection} {probabilities.get(protection, 0):.1%}",
                 f"coverage {probabilities.get(main, 0)+probabilities.get(protection, 0):.1%} | {state['mode']}",
                 f"deployed {deployed:.2f}/{bankroll:.2f}U | tier {state['highest_tier']}",
                 f"settlement Main {pnl.get(main, -deployed):+.2f}U | Protection {pnl.get(protection, -deployed):+.2f}U"]
        if state["mode"] == MODES[2]:
            info = json.loads(state["recovery_info"])
            parts.append(f"recovery {state['recovery_used']:.2f}U | break-even {'yes' if info.get('break_even_feasible') else 'no'}")
        parts.append(f"cumulative PnL {(last['bankroll_after_target']-INITIAL_BANKROLL) if last else 0:+.2f}U | drawdown {last['drawdown'] if last else 0:.1%}")
        return "\n".join(parts)
