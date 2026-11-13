"""Offline ledger primitives extracted from the original observation engine."""
from __future__ import annotations
import sqlite3,json,re
from pathlib import Path
from datetime import datetime,date,timedelta,timezone
from copy import deepcopy
from src.polymarket_client import *
from src.market_calendar import MONTH_SLUGS
ROOT=Path(__file__).resolve().parents[1]
DATA_DIR=ROOT/'research_outputs'
HKT=timezone(timedelta(hours=8))
EPS=1e-9


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def make_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS epoch(epoch_id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS positions(
          target_date TEXT NOT NULL, outcome TEXT NOT NULL, shares REAL NOT NULL DEFAULT 0,
          total_cost REAL NOT NULL DEFAULT 0, highest_band_reached TEXT,
          target_position_cost REAL NOT NULL DEFAULT 0, fixed_executed INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(target_date,outcome));
        CREATE TABLE IF NOT EXISTS tranches(
          target_date TEXT NOT NULL, outcome TEXT NOT NULL, tranche TEXT NOT NULL,
          target_cost REAL NOT NULL, deployed REAL NOT NULL DEFAULT 0,
          PRIMARY KEY(target_date,outcome,tranche));
        CREATE TABLE IF NOT EXISTS orders(
          id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, target_date TEXT NOT NULL,
          outcome TEXT NOT NULL, side TEXT NOT NULL CHECK(side='BUY'), tranche TEXT,
          sizing_source TEXT NOT NULL, raw_desired_size REAL NOT NULL,
          requested_cost REAL NOT NULL, executable_size REAL NOT NULL,
          actual_fill_size REAL NOT NULL, status TEXT NOT NULL, limitation TEXT);
        CREATE TABLE IF NOT EXISTS fills(
          id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
          timestamp TEXT NOT NULL, target_date TEXT NOT NULL, outcome TEXT NOT NULL,
          tranche TEXT, sizing_source TEXT NOT NULL, raw_price REAL NOT NULL,
          normalized_probability REAL NOT NULL, best_bid REAL, best_ask REAL,
          spread REAL, cost REAL NOT NULL, vwap REAL NOT NULL, shares REAL NOT NULL,
          settlement_value REAL, pnl REAL, roi REAL, winner INTEGER);
        CREATE TABLE IF NOT EXISTS settlements(
          target_date TEXT PRIMARY KEY, timestamp TEXT NOT NULL, winner TEXT NOT NULL,
          cost REAL NOT NULL, settlement_value REAL NOT NULL, pnl REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS exposure_snapshots(
          timestamp TEXT NOT NULL, target_date TEXT NOT NULL,
          total_deployed REAL NOT NULL, by_outcome_json TEXT NOT NULL,
          worst_case_settlement_pnl REAL NOT NULL,
          PRIMARY KEY(timestamp,target_date));
        CREATE TABLE IF NOT EXISTS snapshots(
          id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, target_date TEXT NOT NULL,
          outcome TEXT NOT NULL, raw_price REAL NOT NULL, normalized_probability REAL NOT NULL,
          best_bid REAL, best_ask REAL, spread REAL, available_depth_shares REAL NOT NULL,
          available_depth_cost REAL NOT NULL, one_u_vwap REAL,
          change_5m REAL, change_15m REAL, change_30m REAL, change_60m REAL,
          main_outcome TEXT NOT NULL, distribution_mean REAL, distribution_variance REAL,
          hko_json TEXT NOT NULL, book_error TEXT, UNIQUE(timestamp,target_date,outcome));
        CREATE INDEX IF NOT EXISTS snapshots_history ON snapshots(target_date,outcome,timestamp);
    """)


def normalized(prices: dict[str, float]) -> dict[str, float]:
    valid = {name: float(price) for name, price in prices.items() if 0 <= float(price) <= 1}
    if len(valid) != len(prices) or not valid or sum(valid.values()) <= 0:
        raise ValueError("Incomplete or invalid raw Market price distribution")
    total = sum(valid.values())
    return {name: price / total for name, price in valid.items()}


def market_inputs(client: PolymarketClient, *, details: dict | None = None) -> tuple[dict[str, float], dict[str, str], bool]:
    """Read Gamma outcome prices for signals and CLOB token IDs for execution."""
    market = client.fetch_market_or_none(client.event_slug)
    if market and is_multi_outcome_temperature_market(market):
        markets = [market]
    else:
        markets = client.fetch_event(client.event_slug).get("markets", [])
    prices: dict[str, float] = {}
    tokens: dict[str, str] = {}
    closed: list[bool] = []
    for market in markets:
        labels = parse_json_list(market.get("outcomes"))
        if is_multi_outcome_temperature_market(market):
            for label in labels:
                name = outcome_name_from_label(str(label))
                if name:
                    refs = outcome_refs_from_gamma_market(str(market.get("slug") or ""), str(label), market)
                    if refs["gamma_price"] is None:
                        raise ValueError(f"Missing raw Market price: {name}")
                    prices[name] = float(refs["gamma_price"])
                    tokens[name] = str(refs["clob_token_id"] or "")
                    if details is not None:
                        details[name] = market
            closed.append(bool(market.get("closed")))
        else:
            name = outcome_name_from_market(market)
            if name is None:
                continue
            values = parse_json_list(market.get("outcomePrices"))
            index = yes_outcome_index(market)
            if index >= len(values):
                raise ValueError(f"Missing raw Market price: {name}")
            prices[name] = float(values[index])
            tokens[name] = yes_clob_token_id(market) or ""
            if details is not None:
                details[name] = market
            closed.append(bool(market.get("closed")))
    if len(prices) < 2 or len(prices) != len(tokens):
        raise ValueError("No complete temperature market found")
    return prices, tokens, bool(closed and all(closed))


def execution(book: dict, *, requested_cost: float | None = None,
              requested_shares: float | None = None) -> dict:
    levels = orderbook_levels(book, "BUY")
    bids = orderbook_levels(book, "BID")
    bid = bids[0][0] if bids else None
    ask = levels[0][0] if levels else None
    remaining_cost = max(0.0, requested_cost) if requested_cost is not None else None
    remaining_shares = max(0.0, requested_shares) if requested_shares is not None else None
    cost = shares = 0.0
    consumed = []
    for price, depth in levels:
        take = depth
        if remaining_cost is not None:
            take = min(take, remaining_cost / price)
        if remaining_shares is not None:
            take = min(take, remaining_shares)
        cost += take * price
        shares += take
        if take > EPS:
            consumed.append((price, take))
        if remaining_cost is not None:
            remaining_cost -= take * price
        if remaining_shares is not None:
            remaining_shares -= take
        if (remaining_cost is not None and remaining_cost <= EPS) or (remaining_shares is not None and remaining_shares <= EPS):
            break
    depth_shares = sum(size for _, size in levels)
    depth_cost = sum(price * size for price, size in levels)
    limited = (remaining_cost is not None and remaining_cost > EPS) or (remaining_shares is not None and remaining_shares > EPS)
    return {"best_bid": bid, "best_ask": ask,
            "spread": ask - bid if ask is not None and bid is not None else None,
            "depth_shares": depth_shares, "depth_cost": depth_cost,
            "cost": cost, "shares": shares, "vwap": cost / shares if shares > EPS else None,
            "limited": limited, "consumed": consumed}


def consume_book(book: dict, fill: dict) -> None:
    """Prevent separate tranches from reusing the same displayed ask depth."""
    for price, shares in fill["consumed"]:
        remaining = shares
        for level in book.get("asks", []):
            if abs(float(level["price"]) - price) <= EPS and remaining > EPS:
                available = float(level.get("size") or 0.0)
                used = min(available, remaining)
                level["size"] = str(available - used)
                remaining -= used


def position(conn: sqlite3.Connection, day: str, outcome: str) -> sqlite3.Row:
    conn.execute("INSERT OR IGNORE INTO positions(target_date,outcome) VALUES (?,?)", (day, outcome))
    return conn.execute("SELECT * FROM positions WHERE target_date=? AND outcome=?", (day, outcome)).fetchone()


def record_order(conn: sqlite3.Connection, *, timestamp: str, day: str, outcome: str,
                 tranche: str | None, source: str, raw_size: float,
                 requested_cost: float, fill: dict, raw_price: float, probability: float) -> None:
    actual = fill["cost"]
    limitation = "insufficient_ask_depth" if fill["limited"] else None
    cursor = conn.execute("""INSERT INTO orders(timestamp,target_date,outcome,side,tranche,sizing_source,
        raw_desired_size,requested_cost,executable_size,actual_fill_size,status,limitation)
        VALUES (?,?,?,'BUY',?,?,?,?,?,?,?,?)""",
        (timestamp, day, outcome, tranche, source, raw_size, requested_cost,
         actual, actual, "PARTIAL" if limitation and actual > EPS else "FILLED" if actual > EPS else "UNFILLED",
         limitation))
    if actual <= EPS:
        return
    conn.execute("""INSERT INTO fills(order_id,timestamp,target_date,outcome,tranche,sizing_source,
        raw_price,normalized_probability,best_bid,best_ask,spread,cost,vwap,shares)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cursor.lastrowid, timestamp, day, outcome, tranche, source, raw_price, probability,
         fill["best_bid"], fill["best_ask"], fill["spread"], actual, fill["vwap"], fill["shares"]))
    conn.execute("UPDATE positions SET shares=shares+?, total_cost=total_cost+? WHERE target_date=? AND outcome=?",
                 (fill["shares"], actual, day, outcome))


def save_exposure(conn: sqlite3.Connection, day: str, timestamp: str,
                  all_outcomes: list[str]) -> None:
    rows = conn.execute("SELECT outcome,shares,total_cost FROM positions WHERE target_date=?", (day,)).fetchall()
    by_outcome = {row["outcome"]: {"shares": row["shares"], "notional": row["total_cost"]}
                  for row in rows}
    total = sum(row["total_cost"] for row in rows)
    worst = min((by_outcome.get(name, {}).get("shares", 0.0) - total for name in all_outcomes),
                default=-total)
    conn.execute("INSERT OR REPLACE INTO exposure_snapshots VALUES (?,?,?,?,?)",
                 (timestamp, day, total, json.dumps(by_outcome, sort_keys=True), worst))


def _date_from_slug(slug: str) -> str:
    match = re.search(r"on-([a-z]+)-(\d{1,2})-(\d{4})", slug.lower())
    months = {name: number for number, name in MONTH_SLUGS.items()}
    if not match or match.group(1) not in months:
        raise ValueError(f"Invalid target date in slug: {slug}")
    return date(int(match.group(3)), months[match.group(1)], int(match.group(2))).isoformat()
