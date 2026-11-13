"""Deterministic synthetic frames, unrelated to any recorded event or account."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
from contextlib import redirect_stdout
import io
from unittest.mock import patch

def frames(day, scenario=0):
    start = datetime.fromisoformat(day+'T12:00:00+08:00')
    names = ['30C', '31C', '32C']
    result = []
    for minute in range(360):
        # Deliberately artificial convergence and one flip; neither predicts weather.
        if minute < 15:
            probabilities = [.36, .40, .24]
        elif minute < 75:
            probabilities = [.13, .82, .05]
        elif minute < 135:
            probabilities = [.10, .87, .03]
        elif minute < 210:
            probabilities = [.06, .92, .02]
        elif scenario:
            probabilities = [.03, .12, .85]
        else:
            probabilities = [.001, .998, .001]
        books, markets = {}, {}
        prices = dict(zip(names, probabilities))
        for name, p in prices.items():
            ask = min(.999, round(p+.001, 3))
            bid = max(.001, round(p-.001, 3))
            if minute >= 210 and not scenario and name == '31C':
                bid, ask = .998, .999
            books[name] = {'asset_id':'SYNTHETIC_'+name, 'tick_size':'.001', 'min_order_size':'5',
                           'asks':[{'price':str(ask),'size':'1000'}],
                           'bids':[{'price':str(bid),'size':'1000'}]}
            markets[name] = {'version':'v1','active':True,'closed':False,'acceptingOrders':True,
                             'feesEnabled':True,'feeSchedule':{'rate':.05,'exponent':1}}
        result.append({'received_at':(start+timedelta(minutes=minute)).isoformat(),
                       'raw_prices_json':json.dumps(prices), 'books_json':json.dumps(books),
                       'markets_json':json.dumps(markets),'errors_json':'{}'})
    return result

def create_inputs(directory):
    directory = Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    sources = []
    for i, day in enumerate(['2030-01-01','2030-01-02']):
        p = directory / (day+'.json')
        p.write_text(json.dumps(frames(day,i)),encoding='utf-8')
        sources.append({'day':day,'path':str(p),'kind':'PUBLIC_GAMMA_BOOK',
                        'synthetic':True,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    return {'synthetic':True,'sources':sources,'labels':{s['day']:{'status':'unknown','winner':None} for s in sources}}

def run(bundle, selected=('baseline','S2','H2','A1'), capital=200.):
    from scripts import v4_continuous as v4, v4_round7 as r7, v4_hypotheses as hypotheses
    from scripts import v3_tier_search as tier
    from copy import deepcopy
    manifest = {'sources':bundle['sources'],'candidates':tier.configs()}
    configs={'baseline':v4.CONFIGS['C1_original'],
             'S2':v4.CONFIGS['S2_capinv_delayconfirm'],
             'H2':hypotheses.HCONFIGS['H2_late14']}
    days=sorted(s['day'] for s in bundle['sources'])
    # No automatic winner inference or assumed-label override.
    labels=bundle.get('labels',{})
    winners={day:x['winner'] for day,x in labels.items()
             if x.get('status')=='official' and x.get('winner') is not None and x.get('evidence')}
    out=[]
    with patch.object(v4,'manifest',return_value=manifest), \
         patch.object(v4,'WINNER',winners),patch.object(v4,'ASSUMED',set()), \
         patch.object(r7,'tier_sources',return_value=bundle['sources']), \
         patch.object(v4,'ladder_tuple',side_effect=lambda ident: tuple(
             (t,b,next((c for ot,_,c in reversed(tier.ORIGINAL) if t+1e-7>=ot),.15))
             for cfg in manifest['candidates'] if cfg['id']==ident
             for t,b in zip(cfg['thresholds'],cfg['budgets']))),redirect_stdout(io.StringIO()):
        for name in selected:
            if name=='A1':
                r=r7.run_config_r7(name,deepcopy(r7.CONFIGS['A1_no_lowvalue_addons']),capital,days)
            else:
                r=v4.run_config(name,deepcopy(configs[name]),capital,days,store=False)
            r['synthetic']=bool(bundle.get('synthetic'))
            r['interpretation']='window-end valuation where winner unknown; not proven settled profit'
            out.append(r)
    return out
