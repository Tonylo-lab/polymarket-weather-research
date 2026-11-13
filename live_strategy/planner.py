"""Offline execution arithmetic; live proposal/gateway/client code excluded."""
from copy import deepcopy
from decimal import Decimal,ROUND_FLOOR,ROUND_CEILING
from unittest.mock import patch
from live_test import probe,live_gateway as lg
from live_test.fok_precision import quantity_step,amounts
from src import paired_cash as cash,paired_ladder as ladder,paper_execution as pe,weather_relay as relay
from src.polymarket_client import orderbook_levels
UNIT=1_000_000
ORIGINAL_QUOTE=pe.quote
ORIGINAL_SOLVE=ladder.solve_handover
ORIGINAL_COST_FOR_SHARES=ladder._cost_for_shares


def raw(value):return int((Decimal(str(value))*UNIT).to_integral_value(rounding=ROUND_FLOOR))


def executable_quote(book,*,cost=None,shares=None,side='BUY'):
    """FOK size precision and worst-limit fee reservation inside all previews.

    This is execution normalization, not a different ladder or cash policy.
    No network or financial side effects, including during SQL SAVEPOINTs.
    """
    book=deepcopy(book);schedule=book['_paper_fee']
    levels=orderbook_levels(book,'BUY' if side=='BUY' else 'BID')
    # Orders cannot be signed at 1; a bid at 1 may still fill a legal sell limit.
    levels=[(p,q) for p,q in levels if 0<p<1 or (side=='SELL' and p==1)]
    tick=Decimal(str(book.get('tick_size') or '.001'))
    tick_raw=raw(tick);probe.require(tick_raw>0,'INVALID_TICK')
    book['asks' if side=='BUY' else 'bids']=[{'price':str(p),'size':str(q)} for p,q in levels]
    base=ORIGINAL_QUOTE(book,shares=0,side=side)
    depth=sum(q for p,q in levels)
    rate,exponent=Decimal(str(schedule['rate'])),Decimal(str(schedule['exponent']))
    # Each price has its own legal BUY quantity grid. Enumerate depth bands;
    # a binary search across all quantities would skip valid grid points when
    # the worst consumed ask changes. Always round DOWN within the budget.
    selected=None;full=None;cumulative=Decimal(0);previous=0
    for p,size in levels:
        cumulative+=Decimal(str(size));available=raw(cumulative)
        price=raw(p)
        if price==UNIT:price=UNIT-tick_raw
        if not 0<price<UNIT or price%tick_raw:previous=available;continue
        step=quantity_step(side,price)
        ceiling=min(available,raw(shares)) if shares is not None else available
        def candidate(n):
            q=n*step
            if not q:return None
            maker,taker=amounts(side,q,price)
            principal=maker if side=='BUY' else taker
            fee=lg.fee_cap(q,rate,exponent) if rate else 0
            return q,price,principal,fee
        hi=ceiling//step
        maximum=candidate(hi)
        if maximum and maximum[0]>previous and (full is None or maximum[0]>full[0]):full=maximum
        lo=0
        while lo<hi:
            mid=(lo+hi+1)//2;value=candidate(mid)
            if cost is None or value[2]+(value[3] if side=='BUY' else 0)<=raw(cost):lo=mid
            else:hi=mid-1
        value=candidate(lo)
        if value and value[0]>previous and (selected is None or value[0]>selected[0]):selected=value
        previous=available
    base.update(depth_shares=depth,depth_cost=(full[2]+(full[3] if side=='BUY' else 0))/UNIT if full else 0.)
    if selected is None:
        base.update(limited=bool((cost or 0)>0 or (shares or 0)>0));return base
    q,price,principal,fee=selected
    f=ORIGINAL_QUOTE(book,shares=q/UNIT,side=side)
    probe.require(f['shares']+1e-8>=q/UNIT,'FOK_QUANTITY_EXCEEDS_DEPTH')
    f.update(cost=(principal+fee if side=='BUY' else principal)/UNIT,gross=principal/UNIT,fee=fee/UNIT,
             net=(principal-fee)/UNIT,shares=q/UNIT,vwap=price/UNIT,depth_shares=base['depth_shares'],depth_cost=base['depth_cost'],
             limited=(cost is not None and principal+(fee if side=='BUY' else 0)<raw(cost)-10) or (shares is not None and q<raw(shares)))
    return f


def executable_cost_for_shares(book,shares):
    """Least reserved BUY cost covering a strategy's continuous share target.

    A target is a lower bound, unlike executable_quote's shares upper bound.
    Round up on each price's legal grid, then let the original allocation and
    its cost-capped quote enforce available cash. Genuine short depth is inf.
    """
    requested=Decimal(str(shares))*UNIT
    probe.require(requested.is_finite() and requested>=0,'INVALID_TARGET_SHARES')
    if not requested:return 0.
    required=int(requested.to_integral_value(rounding=ROUND_CEILING))
    schedule=(book or {}).get('_paper_fee')
    if not isinstance(schedule,dict) or not {'rate','exponent'}<=schedule.keys():return float('inf')
    rate=Decimal(str(schedule['rate']));exponent=Decimal(str(schedule['exponent']))
    tick=raw(book.get('tick_size') or '.001');probe.require(tick>0,'INVALID_TICK')
    cumulative=Decimal(0);previous=0;best=None
    for price,size in orderbook_levels(book,'BUY'):
        if not 0<price<1:continue
        cumulative+=Decimal(str(size));available=raw(cumulative);limit=raw(price)
        if not 0<limit<UNIT or limit%tick:previous=available;continue
        step=quantity_step('BUY',limit)
        quantity=((max(required,previous+1)+step-1)//step)*step
        if quantity<=available:
            principal,_=amounts('BUY',quantity,limit)
            cost=principal+(lg.fee_cap(quantity,rate,exponent) if rate else 0)
            if best is None or cost<best:best=cost
        previous=available
    return best/UNIT if best is not None else float('inf')


def executable_minimax(books,names,held,current_cost,cash_available):
    # Keep the accepted optimizer objective; normalize only its executable legs
    # before the original C gate evaluates actual worst-case PnL.
    def reserve_fee(quantity,price,schedule):
        return quantity*float(schedule['rate'])*(.25**float(schedule['exponent']))
    with patch.object(pe,'quote',ORIGINAL_QUOTE),patch.object(pe,'fee',reserve_fee),patch.object(pe,'ask_cost',side_effect=lambda p,s:p+reserve_fee(1,p,s)):
        plan=ORIGINAL_SOLVE(books,names,held,current_cost,cash_available)
    total=0.
    for name in names:
        amount=plan['allocation'][name]['cost']
        q=executable_quote(books[name],cost=amount) if amount>0 else {'cost':0.,'shares':0.,'vwap':None}
        if q['shares']+1e-9<float(books[name].get('min_order_size') or 5):q={'cost':0.,'shares':0.,'vwap':None}
        plan['allocation'][name]={k:q[k] for k in ('cost','shares','vwap')};total+=q['cost']
    after={name:held.get(name,0)+plan['allocation'][name]['shares']-current_cost-total for name in names}
    plan.update(after=after,z=min(after.values()),added_capital=total,cash_unused=cash_available-total,
                break_even_feasible=plan['break_even_feasible'] and min(after.values())>=-1e-7)
    return plan
