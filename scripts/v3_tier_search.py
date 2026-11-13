"""Frozen ladder candidates and recovery adapter; no optimizer run on import."""
import json
from contextlib import contextmanager
from unittest.mock import patch
from scripts import v3_backtest as base
OUT=base.ROOT/'samples'
ORIGINAL=((.60,.20,.70),(.70,.35,.80),(.80,.50,.90),(.90,.60,1.0))
PROFILES=[(.60,.70,.80,.90),(.65,.75,.85,.90),(.70,.80,.90),(.75,.85,.90),(.80,.85,.90)]
ASSUMED={} # Historical assumed labels are not automatically loaded.


def configs():
    result=[]
    for thresholds in PROFILES:
        for cap in [.30,.45,.60]:
            weights=[1/3,7/12,5/6,1.] if len(thresholds)==4 else [1/3,2/3,1.]
            budgets=[round(cap*w,8) for w in weights]
            ident='p'+'_'.join(str(round(t*100)) for t in thresholds)+'_cap'+str(round(cap*100))
            if thresholds==PROFILES[0] and cap==.60:
                ident='baseline'
                budgets=[.20,.35,.50,.60]
            result.append(dict(id=ident,thresholds=list(thresholds),budgets=budgets,cap=cap,shape='regular'))
    for thresholds in [(.70,.80,.90),(.75,.85,.90)]:
        for label,budgets in [('backloaded',[.075,.225,.45]),('frontloaded',[.20,.35,.45])]:
            ident='p'+'_'.join(str(round(t*100)) for t in thresholds)+'_cap45_'+label
            result.append(dict(id=ident,thresholds=list(thresholds),budgets=budgets,cap=.45,shape=label))
    assert len(result)==19
    return result


@contextmanager
def candidate_scope(cfg):
    planner,_,_,_=base.load_engine()
    ladder=planner.ladder
    from src.paired_cash_policy import CashPolicy
    original_recovery=CashPolicy.recovery
    def recovery(self,conn,state,timestamp,day,prices,probs,books,tier):
        old_tier=sum(probs[state['main']]+1e-7>=t for t,_,_ in ORIGINAL)
        with patch.object(ladder,'TIERS',ORIGINAL):
            return original_recovery(self,conn,state,timestamp,day,prices,probs,books,old_tier)
    tiers=tuple((t,b,next((c for ot,_,c in reversed(ORIGINAL) if t+1e-7>=ot),.15))
                for t,b in zip(cfg['thresholds'],cfg['budgets']))
    with patch.object(ladder,'TIERS',tiers),patch.object(CashPolicy,'recovery',recovery):
        yield
