"""Offline replay parsing and accounting. Original data downloader excluded."""
from __future__ import annotations
import hashlib,json,math,sqlite3,io
from collections import Counter
from contextlib import closing,ExitStack,redirect_stdout
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
HK=timezone(timedelta(hours=8))
EPS=1e-7
ENGINE=None


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024*1024), b''):
            h.update(data)
    return h.hexdigest()


def finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    return value


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + '.part')
    part.write_text(json.dumps(finite(value), ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    part.replace(path)


def readonly(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    return c


def load_engine():
    global ENGINE
    if ENGINE is not None:
        return ENGINE
    # Import current code without reading the real environment file.
    original = Path.read_text
    with patch.object(Path, 'read_text', lambda p, *a, **k: '' if p.resolve() == ROOT/'.env' else original(p, *a, **k)):
        import src.config
    from live_strategy import planner, minimax_exit
    from paper_strategy import legacy
    from src.polymarket_client import outcome_name_from_label
    ENGINE = planner, minimax_exit, legacy, outcome_name_from_label
    return ENGINE


def inputs_for_frame(raw, identity, fee_rate, name_from_label):
    prices, books, markets, tokens = {}, {}, {}, {}
    missing, inferred_tick = 0, 0
    for item in identity['markets']:
        token = item['token']; b = raw[token]
        name = name_from_label(item['label'])
        if not name or not b['known'] or b['crossed']:
            raise ValueError('UNUSABLE_DEPTH_BASELINE')
        price = b.get('price_proxy')
        if price is None:
            interval = b.get('price_interval')
            if not interval:
                raise ValueError('MISSING_PRICE_ENVELOPE')
            lower, upper = Decimal(interval['lower']), Decimal(interval['upper'])
            if upper-lower > Decimal('.01'):
                raise ValueError('WIDE_MISSING_PRICE_ENVELOPE')
            price = str((lower+upper)/2)
            missing += 1
        prices[name] = float(price)
        bid, ask = deepcopy(b['bids']), deepcopy(b['asks'])
        tick = b.get('tick_size')
        if tick is None:
            values = [Decimal(x['price']) for x in bid+ask]
            tick = '.0001' if any(v % Decimal('.001') for v in values) else '.001'
            inferred_tick += 1
        fingerprint = hashlib.sha256(json.dumps([token, bid, ask], sort_keys=True).encode()).hexdigest()
        books[name] = {'asset_id': token, 'bids': bid, 'asks': ask,
                       'tick_size': str(tick), 'min_order_size': '5', 'hash': fingerprint}
        markets[name] = {'version': 'v1', 'active': True, 'closed': False, 'acceptingOrders': True,
                         'feesEnabled': fee_rate != 0, 'feeSchedule': {'rate': fee_rate, 'exponent': 1}}
        tokens[name] = token
    return prices, books, markets, tokens, {'missing_point_prices': missing, 'inferred_ticks': inferred_tick}


class SharedConnection:
    def __init__(self, conn): self.conn = conn
    def __getattr__(self, name): return getattr(self.conn, name)
    def close(self): pass
    def __enter__(self): self.conn.__enter__(); return self
    def __exit__(self, *error): return self.conn.__exit__(*error)


def quote_key(book, cost, shares, side, fee_function):
    fee = book.get('_paper_fee', {})
    return (tuple((str(x['price']), str(x['size'])) for x in book.get('bids', [])),
            tuple((str(x['price']), str(x['size'])) for x in book.get('asks', [])),
            book.get('tick_size'), book.get('min_order_size'), fee.get('rate'), fee.get('exponent'),
            cost, shares, side, id(fee_function))


def collect(c, day, capital, fee_rate, last, skipped, inferred):
    planner, risk, _, _ = load_engine()
    moves = [dict(x) for x in c.execute('SELECT * FROM paper_cash_movements ORDER BY rowid')]
    fills = [dict(x) for x in c.execute('SELECT * FROM fills ORDER BY id')]
    positions = [dict(x) for x in c.execute('SELECT * FROM positions WHERE target_date=?', (day,))]
    target = dict(c.execute('SELECT * FROM paper_targets WHERE target_date=?', (day,)).fetchone())
    events = [dict(x) for x in c.execute('SELECT * FROM paired_events ORDER BY id')]
    for x in events: x['details'] = json.loads(x['details'])
    checks = [x for x in events if x['event'] == 'HANDOVER_C_CHECK']
    computed = [x for x in checks if risk.computed_review(x['details']) or x['details'].get('comparison', {}).get('selected') == 'WAIT']
    handovers = [dict(x) for x in c.execute('SELECT * FROM handover_audit ORDER BY id')]
    for x in handovers: x['details'] = json.loads(x['details'])
    balance = c.execute('SELECT cash FROM paper_cash_account WHERE id=1').fetchone()[0]
    assert abs(balance-capital-sum(m['amount'] for m in moves)) < 1e-6
    buys = sum(-m['amount'] for m in moves if m['kind'] == 'BUY')
    proceeds = sum(m['amount'] for m in moves if m['kind'] in ('SELL','REDEEM'))
    assert abs(buys-sum(f['cost'] for f in fills)) < 1e-6
    for p in positions:
        acquired = sum(f['shares'] for f in fills if f['outcome'] == p['outcome'])
        sold = sum(json.loads(m['details_json']).get('shares', 0) for m in moves if m['kind'] == 'SELL' and m['outcome'] == p['outcome'])
        assert abs(acquired-sold-p['shares']) < 1e-6
    for f in fills:
        assert f['shares']*f['vwap'] >= 1-EPS
        assert f['shares'] >= 5-EPS
    held = {p['outcome']: p['shares'] for p in positions if p['shares'] > EPS}
    cash_delta = balance-capital
    estimate, unsold = 0., {}
    if last:
        raw_books, markets = last[1][1:3]
        executable = planner.cash._executable_books(c, day, raw_books, markets, {})
        for name, quantity in held.items():
            q = risk.sell_quote(executable[name], quantity) if name in executable else {'shares':0, 'net':0}
            estimate += q['net']
            if quantity-q['shares'] > EPS: unsold[name] = quantity-q['shares']
    chosen = [x for x in computed if x['details'].get('comparison', {}).get('selected') == 'MINIMAX']
    choices = [x['details'].get('comparison', {}) for x in computed]
    if handovers:
        category = 'MINIMAX_ADOPTED'
    elif any(x.get('selected') == 'SELL' and not x.get('minimax', {}).get('feasible') for x in choices):
        category = 'MINIMAX_INFEASIBLE'
    elif any(x.get('selected') == 'SELL' for x in choices):
        category = 'SELL_PREFERRED'
    elif choices:
        category = 'REVIEW_WAIT'
    else:
        category = 'NOT_COMPUTED'
    ordinary = [m for m in moves if m['kind'] == 'SELL' and 'minimum_exit_bid' in json.loads(m['details_json'])]
    stop = c.execute('SELECT * FROM live_minimax_exits WHERE target_date=?', (day,)).fetchone()
    naked = c.execute('SELECT * FROM legacy_naked_exits WHERE target_date=?', (day,)).fetchone()
    state = ('NO_TRADE' if not fills else 'ALL_POSITIONS_SOLD' if not held else
             'MAIN_EXIT_WITH_RESIDUALS' if ordinary and held.get(target.get('exit_outcome'), 0) < .01 else
             'MINIMAX_SELL_WITH_RESIDUALS' if stop else 'NAKED_EXIT_WITH_RESIDUALS' if naked else 'WINDOW_ENDED_OPEN')
    details = []
    for x in computed:
        comparison = x['details'].get('comparison', {})
        details.append({'timestamp':x['timestamp'], 'main':x['details'].get('main'),
                        'old_protection':x['details'].get('old_protection'), 'new_protection':x['details'].get('new_protection'),
                        'comparison': comparison, 'reference_loss_limit':x['details'].get('loss_limit'),
                        'allocation':x['details'].get('proposed_allocation'),
                        'original_review':x['details'].get('original_review')})
    return {'day':day, 'capital':capital, 'fee_rate_assumption':fee_rate,
            'category':category, 'exit_state':state, 'target_state':target['state'],
            'cash_remaining':balance, 'cash_change':cash_delta, 'buy_cost_including_fees':buys,
            'sell_net_proceeds':proceeds, 'fees':sum(m['fee'] for m in moves),
            'window_end_liquidation_estimate_pnl':cash_delta+estimate,
            'unliquidatable_residuals':unsold, 'residual_shares':held,
            'official_winner':None, 'final_settlement_pnl':None,
            'possible_settlement_pnl_range':[cash_delta, cash_delta+max(held.values(),default=0)],
            'buy_orders':len(fills), 'sell_orders':sum(m['kind']=='SELL' for m in moves),
            'computed_reviews':details, 'handover_executions':handovers,
            'minimax_additional_cost':sum(h['details']['added_capital'] for h in handovers),
            'precondition_rejections':dict(Counter(x['details'].get('reason') for x in checks if not risk.computed_review(x['details']))),
            'event_counts':dict(Counter(x['event'] for x in events)),
            'skipped_frames':dict(skipped), 'input_assumption_counts':dict(inferred),
            'movements':[dict(m, details_json=json.loads(m['details_json'])) for m in moves],
            'fills':fills, 'conservation_verified':True}
