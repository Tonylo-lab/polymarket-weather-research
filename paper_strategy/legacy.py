"""Old C policy with a durable, BUY-free exit after an uncovered initial flip.

Shared by paper and private live planning. This module never submits orders.
"""

from contextlib import closing, contextmanager, ExitStack

from copy import deepcopy

import json

from unittest.mock import patch

from src import paired_cash as cash, paired_ladder as ladder

from src.market_only import normalized

from src.polymarket_client import orderbook_levels

from . import profit_floor

PAPER_POLICY = 'LEGACY_NAKED_EXIT_PAPER_V1'

LIVE_POLICY = 'LEGACY_NAKED_EXIT_LIVE_V1'

EXIT_POLICY = 'UNCOVERED_INITIAL_MAIN_FLIP_EXIT_V1'

STRATEGY = 'MARKET_PAIRED_LADDER_V2_1_NAKED_EXIT_V1'

MIN_BUY_PRINCIPAL = 1.

EPS = 1e-8

def config(*, live=False, initial_cash=200.0):
    value = {'policy': LIVE_POLICY if live else PAPER_POLICY,
             'exit_price': .998, 'interval_seconds': 30,
             'minimum_buy_principal': MIN_BUY_PRINCIPAL,
             'naked_flip_exit': EXIT_POLICY, 'after_stop': 'global_local_today_relay',
             'confirmation_delay_seconds': 0, 'day_cooldown': False}
    value['main_profit_floor'] = profit_floor.rules()
    if not live:
        value['initial_cash'] = initial_cash
    return value

def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS legacy_naked_exits(
        target_date TEXT PRIMARY KEY, old_main TEXT NOT NULL, new_main TEXT NOT NULL,
        triggered_at TEXT NOT NULL, status TEXT NOT NULL,
        evidence_json TEXT NOT NULL, completed_at TEXT)''')

def initial_naked(c, day):
    fills = list(c.execute('SELECT * FROM fills WHERE target_date=? ORDER BY id', (day,)))
    if not fills or fills[0]['tranche'] != 'ENTRY_MAIN_ONLY':
        return None
    main = fills[0]['outcome']
    if any(f['outcome'] != main for f in fills):
        return None
    held = c.execute('SELECT shares FROM positions WHERE target_date=? AND outcome=?', (day, main)).fetchone()
    if not held or held[0] <= EPS:
        return None
    return {'old_main': main, 'entry_at': fills[0]['timestamp'], 'shares': held[0],
            'buy_cost': sum(f['cost'] for f in fills), 'buy_count': len(fills)}

class BatchConnection:
    """Keep the original cycle's connection contexts inside one preflight."""
    def __init__(self,c):self.c=c
    def __getattr__(self,name):return getattr(self.c,name)
    def close(self):pass
    def __enter__(self):return self
    def __exit__(self,*error):return False

@contextmanager
def adapters(*, grandfathered=None):
    """Install only inside one paper runner or private live proposal scope."""
    grandfathered = grandfathered or {}
    original_cycle, original_observe = ladder.cycle, cash._observe

    def cycle(directory, day, timestamp, prices, probabilities, books, **kwargs):
        if day in grandfathered:
            from . import engine
            return engine.cycle(directory, day, timestamp, prices, probabilities, books,
                                config=grandfathered[day], **kwargs)
        # Initialize schema outside the SAVEPOINT: legacy schema uses executescript.
        epoch=ladder.initialize(directory,handover_enabled=kwargs.get('handover_enabled'),
                                handover_policy=kwargs.get('handover_policy'))
        with closing(ladder.connect(ladder.path(directory))) as c,c:
            before=c.execute('SELECT COALESCE(MAX(id),0) FROM fills').fetchone()[0]
            c.execute('SAVEPOINT legacy_minimum_preflight')
            proxy=BatchConnection(c)
            with patch.object(ladder,'connect',return_value=proxy),patch.object(ladder,'initialize',return_value=epoch):
                result=original_cycle(directory,day,timestamp,prices,probabilities,deepcopy(books),**kwargs)
            planned=[dict(r) for r in c.execute('SELECT outcome,tranche,shares,vwap,cost FROM fills WHERE id>? ORDER BY id',(before,))]
            rejected=[dict(r,principal=r['shares']*r['vwap']) for r in planned
                      if r['shares']*r['vwap']<MIN_BUY_PRINCIPAL-EPS]
            if rejected:
                c.execute('ROLLBACK TO SAVEPOINT legacy_minimum_preflight')
                ladder._event(c,timestamp,day,'BUY_MINIMUM_BATCH_SKIPPED',
                              {'minimum_principal':MIN_BUY_PRINCIPAL,'rejected_legs':rejected,
                               'entire_cycle_rolled_back':True})
            c.execute('RELEASE SAVEPOINT legacy_minimum_preflight')
            return result

    def observe(directory, day, timestamp, client, active, hko):
        if day in grandfathered:
            return original_observe(directory, day, timestamp, client, active, hko)
        with closing(ladder.connect(ladder.path(directory))) as c, c:
            schema(c)
            stop = c.execute('SELECT * FROM legacy_naked_exits WHERE target_date=?', (day,)).fetchone()
            naked = initial_naked(c, day) if not stop else None
        if not stop and not naked:
            return original_observe(directory, day, timestamp, client, active, hko)

        prices, tokens, books, markets, errors, probabilities = {}, {}, {}, {}, {}, {}
        try:
            resolution = client.get_official_resolution()
            if not isinstance(resolution, dict):
                raise ValueError('invalid_official_resolution')
        except Exception as error:
            resolution = {'is_officially_resolved': False, 'resolution_status': type(error).__name__}
        try:
            if isinstance(client, cash.weather_relay.EventClient):
                prices, tokens, _ = client.market_inputs(markets)
            else:
                prices, tokens, _ = cash.market_inputs(client, details=markets)
            probabilities = normalized(prices)
            for name, token in tokens.items():
                try:
                    books[name] = client.fetch_clob_book(cash._asset_id(markets[name], name, token))
                except Exception as error:
                    errors[name] = type(error).__name__
        except Exception as error:
            errors['market'] = type(error).__name__
        with closing(ladder.connect(ladder.path(directory))) as c, c:
            executable = cash._executable_books(c, day, books, markets, errors)
            c.execute('INSERT OR IGNORE INTO paper_market_samples VALUES (?,?,?,?,?,?,?,?,?)',
                      (day,timestamp,json.dumps(prices),json.dumps(probabilities),json.dumps(books),
                       json.dumps(markets),json.dumps(resolution),json.dumps(errors),json.dumps(hko or {})))
            c.execute('UPDATE paper_targets SET last_observed_at=?,last_error=? WHERE target_date=?',
                      (timestamp,json.dumps(errors) if errors else None,day))
        with closing(ladder.connect(ladder.path(directory))) as c, c:
            if resolution.get('is_officially_resolved') and resolution.get('winner'):
                cash._redeem(c, day, timestamp, resolution)
                c.execute("UPDATE legacy_naked_exits SET status='RESOLVED',completed_at=COALESCE(completed_at,?) WHERE target_date=?", (timestamp,day))
                return
            target = c.execute('SELECT * FROM paper_targets WHERE target_date=?', (day,)).fetchone()
            # Completed stop events remain resolution-only, even if another
            # outcome later develops a .998 bid. Never relock their route.
            if stop and target['state'] in cash.weather_relay.DONE + cash.weather_relay.SKIPPED:
                return
            if not stop and active and probabilities and target['state']=='ACTIVE' and not target['exit_outcome']:
                main = max(probabilities, key=probabilities.get)
                if main != naked['old_main']:
                    evidence = dict(naked, policy=EXIT_POLICY, probabilities=probabilities,
                                    trigger_at=timestamp, new_main=main)
                    c.execute('INSERT INTO legacy_naked_exits VALUES(?,?,?,?,?,?,NULL)',
                              (day,naked['old_main'],main,timestamp,'PENDING',json.dumps(evidence,sort_keys=True)))
                    c.execute("UPDATE paper_targets SET state='EXIT_PENDING' WHERE target_date=?", (day,))
                    ladder._event(c,timestamp,day,'NAKED_MAIN_FLIP_EXIT_TRIGGER',evidence)
                    stop=c.execute('SELECT * FROM legacy_naked_exits WHERE target_date=?',(day,)).fetchone()
            if stop:
                sell_stop(c,day,timestamp,stop,executable)
                return

        # Reuse the original lifecycle/BUY logic with the already fetched frame;
        # no duplicate network calls and no alternative ordinary allocation.
        assets={cash._asset_id(markets[n],n,t):n for n,t in tokens.items() if n in markets}
        def cached_book(asset):
            name=assets.get(asset)
            if name not in books:
                raise ValueError('cached_book_unavailable')
            return deepcopy(books[name])
        def cached_inputs(details):
            details.update(deepcopy(markets))
            return dict(prices),dict(tokens),False
        with ExitStack() as stack:
            stack.enter_context(patch.object(client,'get_official_resolution',return_value=deepcopy(resolution)))
            stack.enter_context(patch.object(client,'fetch_clob_book',side_effect=cached_book))
            if isinstance(client,cash.weather_relay.EventClient):
                stack.enter_context(patch.object(client,'market_inputs',side_effect=cached_inputs))
            else:
                stack.enter_context(patch.object(cash,'market_inputs',side_effect=lambda _,details:cached_inputs(details)))
            return original_observe(directory,day,timestamp,client,active,hko)

    with ExitStack() as stack:
        from live_strategy.planner import executable_minimax
        stack.enter_context(patch.object(cash,'CashPolicy',profit_floor.CashPolicy))
        stack.enter_context(patch.object(ladder,'cycle',cycle))
        stack.enter_context(patch.object(cash,'_observe',observe))
        stack.enter_context(patch.object(ladder,'solve_handover',executable_minimax))
        yield

def sell_stop(c,day,timestamp,stop,books):
    name=stop['old_main']
    held=c.execute('SELECT shares FROM positions WHERE target_date=? AND outcome=?',(day,name)).fetchone()
    quantity=held[0] if held else 0.
    book=books.get(name)
    if quantity>EPS and book:
        clean=deepcopy(book)
        clean['bids']=[{'price':str(p),'size':str(q)} for p,q in orderbook_levels(book,'BID') if 0<p<=1 and q>0]
        quote=cash.paper_execution.quote(clean,shares=quantity,side='SELL')
        minimum=float(book.get('min_order_size') or ladder.MIN_ORDER_SHARES)
        if quantity+EPS>=minimum and quote['shares']+EPS>=minimum:
            detail={'shares':quote['shares'],'vwap':quote['vwap'],'consumed':quote['consumed'],
                    'fee_schedule':book['_paper_fee'],'exit_policy':EXIT_POLICY,
                    'exit_trigger':stop['triggered_at'],'old_main':name,'new_main':stop['new_main']}
            if cash._move(c,f'SELL:{day}:{timestamp}:{name}',timestamp,day,name,'SELL',quote['net'],quote['gross'],quote['fee'],detail):
                c.execute('UPDATE positions SET shares=MAX(0,shares-?) WHERE target_date=? AND outcome=?',(quote['shares'],day,name))
                cash._use_depth(c,day,name,book['_paper_fee']['book_key'],'bids',quote['consumed'])
                ladder._event(c,timestamp,day,'NAKED_MAIN_FLIP_EXIT_SELL',dict(detail,net=quote['net'],fee=quote['fee']))
        else:
            ladder._event(c,timestamp,day,'NAKED_MAIN_FLIP_EXIT_WAIT',{'reason':'minimum_or_bid_depth','shares':quantity})
    elif quantity>EPS:
        ladder._event(c,timestamp,day,'NAKED_MAIN_FLIP_EXIT_WAIT',{'reason':'no_executable_book_or_fee','shares':quantity})
    remaining=c.execute('SELECT COALESCE(SUM(shares),0) FROM positions WHERE target_date=?',(day,)).fetchone()[0]
    if remaining<=EPS:
        c.execute("UPDATE legacy_naked_exits SET status='SOLD',completed_at=? WHERE target_date=?",(timestamp,day))
        c.execute("UPDATE paper_targets SET state='EXITED_WAIT_RESOLUTION' WHERE target_date=?",(day,))
        ladder._event(c,timestamp,day,'NAKED_MAIN_FLIP_EXIT_COMPLETE',{'policy':EXIT_POLICY,'next':'global_relay','official_winner':None})
