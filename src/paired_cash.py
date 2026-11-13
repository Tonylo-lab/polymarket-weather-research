"""Offline cash ledger and original decision flow. Account migrations excluded."""
from __future__ import annotations
import hashlib,json,math,sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from datetime import date,datetime,timedelta
from src import paired_ladder as ladder
from src import paired_cash_policy,paper_execution,weather_relay
from src.market_calendar import event_slug_for_date
from src.market_only import DATA_DIR,EPS,HKT,connect,normalized,market_inputs,_date_from_slug
from src.polymarket_client import *
from src.paired_cash_policy import CashPolicy
DIRECTORY=DATA_DIR
INITIAL_CASH=100.
EXIT_PRICE=.998
EXIT_POLICY='best_bid_limit_v1'
POLYMARKET_AUTO_EVENT_SLUG=True
POLYMARKET_EVENT_SLUG='synthetic'
RELAY_ENGINE=weather_relay
def _hko_data(*args,**kwargs):
    raise RuntimeError('HKO_NETWORK_EXCLUDED_USE_EXPLICIT_OFFLINE_INPUT')
def initialize(directory=DIRECTORY,now=None,*,enable_relay=None):
    # Only fresh, isolated research accounts; never import or upgrade an account.
    if enable_relay: raise RuntimeError('NETWORK_RELAY_EXCLUDED')
    directory=Path(directory)
    marker=directory/'cash_experiment_epoch.json'
    if marker.exists() or any(directory.glob('*.db')) or any(directory.glob('*.sqlite')):
        raise RuntimeError('RESEARCH_REQUIRES_FRESH_DIRECTORY')
    epoch=ladder.initialize(directory,now=now,handover_policy='C',initial_bankroll=INITIAL_CASH)
    with closing(connect(ladder.path(directory))) as conn,conn:
        _schema(conn)
        conn.execute('INSERT INTO paper_cash_account VALUES(1,?,?,NULL)',(INITIAL_CASH,INITIAL_CASH))
    return epoch


def balance(conn):
    return float(conn.execute('SELECT cash FROM paper_cash_account WHERE id=1').fetchone()[0])


def _schema(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS paper_cash_account(
          id INTEGER PRIMARY KEY CHECK(id=1), initial_cash REAL NOT NULL,
          cash REAL NOT NULL CHECK(cash>=-0.0000001), active_target_date TEXT);
        CREATE TABLE IF NOT EXISTS paper_targets(
          target_date TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'ACTIVE',
          exit_outcome TEXT, exit_triggered_at TEXT, official_winner TEXT,
          official_resolved_at TEXT, last_observed_at TEXT, last_error TEXT);
        CREATE TABLE IF NOT EXISTS paper_cash_movements(
          movement_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, target_date TEXT NOT NULL,
          outcome TEXT, kind TEXT NOT NULL, amount REAL NOT NULL, gross REAL NOT NULL,
          fee REAL NOT NULL, details_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS paper_entry_wait(
          target_date TEXT PRIMARY KEY, main TEXT NOT NULL, reserved_cash REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS paper_depth_usage(
          target_date TEXT NOT NULL,outcome TEXT NOT NULL,book_key TEXT NOT NULL,
          side TEXT NOT NULL,price REAL NOT NULL,shares REAL NOT NULL,
          PRIMARY KEY(target_date,outcome,book_key,side,price));
        CREATE TABLE IF NOT EXISTS paper_market_samples(
          target_date TEXT NOT NULL,timestamp TEXT NOT NULL,raw_prices_json TEXT NOT NULL,
          normalized_json TEXT NOT NULL,books_json TEXT NOT NULL,markets_json TEXT NOT NULL,
          resolution_json TEXT NOT NULL,errors_json TEXT NOT NULL,hko_json TEXT NOT NULL,
          PRIMARY KEY(target_date,timestamp));
        CREATE TABLE IF NOT EXISTS paper_day_results(
          target_date TEXT PRIMARY KEY,timestamp TEXT NOT NULL,winner TEXT NOT NULL,
          buy_cost REAL NOT NULL,sell_net REAL NOT NULL,redemption REAL NOT NULL,
          realized_pnl REAL NOT NULL,cash_after REAL NOT NULL);
    ''')
    RELAY_ENGINE.schema(conn)


def _move(conn, key, timestamp, day, outcome, kind, amount, gross, charge, details):
    if conn.execute('SELECT 1 FROM paper_cash_movements WHERE movement_id=?', (key,)).fetchone():
        return False
    if balance(conn)+amount < -1e-7 or not math.isfinite(amount):
        raise RuntimeError('Paper cash movement would overdraw the account')
    conn.execute('INSERT INTO paper_cash_movements VALUES (?,?,?,?,?,?,?,?,?)',
                 (key, timestamp, day, outcome, kind, amount, gross, charge, json.dumps(details, sort_keys=True)))
    conn.execute('UPDATE paper_cash_account SET cash=cash+? WHERE id=1', (amount,))
    return True


def _use_depth(conn, day, name, key, side, consumed):
    for price, shares in consumed:
        conn.execute('INSERT INTO paper_depth_usage VALUES (?,?,?,?,?,?) '
            'ON CONFLICT(target_date,outcome,book_key,side,price) DO UPDATE SET shares=shares+excluded.shares',
            (day, name, key, side, price, shares))


def debit_buy(conn, timestamp, day, name, fill, schedule):
    row = conn.execute('SELECT id FROM fills ORDER BY id DESC LIMIT 1').fetchone()
    _move(conn, f'BUY:{row[0]}', timestamp, day, name, 'BUY', -fill['cost'],
          fill['gross'], fill['fee'], {'shares': fill['shares'], 'vwap': fill['vwap'], 'fee_schedule': schedule})
    _use_depth(conn, day, name, schedule['book_key'], 'asks', fill['consumed'])


def _fee_schedule(market):
    if market.get('feesEnabled') is False:
        return {'rate': 0., 'exponent': 1., 'source': 'Gamma feesEnabled=false'}
    schedule = market.get('feeSchedule')
    if not isinstance(schedule, dict) or 'rate' not in schedule or 'exponent' not in schedule:
        raise ValueError('Missing market fee schedule; paper execution waits')
    result = {'rate': float(schedule['rate']), 'exponent': float(schedule['exponent']),
              'source': 'Gamma feeSchedule', 'raw': schedule}
    paper_execution.fee(1., .5, result)
    return result


def _asset_id(market, name, fallback):
    version = market.get('version') or 'v1'
    if version == 'v1':
        return fallback
    if version != 'v2':
        raise ValueError(f'Unsupported market version: {version}')
    ids = parse_json_list(market.get('positionIds'))
    if is_multi_outcome_temperature_market(market):
        labels = parse_json_list(market.get('outcomes'))
        from src.polymarket_client import outcome_name_from_label
        index = next(i for i, label in enumerate(labels) if outcome_name_from_label(str(label)) == name)
    else:
        index = yes_outcome_index(market)
    if index >= len(ids) or not ids[index]:
        raise ValueError('Missing V2 position ID')
    return str(ids[index])


def _executable_books(conn, day, books, markets, errors):
    result = {}
    for name, raw in books.items():
        try:
            if not isinstance(raw, dict) or not isinstance(raw.get('asks', []), list) or not isinstance(raw.get('bids', []), list):
                raise ValueError('Invalid order book payload')
            minimum = float(raw.get('min_order_size') or ladder.MIN_ORDER_SHARES)
            if not math.isfinite(minimum) or minimum <= 0:
                raise ValueError('Invalid minimum order size')
            market = markets[name]
            if market.get('closed') is True or market.get('acceptingOrders') is not True or market.get('active') is False:
                continue
            schedule = _fee_schedule(market)
            identity = {'hash': raw['hash'], 'asset_id': raw.get('asset_id')} if raw.get('hash') else raw
            key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            book = deepcopy(raw)
            for usage in conn.execute('SELECT side,price,shares FROM paper_depth_usage '
                                     'WHERE target_date=? AND outcome=? AND book_key=?', (day, name, key)):
                remaining = usage['shares']
                for level in book.get(usage['side'], []):
                    if abs(float(level['price'])-usage['price']) <= EPS:
                        size = float(level.get('size') or 0.)
                        take = min(size, remaining)
                        level['size'] = str(size-take)
                        remaining -= take
            schedule['book_key'] = key
            book['_paper_fee'], book['_paper_cash'] = schedule, True
            result[name] = book
        except (ValueError, TypeError, KeyError, StopIteration) as exc:
            errors[name] = str(exc)
    return result


def _exit_bid_book(book):
    if book is None:
        return None
    # Keep the original book identity for depth accounting, but never cross
    # below the exit floor while filling a larger or already pending order.
    bids = [{'price': str(price), 'size': str(size)}
            for price, size in orderbook_levels(book, 'BID')
            if math.isfinite(price+size) and EXIT_PRICE <= price <= 1.]
    return {**book, 'bids': bids}


def _sell(conn, day, timestamp, name, book):
    position = conn.execute('SELECT shares FROM positions WHERE target_date=? AND outcome=?', (day, name)).fetchone()
    quantity = position['shares'] if position else 0.
    if quantity <= EPS:
        return True
    if book is None:
        ladder._event(conn, timestamp, day, 'CASH_EXIT_WAIT', {'outcome': name, 'reason': 'no_executable_book_or_fee_schedule'})
        return False
    book = _exit_bid_book(book)
    minimum = float(book.get('min_order_size') or ladder.MIN_ORDER_SHARES)
    fill = paper_execution.quote(book, shares=quantity, side='SELL')
    if quantity+EPS < minimum or fill['shares']+EPS < minimum:
        ladder._event(conn, timestamp, day, 'CASH_EXIT_WAIT',
                      {'outcome': name, 'reason': 'minimum_order_or_bid_depth', 'held_shares': quantity,
                       'executable_shares': fill['shares'], 'minimum_shares': minimum,
                       'minimum_exit_bid': EXIT_PRICE})
        return False
    key = f'SELL:{day}:{timestamp}:{name}'
    details = {'shares': fill['shares'], 'vwap': fill['vwap'], 'bid': fill['best_bid'],
               'partial': fill['limited'], 'fee_schedule': book['_paper_fee'],
               'minimum_exit_bid': EXIT_PRICE, 'consumed': fill['consumed']}
    if _move(conn, key, timestamp, day, name, 'SELL', fill['net'], fill['gross'], fill['fee'], details):
        conn.execute('UPDATE positions SET shares=MAX(0,shares-?) WHERE target_date=? AND outcome=?',
                     (fill['shares'], day, name))
        _use_depth(conn, day, name, book['_paper_fee']['book_key'], 'bids', fill['consumed'])
        ladder._event(conn, timestamp, day, 'PAPER_SELL', details | {'outcome': name, 'net': fill['net'], 'fee': fill['fee']})
    remaining = conn.execute('SELECT shares FROM positions WHERE target_date=? AND outcome=?', (day, name)).fetchone()[0]
    return remaining <= EPS


def _redeem(conn, day, timestamp, resolution):
    target = conn.execute('SELECT * FROM paper_targets WHERE target_date=?', (day,)).fetchone()
    if target['state'] in ('SETTLED', 'RELAY_VOIDED_ERROR'):
        return
    winner = resolution['winner']
    position = conn.execute('SELECT shares FROM positions WHERE target_date=? AND outcome=?', (day, winner)).fetchone()
    payout = position['shares'] if position else 0.
    _move(conn, f'REDEEM:{day}', timestamp, day, winner, 'REDEEM', payout, payout, 0.,
          {'remaining_winner_shares': payout, 'resolution': resolution, 'paper': True})
    conn.execute('UPDATE positions SET shares=0 WHERE target_date=?', (day,))
    cost = conn.execute('SELECT COALESCE(SUM(cost),0) FROM fills WHERE target_date=?', (day,)).fetchone()[0]
    proceeds = conn.execute("SELECT COALESCE(SUM(amount),0) FROM paper_cash_movements WHERE target_date=? AND kind='SELL'", (day,)).fetchone()[0]
    conn.execute('INSERT INTO paper_day_results VALUES (?,?,?,?,?,?,?,?)',
                 (day, timestamp, winner, cost, proceeds, payout, proceeds+payout-cost, balance(conn)))
    conn.execute("UPDATE paper_targets SET state='SETTLED',official_winner=?,official_resolved_at=? WHERE target_date=?",
                 (winner, resolution.get('resolved_at') or timestamp, day))
    ladder._event(conn, timestamp, day, 'PAPER_REDEEM_REMAINDER', {'winner': winner, 'payout': payout})


def _observe(directory, day, timestamp, client, active, hko):
    with closing(connect(ladder.path(directory))) as conn:
        target = conn.execute('SELECT state FROM paper_targets WHERE target_date=?', (day,)).fetchone()
        if target and target['state'] == 'RELAY_VOIDED_ERROR':
            return
    relay = client.relay_metadata if isinstance(client, weather_relay.EventClient) else None
    slug = relay['slug'] if relay else event_slug_for_date(date.fromisoformat(day)) if POLYMARKET_AUTO_EVENT_SLUG else POLYMARKET_EVENT_SLUG
    client.event_slug = slug
    prices, probs, books, markets, errors = {}, {}, {}, {}, {}
    try:
        resolution = client.get_official_resolution()
        if not isinstance(resolution, dict):
            raise ValueError('Invalid official resolution response')
    except Exception as exc:
        resolution = {'is_officially_resolved': False, 'resolution_status': f'{type(exc).__name__}: {exc}'}
    try:
        prices, tokens, _ = client.market_inputs(markets) if relay else market_inputs(client, details=markets)
        probs = normalized(prices)
        # Even closed, price-one and bid-only markets are observed, never stopped by a threshold.
        for name, token in tokens.items():
            try:
                asset = _asset_id(markets[name], name, token)
                if not asset:
                    raise ValueError('Missing outcome asset ID')
                books[name] = client.fetch_clob_book(asset)
            except Exception as exc:
                errors[name] = f'{type(exc).__name__}: {exc}'
    except Exception as exc:
        errors['market'] = f'{type(exc).__name__}: {exc}'
    weather = {} if relay and relay['city_slug'] != 'hong-kong' else hko if hko is not None else _hko_data(relay['contract_date'] if relay else day) if active else {}
    should_buy = False
    with closing(connect(ladder.path(directory))) as conn, conn:
        executable = _executable_books(conn, day, books, markets, errors)
        conn.execute('INSERT OR IGNORE INTO paper_market_samples VALUES (?,?,?,?,?,?,?,?,?)',
                     (day, timestamp, json.dumps(prices, sort_keys=True), json.dumps(probs, sort_keys=True),
                      json.dumps(books, sort_keys=True), json.dumps(markets, sort_keys=True),
                      json.dumps(resolution, sort_keys=True), json.dumps(errors, sort_keys=True), json.dumps(weather, sort_keys=True)))
        conn.execute('UPDATE paper_targets SET last_observed_at=?,last_error=? WHERE target_date=?',
                     (timestamp, json.dumps(errors, sort_keys=True) if errors else None, day))
    # Observations survive an interrupted trade; financial effects remain atomic.
    with closing(connect(ladder.path(directory))) as conn, conn:
        executable = _executable_books(conn, day, books, markets, errors)
        if resolution.get('is_officially_resolved') and resolution.get('winner'):
            _redeem(conn, day, timestamp, resolution)
            return
        target = conn.execute('SELECT * FROM paper_targets WHERE target_date=?', (day,)).fetchone()
        exit_books = {name: _exit_bid_book(book) for name, book in executable.items()}
        candidates = [name for name, book in exit_books.items() if book['bids']]
        candidate = max(candidates, key=lambda name: float(exit_books[name]['bids'][0]['price'])) if candidates else None
        if candidate is not None:
            if target['exit_outcome'] is None:
                conn.execute("UPDATE paper_targets SET exit_outcome=?,exit_triggered_at=?,state='EXIT_PENDING' WHERE target_date=?",
                             (candidate, timestamp, day))
                ladder._event(conn, timestamp, day, 'BEST_BID_EXIT_TRIGGER',
                              {'outcome': candidate, 'best_bid': float(exit_books[candidate]['bids'][0]['price']),
                               'minimum_exit_bid': EXIT_PRICE, 'raw_price': prices.get(candidate),
                               'normalized_probability': probs.get(candidate)})
                target = conn.execute('SELECT * FROM paper_targets WHERE target_date=?', (day,)).fetchone()
        if target['exit_outcome']:
            complete = _sell(conn, day, timestamp, target['exit_outcome'], executable.get(target['exit_outcome']))
            state = 'EXITED_WAIT_RESOLUTION' if complete else 'EXIT_PENDING'
            conn.execute('UPDATE paper_targets SET state=? WHERE target_date=?', (state, day))
        elif resolution.get('is_terminal') or (markets and all(
                m.get('closed') is True or m.get('acceptingOrders') is False for m in markets.values())):
            conn.execute("UPDATE paper_targets SET state='CLOSED_WAIT_RESOLUTION' WHERE target_date=?", (day,))
        elif active and probs:
            main = max(probs, key=probs.get)
            # Missing asks or fees pause BUYs for this tick, without latching a terminal state.
            # A repair of an already held Protection needs only executable Main asks.
            should_buy = main in executable and ladder._quote(executable[main])['depth_shares'] > EPS
    if should_buy:
        ladder.cycle(directory, day, timestamp, prices, probs, executable, handover_policy='C',
                     paper_policy=CashPolicy(exit_price=EXIT_PRICE))
