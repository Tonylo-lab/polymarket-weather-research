"""Asymmetric paper decisions using the existing cash ledger and event lifecycle."""

from contextlib import closing

from copy import deepcopy

import json

from src import paired_ladder as ladder, paired_cash as cash

from src.market_only import save_exposure

from . import solver

def schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS asymmetric_decisions(
        id INTEGER PRIMARY KEY, target_date TEXT NOT NULL, timestamp TEXT NOT NULL,
        kind TEXT NOT NULL, reason TEXT NOT NULL, details_json TEXT NOT NULL)''')

def scenarios(held, cost, recovered, books, outcomes, exit_price):
    return {n:recovered+solver.exit_net(held.get(n,0.),books.get(n),exit_price)-cost for n in outcomes}

def record(conn, day, timestamp, kind, reason, details):
    conn.execute('INSERT INTO asymmetric_decisions(target_date,timestamp,kind,reason,details_json) VALUES(?,?,?,?,?)',
                 (day,timestamp,kind,reason,json.dumps(details,sort_keys=True,allow_nan=False)))
    ladder._event(conn,timestamp,day,'ASYMMETRIC_CHECK',dict(details,kind=kind,reason=reason))

def recovery_budget(config,bankroll,state,cost,cash_available,tier):
    total_cap=bankroll*(ladder.TIERS[tier-1][2] if tier else .15)
    return min(cash_available,max(0.,total_cap-cost),
               max(0.,config['recovery_additional_fraction']*bankroll-state['recovery_used']))

def cycle(directory, day, timestamp, prices, probabilities, books, *, config,
          handover_enabled=None, handover_policy=None, paper_policy=None):
    """Case labels describe holdings; they never latch recovery after a return."""
    with closing(ladder.connect(ladder.path(directory))) as conn, conn:
        schema(conn)
        if conn.execute('SELECT 1 FROM settlements WHERE target_date=?',(day,)).fetchone():
            return
        conn.execute('INSERT OR IGNORE INTO paired_state(target_date,bankroll_before) VALUES(?,?)',
                     (day,cash.balance(conn)))
        state=dict(conn.execute('SELECT * FROM paired_state WHERE target_date=?',(day,)).fetchone())
        held,cost=ladder._portfolio(conn,day)
        if cost <= solver.EPS:
            state['bankroll_before']=cash.balance(conn)
            conn.execute('UPDATE paired_state SET bankroll_before=? WHERE target_date=?',(state['bankroll_before'],day))
        bankroll=state['bankroll_before']
        recovered=conn.execute("SELECT COALESCE(SUM(amount),0) FROM paper_cash_movements WHERE target_date=? AND kind IN ('SELL','REDEEM')",(day,)).fetchone()[0]
        main=max(probabilities,key=probabilities.get)
        proposed=ladder._protection(probabilities,main)
        old_main,old_p=state['main'],state['protection']
        protection=proposed
        tier=ladder._tier(probabilities[main])
        highest=state['highest_tier']
        main_flip=bool(old_main and main != old_main)
        switch=bool(not main_flip and old_p and proposed != old_p and held.get(old_p,0.) > solver.EPS)
        main_floor=config['main_profit_fraction']*bankroll
        available=min(cash.balance(conn),max(0.,bankroll*(ladder.TIERS[tier-1][1] if tier else 0.)-cost))
        common=dict(protection_loss=config['protection_loss_fraction'],legacy_loss=config['legacy_loss_fraction'],exit_price=config['exit_price'])
        switch_plan=None
        if switch:
            permitted=(tier>0 and probabilities[main]<.90 and probabilities[proposed]>=.10
                       and probabilities[proposed]-probabilities.get(old_p,0.)>=.03-solver.EPS)
            try:
                before=solver.exit_net(held.get(main,0.),books[main],config['exit_price'])+recovered-cost
                floor=max(main_floor,config['switch_profit_keep']*max(0.,before))
                if permitted:
                    repairing=cost>solver.EPS and before < -solver.EPS
                    preview_budget=recovery_budget(config,bankroll,state,cost,cash.balance(conn),tier) if repairing else available
                    switch_plan=solver.solve(books,main,proposed,held,cost,recovered,preview_budget,bankroll,
                                              main_floor=0. if repairing else floor,
                                              recovery=repairing,allow_main_reduction=not repairing,**common)
            except (KeyError,ValueError,TypeError):
                switch_plan=None
            if switch_plan is None:
                protection=old_p
                ladder._event(conn,timestamp,day,'ASYMMETRIC_SWITCH_WAIT',{'old':old_p,'proposed':proposed,'reason':'keep_old_until_joint_scenarios_executable'})
                switch=False
        case=0
        if main_flip and cost>solver.EPS:
            case=2 if held.get(main,0.)<=solver.EPS else 1 if held.get(protection,0.)>solver.EPS else 3
        mode=ladder.MODES[case]
        mh,ph=json.loads(state['main_history']),json.loads(state['protection_history'])
        if not mh or mh[-1]!=main:mh.append(main)
        if not ph or ph[-1]!=protection:ph.append(protection)
        conn.execute('''UPDATE paired_state SET main=?,protection=?,mode=?,main_history=?,protection_history=?,
            main_flips=main_flips+?,protection_switches=protection_switches+?,
            case1_count=case1_count+?,case2_count=case2_count+?,case3_count=case3_count+?,
            tier_probability_seen=MAX(tier_probability_seen,?),handover_completed=0,handover_pending='{}' WHERE target_date=?''',
            (main,protection,mode,json.dumps(mh),json.dumps(ph),int(main_flip),int(switch),int(case==1),int(case==2),int(case==3),tier,day))
        if main_flip:
            ladder._event(conn,timestamp,day,'MAIN_FLIP',{'from':old_main,'to':main,'case':case,'latched_recovery':False})
        if switch:
            ladder._event(conn,timestamp,day,'PROTECTION_SWITCH',{'from':old_p,'to':protection,'policy':config['policy']})
        kind,reason,plan='WAIT','no_active_tier',None
        before_map={}
        try:
            before_map=scenarios(held,cost,recovered,books,probabilities,config['exit_price'])
            coverage=probabilities[main]+probabilities[protection]
            recovering=cost>solver.EPS and before_map[main]<-solver.EPS
            if coverage+solver.EPS < .80:
                reason='pair_coverage_below_80'
            elif recovering:
                kind='RECOVERY'
                available=recovery_budget(config,bankroll,state,cost,cash.balance(conn),tier)
                plan=switch_plan or solver.solve(books,main,protection,held,cost,recovered,available,bankroll,main_floor=0.,recovery=True,**common)
                reason='partial_repair' if plan and plan['partial'] else 'full_repair' if plan else 'no_feasible_improving_repair'
            elif switch_plan is not None:
                kind,reason,plan='SWITCH','asymmetric_switch',switch_plan
            elif tier and tier>highest:
                kind='ENTRY' if cost<=solver.EPS else 'ADD'
                plan=solver.solve(books,main,protection,held,cost,recovered,available,bankroll,main_floor=main_floor,**common)
                reason='joint_scenarios_pass' if plan else 'no_joint_plan_within_limits'
            elif tier:
                reason='tier_already_completed'
        except (KeyError,ValueError,TypeError,OverflowError):
            reason='missing_or_invalid_fee_depth_identity'
        details={'policy':config['policy'],'main':main,'protection':protection,'case':case,'tier':tier,
                 'opening_bankroll':bankroll,'cash':cash.balance(conn),'budget':available,
                 'before':before_map,'plan':plan,'exit_price':config['exit_price'],
                 'main_profit_floor':main_floor,'protection_loss_floor':-config['protection_loss_fraction']*bankroll}
        if plan:
            working=deepcopy(books)
            conn.execute('SAVEPOINT asymmetric_pair')
            try:
                for name,leg in ((protection,plan['protection']),(main,plan['main'])):
                    if leg['shares']<=solver.EPS:continue
                    tranche='ASYMMETRIC_'+kind+('_PROTECTION' if name==protection else '_MAIN')
                    paid=ladder._buy(conn,timestamp,day,name,leg['cost']+1e-8,working,prices,probabilities,tranche,shares=leg['shares'])
                    if abs(paid-leg['cost'])>solver.EPS:
                        raise ValueError('planned_leg_not_fully_executed')
                after_held,after_cost=ladder._portfolio(conn,day)
                after_map=scenarios(after_held,after_cost,recovered,books,probabilities,config['exit_price'])
                if (abs(after_map[main]-plan['main_pnl'])>solver.EPS or
                        abs(after_map[protection]-plan['protection_pnl'])>solver.EPS):
                    raise ValueError('executed_scenarios_differ')
                conn.execute('RELEASE SAVEPOINT asymmetric_pair')
                if kind=='RECOVERY':
                    conn.execute('UPDATE paired_state SET recovery_used=recovery_used+?,break_even_feasible=?,recovery_info=? WHERE target_date=?',
                                 (plan['added_cost'],int(not plan['partial']),json.dumps(plan),day))
                elif kind in ('ENTRY','ADD'):
                    conn.execute('UPDATE paired_state SET highest_tier=MAX(highest_tier,?),pending_tier=0 WHERE target_date=?',(tier,day))
                details['after']=after_map
                details['executed']=True
            except Exception:
                conn.execute('ROLLBACK TO SAVEPOINT asymmetric_pair')
                conn.execute('RELEASE SAVEPOINT asymmetric_pair')
                details['executed']=False
                details['after']=before_map
                if switch:
                    details['attempted_protection']=protection
                    details['protection']=old_p
                    conn.execute('UPDATE paired_state SET protection=?,protection_history=?,protection_switches=MAX(0,protection_switches-1) WHERE target_date=?',
                                 (old_p,state['protection_history'],day))
                    ladder._event(conn,timestamp,day,'ASYMMETRIC_SWITCH_ROLLED_BACK',{'from':protection,'retained':old_p})
                reason='pair_preview_execution_mismatch_rolled_back'
        else:
            details.update(after=before_map,executed=False)
            if kind in ('ENTRY','ADD'):
                conn.execute('UPDATE paired_state SET pending_tier=? WHERE target_date=?',(tier,day))
        record(conn,day,timestamp,kind,reason,details)
        ladder._snapshot_map(conn,timestamp,day,'AFTER',mode,probabilities)
        save_exposure(conn,day,timestamp,list(probabilities))
