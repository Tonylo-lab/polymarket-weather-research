"""Single supported offline command; no account, network or service mode."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
from research.demo import create_inputs, run

def load_bundle(path):
    path=Path(path).resolve()
    bundle=json.loads(path.read_text(encoding='utf-8'))
    sources=bundle['sources']
    if not sources or len({s['day'] for s in sources})!=len(sources):
        raise ValueError('EMPTY_OR_DUPLICATE_EVENT_DAYS')
    for s in sources:
        p=(path.parent/s['path']).resolve()
        if not p.is_relative_to(path.parent):raise ValueError('INPUT_MUST_BE_INSIDE_BUNDLE')
        if hashlib.sha256(p.read_bytes()).hexdigest()!=s['sha256']:raise ValueError('INPUT_HASH_MISMATCH')
        if s['kind'] not in ('V3','PUBLIC_GAMMA_BOOK'):raise ValueError('UNSUPPORTED_FORMAT')
        s['path']=str(p)
    for label in bundle.get('labels',{}).values():
        if label.get('status') not in ('unknown','official'):raise ValueError('ASSUMED_LABELS_REQUIRE_SEPARATE_NEW_EXPERIMENT')
        if label.get('status')=='official' and (not label.get('winner') or not label.get('evidence')):
            raise ValueError('OFFICIAL_LABEL_REQUIRES_EVIDENCE')
    return bundle

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',help='Local, licensed frame bundle manifest; omitted = synthetic demo')
    p.add_argument('--strategy',nargs='+',choices=['baseline','S2','H2','A1'],default=['baseline','S2','H2','A1'])
    p.add_argument('--capital',type=float,default=200.)
    args=p.parse_args()
    if not 0<args.capital<=1e6:raise ValueError('INVALID_RESEARCH_CAPITAL')
    with tempfile.TemporaryDirectory(prefix='weather_research_') as temp:
        bundle=load_bundle(args.input) if args.input else create_inputs(Path(temp))
        results=run(bundle,args.strategy,args.capital)
    summary=[{k:r[k] for k in ('config','capital','events','net_profit','synthetic','interpretation')} for r in results]
    print(json.dumps(summary,indent=2))

if __name__=='__main__':main()
