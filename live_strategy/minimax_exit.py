"""Live-only executable Minimax versus liquidation review; no order submission."""

from contextlib import closing, contextmanager, ExitStack

from copy import deepcopy

import hashlib, json, math, sqlite3

from unittest.mock import patch

from live_test import probe

from live_strategy import planner

from src import paired_cash as cash, paired_ladder as ladder

from src.market_only import normalized

from src.polymarket_client import orderbook_levels

POLICY = 'LIVE_MINIMAX_REJECT_EXIT_V1'

COMPARISON_POLICY = 'LIVE_MINIMAX_VS_SELL_V1'

LOSS_LIMIT = .02

EPS = 1e-8

def previous_rules():
    return {'policy': POLICY, 'maximum_loss_fraction': LOSS_LIMIT,
            'trigger': 'computed original C Minimax review rejected; preconditions do not trigger',
            'exit': 'durable SELL-only for this event; all legally sellable strategy-owned legs',
            'residual': 'retain shares and all buy costs; no-bid/minimum/precision residuals await strict resolution',
            'no_fill': 'proved empty SELL suppresses unchanged bid liquidity; unknown orders remain under checking'}

def rules():
    return dict(previous_rules(),
                trigger='computed original C review compares executable Minimax and direct sale; preconditions do not trigger',
                loss_limit_role='2% is an audit reference; the less-loss comparison replaces the standalone risk veto',
                comparison={'policy': COMPARISON_POLICY,
                            'scope': 'same Main, old Protection and new Protection settlement scenarios; not a global loss guarantee',
                            'basis': 'all event BUY costs including fees, prior net SELL/REDEEM, executable orders and unsold residual payouts',
                            'selection': 'higher worst net PnL wins; ties choose SELL; unavailable sell valuation waits',
                            'execution': 'original cash budget, precision, depth and per-leg 1U BUY minimum remain',
                            'after_entry': 'original Minimax lifecycle; no half-loss exit added'})

def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS live_minimax_exits(
        target_date TEXT PRIMARY KEY, triggered_at TEXT NOT NULL, status TEXT NOT NULL,
        evidence_json TEXT NOT NULL, disposition_json TEXT NOT NULL, completed_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS live_minimax_empty_bids(
        target_date TEXT NOT NULL,outcome TEXT NOT NULL,signature TEXT NOT NULL,
        batch TEXT NOT NULL,PRIMARY KEY(target_date,outcome))''')

def signature(book):
    value = {'bids': orderbook_levels(book, 'BID'), 'minimum': book.get('min_order_size'),
             'tick': book.get('tick_size'), 'fee': {k:book.get('_paper_fee',{}).get(k) for k in ('rate','exponent')}}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

class RejectedReview(Exception):
    def __init__(self, details):
        self.details = deepcopy(details)

class ChosenMinimax(RejectedReview):
    pass

class DeferredReview(RejectedReview):
    pass

def computed_review(details):
    return ({'projected_z', 'projected_after', 'proposed_allocation', 'remaining_cash'} <= details.keys()
            and isinstance(details['projected_z'], (int, float)) and math.isfinite(details['projected_z']))

def computed_rejection(details):
    comparison = details.get('comparison', {})
    selected_sell = (details.get('reason') == 'direct_sell_selected'
                     and comparison.get('policy') == COMPARISON_POLICY
                     and comparison.get('selected') == 'SELL'
                     and comparison.get('direct_sell', {}).get('known') is True)
    return (details.get('accepted') is False and
            details.get('reason') in ('minimum_order_or_depth','insufficient_cash','loss_limit_exceeded',
                                      'no_worst_case_improvement','tail_not_break_even', 'direct_sell_selected')
            and (details.get('reason') != 'direct_sell_selected' or selected_sell)
            and computed_review(details))

def sell_quote(book, quantity):
    """The same conservative legal quote for both comparison and SELL actions."""
    clean = deepcopy(book)
    clean['bids'] = [{'price': str(p), 'size': str(q)} for p, q in orderbook_levels(book, 'BID')
                     if 0 < p <= 1 and q > 0]
    quote = planner.executable_quote(clean, shares=quantity, side='SELL')
    quote['fee_cap'] = quote['fee']
    if quote['shares'] > 0:
        quote['fee'] = cash.paper_execution.fee(quote['shares'], quote['vwap'], book['_paper_fee'])
        quote['net'] = quote['gross'] - quote['fee']
    minimum = float(book.get('min_order_size') or ladder.MIN_ORDER_SHARES)
    if quote['shares'] + EPS < minimum or quote['net'] <= 0:
        return dict(quote, shares=0., gross=0., fee=0., net=0., consumed=[])
    return quote

def compare(c, day, main, protection, probs, books, details):
    """Side-effect-free comparison. Unknown data never means worthless shares."""
    names = (main, details['old_protection'], protection)
    held, cost = ladder._portfolio(c, day)
    realized = c.execute("SELECT COALESCE(SUM(amount),0) FROM paper_cash_movements "
                         "WHERE target_date=? AND kind IN ('SELL','REDEEM')", (day,)).fetchone()[0]
    scenarios = sorted(set(probs) | set(held) | set(names))
    result = {'policy': COMPARISON_POLICY, 'scope': list(names), 'total_buy_cost': cost,
              'prior_net_proceeds': realized, 'reference_loss_limit': details['loss_limit']}
    sale = {'known': True, 'net_proceeds': 0., 'legs': {}, 'complete_liquidation': True}
    residuals = dict(held)
    for name, quantity in sorted(held.items()):
        if quantity <= EPS:
            continue
        book = books.get(name)
        if not book:
            sale.update(known=False, reason='book_or_fee_unavailable', unavailable_outcome=name)
            break
        try:
            q = sell_quote(book, quantity)
        except (KeyError, ValueError, TypeError, ArithmeticError, probe.ProbeError):
            sale.update(known=False, reason='invalid_book_or_fee', unavailable_outcome=name)
            break
        residuals[name] = max(0., quantity - q['shares'])
        sale['net_proceeds'] += q['net']
        sale['legs'][name] = {'held_shares': quantity, 'sell_shares': q['shares'],
                              'net_proceeds': q['net'], 'fee': q['fee'], 'limit': q['vwap'],
                              'residual_shares': residuals[name]}
        sale['complete_liquidation'] &= residuals[name] <= EPS
    if sale['known']:
        pnl = {n: realized + sale['net_proceeds'] + residuals.get(n, 0.) - cost for n in scenarios}
        sale.update(pnl_by_outcome=pnl, worst_pnl=min(pnl[n] for n in names),
                    all_outcome_worst_pnl=min(pnl.values()), residuals=residuals)
    allocation = details['proposed_allocation']
    after = details['projected_after']
    feasible = isinstance(after, dict) and all(n in after and math.isfinite(after[n]) for n in names)
    reason = None if feasible else 'original_execution_review_failed'
    added_cost = 0.
    for name in names:
        leg = allocation[name]
        if not all(isinstance(leg[k], (int, float)) and math.isfinite(leg[k]) and leg[k] >= 0
                   for k in ('cost', 'shares')):
            feasible, reason = False, 'invalid_allocation'
            continue
        added_cost += leg['cost']
        if leg['cost'] > EPS:
            price = leg.get('vwap')
            if not isinstance(price, (int, float)) or not math.isfinite(price) or not 0 < price < 1:
                feasible, reason = False, 'invalid_buy_price'
            elif leg['shares'] * price < 1. - EPS:
                feasible, reason = False, 'buy_principal_below_1U'
    if added_cost > details['remaining_cash'] + EPS:
        feasible, reason = False, 'insufficient_cash'
    minimax = {'feasible': feasible, 'reason': reason, 'added_cost': added_cost}
    if feasible:
        pnl = {n: realized + held.get(n, 0.) + allocation.get(n, {}).get('shares', 0.)
                  - cost - added_cost for n in scenarios}
        # Require the original executable check to agree with the actual legs.
        if any(abs(pnl[n] - realized - after[n]) > 1e-7 for n in names):
            minimax.update(feasible=False, reason='allocation_projection_mismatch')
        else:
            minimax.update(pnl_by_outcome=pnl,
                           worst_pnl=min(details['projected_z'] + realized, min(pnl[n] for n in names)),
                           all_outcome_worst_pnl=min(pnl.values()))
    result.update(minimax=minimax, direct_sell=sale)
    if not sale['known']:
        result.update(selected='WAIT', reason='direct_sale_valuation_unavailable')
    elif minimax['feasible'] and minimax['worst_pnl'] > sale['worst_pnl'] + EPS:
        result.update(selected='MINIMAX', reason='minimax_less_loss')
    else:
        result.update(selected='SELL', reason='direct_sell_less_loss_or_tied' if minimax['feasible']
                      else 'minimax_not_executable')
    return result

def sell_all(c, day, timestamp, stop, books, *, raw_books=None, markets=None):
    """Only private projection sells; impossible orders never become actions."""
    disposition = {}; waiting = False
    for held in list(c.execute('SELECT * FROM positions WHERE target_date=? AND shares>? ORDER BY outcome',(day,EPS))):
        name, quantity = held['outcome'], held['shares']; book = books.get(name)
        if not book:
            market = (markets or {}).get(name, {})
            closed = market.get('closed') is True or market.get('acceptingOrders') is False
            disposition[name] = {'shares': quantity, 'reason': 'market_not_accepting' if closed else 'book_or_fee_unavailable'}
            waiting |= not closed
            continue
        sig = signature(book)
        blocked = c.execute('SELECT signature FROM live_minimax_empty_bids WHERE target_date=? AND outcome=?',(day,name)).fetchone()
        if blocked and blocked[0] == sig:
            disposition[name] = {'shares': quantity, 'reason': 'proved_empty_unchanged_bids'}
            continue
        quote = sell_quote(book, quantity)
        if quote['shares'] <= 0:
            disposition[name] = {'shares':quantity, 'reason':'no_legal_positive_net_bid_quantity'}
            continue
        detail = {'shares':quote['shares'],'vwap':quote['vwap'],'consumed':quote['consumed'],
                  'fee_schedule':book['_paper_fee'],'exit_policy':POLICY,
                  'exit_trigger':stop['triggered_at'],'bid_signature':sig,
                  'fee_cap_raw':planner.raw(quote['fee_cap'])}
        if cash._move(c,f'SELL:{day}:{timestamp}:{name}',timestamp,day,name,'SELL',quote['net'],quote['gross'],quote['fee'],detail):
            c.execute('UPDATE positions SET shares=MAX(0,shares-?) WHERE target_date=? AND outcome=?',(quote['shares'],day,name))
            cash._use_depth(c,day,name,book['_paper_fee']['book_key'],'bids',quote['consumed'])
            ladder._event(c,timestamp,day,'MINIMAX_REJECT_EXIT_SELL',dict(detail,net=quote['net']))
        remainder = max(0,quantity-quote['shares'])
        # The quote used all currently executable legal depth. Remainders stay
        # owned; they may become sellable on a later distinct observation.
        disposition[name] = {'shares':remainder,'reason':'remaining_bid_depth_or_precision' if remainder>EPS else 'planned_sale'}
    # Once routing was released, a later missing book cannot reacquire it.
    status = 'PENDING' if waiting and stop['status']=='PENDING' else 'RESIDUAL_WAIT'
    c.execute('UPDATE live_minimax_exits SET status=?,disposition_json=?,completed_at=? WHERE target_date=?',
              (status,json.dumps(disposition,sort_keys=True),None if status=='PENDING' else timestamp,day))
    c.execute('UPDATE paper_targets SET state=? WHERE target_date=?',('EXIT_PENDING' if status=='PENDING' else 'EXITED_WAIT_RESOLUTION',day))
    ladder._event(c,timestamp,day,'MINIMAX_REJECT_EXIT_DISPOSITION',{'policy':POLICY,'status':status,'residuals':disposition,'official_winner':None})

@contextmanager
def adapters(*, grandfathered=None):
    """Layer outside shared legacy adapters; original paper files stay intact."""
    original_cycle, original_observe, original_gate = ladder.cycle, cash._observe, ladder._handover_c_gate
    original_remaining = cash.weather_relay._observe_remaining
    excluded = grandfathered or {}

    def gate(c,state,timestamp,day,main,protection,probs,books):
        original_event = ladder._event
        original_solve = ladder.solve_handover
        computed = []
        def solve(orderbooks, names, held, cost, available):
            missing = [n for n in set(names) | {n for n, q in held.items() if q > EPS}
                       if not orderbooks.get(n) or '_paper_fee' not in orderbooks[n]]
            if missing:
                raise DeferredReview({'accepted': False, 'reason': 'comparison_unavailable',
                                      'comparison': {'policy': COMPARISON_POLICY, 'selected': 'WAIT',
                                                     'reason': 'book_or_fee_unavailable',
                                                     'unavailable_outcomes': sorted(missing)}})
            plan = original_solve(orderbooks, names, held, cost, available)
            computed.append(plan)
            return plan
        def event(conn, stamp, target, kind, details):
            if kind=='HANDOVER_C_CHECK' and computed_review(details):
                comparison = compare(conn, day, main, protection, probs, books, details)
                details.update(original_review={'accepted': details['accepted'], 'reason': details.get('reason')},
                               comparison=comparison)
                choice = comparison['selected']
                details.update(accepted=choice == 'MINIMAX', reason={'MINIMAX': 'minimax_less_loss',
                               'SELL': 'direct_sell_selected', 'WAIT': 'comparison_unavailable'}[choice])
                if choice == 'SELL':
                    raise RejectedReview(details)
                if choice == 'WAIT':
                    raise DeferredReview(details)
                original_event(conn, stamp, target, kind, details)
                raise ChosenMinimax(details)
            return original_event(conn,stamp,target,kind,details)
        with patch.object(ladder,'_event',event), patch.object(ladder, 'solve_handover', solve):
            try:
                return original_gate(c,state,timestamp,day,main,protection,probs,books)
            except ChosenMinimax as chosen:
                return {'old': chosen.details['old_protection'], 'plan': computed[-1], 'gate': chosen.details}

    def cycle(directory,day,timestamp,prices,probabilities,books,**kwargs):
        if day in excluded:
            return original_cycle(directory,day,timestamp,prices,probabilities,books,**kwargs)
        with closing(ladder.connect(ladder.path(directory))) as c,c:
            schema(c)
            stop=c.execute('SELECT * FROM live_minimax_exits WHERE target_date=?',(day,)).fetchone()
            if stop:
                return sell_all(c,day,timestamp,stop,books)
        try:
            return original_cycle(directory,day,timestamp,prices,probabilities,books,**kwargs)
        except DeferredReview as deferred:
            with closing(ladder.connect(ladder.path(directory))) as c,c:
                ladder._event(c,timestamp,day,'HANDOVER_C_CHECK',deferred.details)
                ladder._event(c,timestamp,day,'MINIMAX_COMPARISON_WAIT',deferred.details['comparison'])
            return
        except RejectedReview as rejected:
            # The nested legacy SAVEPOINT rolls back role/tier/BUY changes.
            # Persist the rejection outside it, then build SELL-only actions.
            with closing(ladder.connect(ladder.path(directory))) as c,c:
                evidence={'policy':POLICY,'gate':rejected.details,
                          'held':{r['outcome']:{'shares':r['shares'],'total_cost':r['total_cost']}
                                  for r in c.execute('SELECT * FROM positions WHERE target_date=?',(day,))}}
                c.execute('INSERT INTO live_minimax_exits VALUES(?,?,?, ?,?,NULL)',
                          (day,timestamp,'PENDING',json.dumps(evidence,sort_keys=True),'{}'))
                ladder._event(c,timestamp,day,'HANDOVER_C_CHECK',rejected.details)
                ladder._event(c,timestamp,day,'MINIMAX_REJECT_EXIT_TRIGGER',evidence)
                stop=c.execute('SELECT * FROM live_minimax_exits WHERE target_date=?',(day,)).fetchone()
                return sell_all(c,day,timestamp,stop,books)

    def observe(directory,day,timestamp,client,active,hko):
        with closing(ladder.connect(ladder.path(directory))) as c,c:
            schema(c)
            stop=c.execute('SELECT * FROM live_minimax_exits WHERE target_date=?',(day,)).fetchone()
        if not stop:
            return original_observe(directory,day,timestamp,client,active,hko)
        # A stop is independent of Main ask availability and remains SELL-only
        # after route release, restart, or the old Main regaining the lead.
        prices={};tokens={};markets={};books={};errors={};resolution={}
        try:
            resolution=client.get_official_resolution()
            if not isinstance(resolution,dict):raise ValueError('invalid_resolution')
        except Exception as error:resolution={'resolution_status':type(error).__name__}
        try:
            if isinstance(client,cash.weather_relay.EventClient):prices,tokens,_=client.market_inputs(markets)
            else:prices,tokens,_=cash.market_inputs(client,details=markets)
            for name,token in tokens.items():
                try:books[name]=client.fetch_clob_book(cash._asset_id(markets[name],name,token))
                except Exception as error:errors[name]=type(error).__name__
        except Exception as error:errors['market']=type(error).__name__
        probabilities={}
        try:probabilities=normalized(prices)
        except ValueError:errors['probabilities']='unavailable'
        with closing(ladder.connect(ladder.path(directory))) as c,c:
            executable=cash._executable_books(c,day,books,markets,errors)
            c.execute('INSERT OR IGNORE INTO paper_market_samples VALUES(?,?,?,?,?,?,?,?,?)',
                      (day,timestamp,json.dumps(prices),json.dumps(probabilities),json.dumps(books),
                       json.dumps(markets),json.dumps(resolution),json.dumps(errors),json.dumps(hko or {})))
            c.execute('UPDATE paper_targets SET last_observed_at=?,last_error=? WHERE target_date=?',
                      (timestamp,json.dumps(errors) if errors else None,day))
            if resolution.get('is_officially_resolved') and resolution.get('winner'):
                cash._redeem(c,day,timestamp,resolution)
                c.execute("UPDATE live_minimax_exits SET status='RESOLVED',completed_at=? WHERE target_date=?",(timestamp,day))
                return
            sell_all(c,day,timestamp,stop,executable,raw_books=books,markets=markets)

    def remaining(directory,day,metadata,timestamp,client):
        with closing(ladder.connect(ladder.path(directory))) as c:
            exists=c.execute('SELECT 1 FROM live_minimax_exits WHERE target_date=?',(day,)).fetchone()
        if exists:
            return cash._observe(directory,day,timestamp,cash.weather_relay.EventClient(client,metadata),False,{})
        return original_remaining(directory,day,metadata,timestamp,client)

    with ExitStack() as stack:
        stack.enter_context(patch.object(ladder,'HANDOVER_C_LOSS_LIMIT',LOSS_LIMIT))
        stack.enter_context(patch.object(ladder,'_handover_c_gate',gate))
        stack.enter_context(patch.object(ladder,'cycle',cycle))
        stack.enter_context(patch.object(cash,'_observe',observe))
        stack.enter_context(patch.object(cash.weather_relay,'_observe_remaining',remaining))
        yield

def persist(store,plan):
    """Durable intent only, before POST. Never import projected sale/completion."""
    with closing(sqlite3.connect(plan['database'])) as source, closing(sqlite3.connect(ladder.path(store.projection))) as c,c:
        source.row_factory=c.row_factory=sqlite3.Row
        schema(c)
        for row in source.execute('SELECT * FROM live_minimax_exits'):
            old=c.execute('SELECT * FROM live_minimax_exits WHERE target_date=?',(row['target_date'],)).fetchone()
            if old:
                probe.require(old['triggered_at']==row['triggered_at'] and old['evidence_json']==row['evidence_json'],'MINIMAX_EXIT_INTENT_CHANGED')
                continue
            evidence=json.loads(row['evidence_json']);gate=evidence.get('gate',{})
            state=c.execute('SELECT * FROM paired_state WHERE target_date=?',(row['target_date'],)).fetchone()
            held={r['outcome']:{'shares':r['shares'],'total_cost':r['total_cost']} for r in c.execute('SELECT * FROM positions WHERE target_date=?',(row['target_date'],))}
            probe.require(evidence.get('policy')==POLICY and computed_rejection(gate) and state
                          and state['main']==gate['main'] and state['bankroll_before']==gate['bankroll']
                          and abs(gate['loss_limit']-LOSS_LIMIT*gate['bankroll'])<EPS
                          and evidence.get('held')==held and held.get(gate['old_protection'],{}).get('shares',0)>EPS,
                          'MINIMAX_EXIT_TRIGGER_NOT_BOUND')
            c.execute('INSERT INTO live_minimax_exits VALUES(?,?,?, ?,?,NULL)',
                      (row['target_date'],row['triggered_at'],'PENDING',row['evidence_json'],'{}'))
            c.execute("UPDATE paper_targets SET state='EXIT_PENDING' WHERE target_date=?",(row['target_date'],))
            ladder._event(c,row['triggered_at'],row['target_date'],'MINIMAX_REJECT_EXIT_DURABLE',evidence)

def authorize(c,plan,action):
    stop=c.execute('SELECT * FROM live_minimax_exits WHERE target_date=?',(action['target_date'],)).fetchone()
    held=c.execute('SELECT shares FROM positions WHERE target_date=? AND outcome=?',(action['target_date'],action['outcome'])).fetchone()
    metadata=planner.relay.metadata_for(c,action['target_date'])
    probe.require(action['kind']=='SELL' and stop and stop['status'] in ('PENDING','RESIDUAL_WAIT')
                  and stop['triggered_at']==action.get('exit_trigger') and held
                  and 0<action['shares_raw']<=planner.raw(held[0])+1
                  and metadata['outcome_identity'][action['outcome']]['asset_id']==action['asset_id'],
                  'MINIMAX_EXIT_ACTION_NOT_BOUND')

def is_exit_plan(plan):
    return bool(plan['actions']) and all(a['kind']=='SELL' and a.get('exit_policy')==POLICY for a in plan['actions'])

def remember_empty(store,plan,batch,indices):
    """Only callers with terminal-empty + full wallet proof may record this."""
    with closing(sqlite3.connect(ladder.path(store.projection))) as c,c:
        schema(c)
        for index in indices:
            action=plan['actions'][index]
            if action.get('exit_policy')!=POLICY:continue
            details=json.loads(action['details_json'])
            c.execute('INSERT OR REPLACE INTO live_minimax_empty_bids VALUES(?,?,?,?)',
                      (action['target_date'],action['outcome'],details['bid_signature'],batch))
