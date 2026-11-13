import importlib
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from research.demo import create_inputs, run, frames
from research.run import load_bundle
from scripts import v4_continuous as v4, v4_round7 as r7
from src.polymarket_client import outcome_name_from_label

class OfflineSafety(unittest.TestCase):
    def test_network_denied(self):
        with self.assertRaisesRegex(RuntimeError,'OFFLINE_NETWORK_DENIED'):
            socket.getaddrinfo('example.invalid',443)

    def test_credentials_denied(self):
        with self.assertRaisesRegex(RuntimeError,'CREDENTIAL_FILE_READ_DENIED'):
            Path('.env').read_text()

    def test_source_write_denied(self):
        with self.assertRaisesRegex(RuntimeError,'OFFLINE_WRITE_OUTSIDE'):
            Path('DO_NOT_CREATE.txt').write_text('forbidden')

    def test_private_database_write_denied(self):
        with self.assertRaisesRegex(RuntimeError,'PRIVATE_DATABASE_WRITE_DENIED'):
            sqlite3.connect('DO_NOT_CREATE.sqlite')

    def test_all_modules_import_without_writes(self):
        root=Path(__file__).resolve().parents[1]
        before={str(p.relative_to(root)):p.stat().st_size for p in root.rglob('*') if p.is_file()}
        for pkg in ['src','live_strategy','live_test','paper_strategy','scripts','research']:
            for p in (root/pkg).glob('*.py'):
                importlib.import_module(pkg+'.'+p.stem)
        after={str(p.relative_to(root)):p.stat().st_size for p in root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

class ActualMechanisms(unittest.TestCase):
    state={'main':'31C','protection':'30C'}
    prices={'30C':.1,'31C':.87,'32C':.03}
    books={'31C':{'asks':[{'price':'.9','size':'1'}],'bids':[{'price':'.1','size':'1'}]}}

    def call_gate(self,gate,timestamp,books=None,state=None):
        return gate(None,None,state or self.state,timestamp,'2030-01-01',self.prices,self.prices,
                    books or self.books,20.,0)

    def test_h2_before_cutoff_blocks(self):
        gate=v4.make_gate('both_spread_late',{'after_hour':14,'max_spread':.03,'min_depth':5})
        with patch.object(v4,'_first_entry',return_value=True),patch.object(v4,'_no_main_only') as both,patch.object(v4._ladder,'_event'):
            self.call_gate(gate,'2030-01-01T13:59:00+08:00')
            both.assert_not_called()

    def test_h2_after_cutoff_does_not_check_explicit_quality(self):
        gate=v4.make_gate('both_spread_late',{'after_hour':14,'max_spread':.03,'min_depth':5})
        with patch.object(v4,'_first_entry',return_value=True),patch.object(v4,'_no_main_only') as both:
            self.call_gate(gate,'2030-01-01T14:00:00+08:00')
            both.assert_called_once()

    def test_s2_distinct_ticks_and_quality_reset(self):
        gate=v4.make_gate('both_spread_confirm',{'confirm_ticks':2,'max_spread':.03,'min_depth':5})
        good={'31C':{'asks':[{'price':'.87','size':'10'}],'bids':[{'price':'.86','size':'10'}]}}
        with patch.object(v4,'_first_entry',return_value=True),patch.object(v4,'_no_main_only') as both,patch.object(v4._ladder,'_event'):
            self.call_gate(gate,'2030-01-01T14:00:00+08:00',good)
            self.call_gate(gate,'2030-01-01T14:00:00+08:00',good)
            both.assert_not_called()
            self.call_gate(gate,'2030-01-01T14:01:00+08:00')
            self.call_gate(gate,'2030-01-01T14:02:00+08:00',good)
            both.assert_not_called()
            self.call_gate(gate,'2030-01-01T14:03:00+08:00',good)
            both.assert_called_once()

    def test_a1_filter_covers_protection_and_repair(self):
        result={'cash_change':0.,'buy_cost_including_fees':0.,'residual_shares':{},
                'window_end_liquidation_estimate_pnl':0.,'fees':0.,'buy_orders':0,'sell_orders':0}
        def replay(*args,**kwargs):
            for reason in ['MAIN','PROTECTION','RECOVERY','MINIMAX']:
                value=v4._ladder._buy(None,'t','2030-01-01','31C',1.5,{}, {}, {},reason)
                self.assertEqual(value,0.)
            return dict(result)
        with patch.object(r7,'tier_sources',return_value=[{'day':'2030-01-01'}]), \
             patch.object(v4,'replay_one',side_effect=replay),patch.object(v4._ladder,'_event'), \
             patch.object(v4._ladder,'_buy') as buy:
            rec=r7.run_config_r7('A1',r7.CONFIGS['A1_no_lowvalue_addons'],200.,['2030-01-01'])
            buy.assert_not_called()
            self.assertEqual(rec['mech_flags']['addon_skipped'],4)

    def test_realized_budget_adapter_differs_from_reservation(self):
        from scripts.v3_true_fee import true_executable_quote
        from scripts.v3_gated_exec import gated_executable_quote
        book={'asks':[{'price':'.1','size':'10'},{'price':'.2','size':'100'}],
              'bids':[],'tick_size':'.001','min_order_size':'5','_paper_fee':{'rate':.05,'exponent':1}}
        true=true_executable_quote(book,cost=2.)
        gated=gated_executable_quote(book,cost=2.)
        self.assertGreater(true['shares'],gated['shares'])
        self.assertLessEqual(gated['reserved_cost'],2.+1e-9)

    def test_temperature_parser_and_v3_input_dependencies(self):
        from scripts.v3_backtest import inputs_for_frame
        self.assertEqual(outcome_name_from_label('31°C'),'31C')
        item={'known':True,'crossed':False,'price_proxy':'.7','bids':[{'price':'.69','size':'10'}],
              'asks':[{'price':'.71','size':'10'}],'tick_size':'.001'}
        prices,*_=inputs_for_frame({'SYNTHETIC':item},{'markets':[{'token':'SYNTHETIC','label':'31°C'}]},.05,outcome_name_from_label)
        self.assertEqual(prices,{'31C':.7})

    def test_unknown_settlement_remains_proxy(self):
        delta,basis=v4.settle_event({'cash_change':-10,'residual_shares':{'31C':20},
                                    'window_end_liquidation_estimate_pnl':-3},None,False)
        self.assertEqual((delta,basis),(-3,'window_end_proxy'))

class ReplayIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.bundle=create_inputs(Path(cls.temp.name))
        cls.results=run(cls.bundle)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_all_four_real_engines_and_unknown_labels(self):
        self.assertEqual([r['config'] for r in self.results],['baseline','S2','H2','A1'])
        for result in self.results:
            self.assertEqual(result['events'],2)
            self.assertTrue(all(x['winner'] is None and x['settlement_basis']=='window_end_proxy' for x in result['ledger']))
            self.assertGreater(sum(x['buy_orders'] for x in result['ledger']),0)

    def test_cash_ledger_continuity(self):
        for r in self.results:
            self.assertAlmostEqual(r['ledger'][0]['cash_after'],r['ledger'][1]['cash_before'],places=3)
            self.assertEqual(r['cash_binding_events'],0)

    def test_input_hash_required(self):
        bundle=dict(self.bundle)
        for s in bundle['sources']:s['path']=Path(s['path']).name
        p=Path(self.temp.name)/'manifest.json'
        p.write_text(json.dumps(bundle))
        self.assertEqual(len(load_bundle(p)['sources']),2)
        bundle['sources'][0]['sha256']='invalid'
        p.write_text(json.dumps(bundle))
        with self.assertRaisesRegex(ValueError,'INPUT_HASH_MISMATCH'):load_bundle(p)

class MinimaxComparison(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
        self.c.executescript('''CREATE TABLE positions(target_date TEXT,outcome TEXT,shares REAL,total_cost REAL);
            CREATE TABLE paper_cash_movements(target_date TEXT,kind TEXT,amount REAL);
            INSERT INTO positions VALUES('2030-01-01','30C',10,3),('2030-01-01','31C',10,5);
            INSERT INTO paper_cash_movements VALUES('2030-01-01','SELL',3);''')
        self.books={n:{'bids':[],'asks':[],'tick_size':'.001','min_order_size':'5',
                       '_paper_fee':{'rate':0.,'exponent':1}} for n in ['30C','31C','32C']}
        self.details={'old_protection':'30C','loss_limit':4.,'remaining_cash':20.,
                      'proposed_allocation':{n:{'cost':0.,'shares':0.,'vwap':None} for n in self.books},
                      'projected_after':{'30C':2.,'31C':2.,'32C':-8.},'projected_z':-8.}
    def tearDown(self):self.c.close()
    def compare(self,books=None):
        from live_strategy.minimax_exit import compare
        return compare(self.c,'2030-01-01','31C','32C',{'30C':.2,'31C':.6,'32C':.19,'33C':.01},
                       self.books if books is None else books,self.details)
    def test_tie_selects_sell_and_keeps_unsold_cost(self):
        r=self.compare()
        self.assertEqual(r['selected'],'SELL')
        self.assertEqual(r['total_buy_cost'],8.)
        self.assertEqual(r['prior_net_proceeds'],3.)
        self.assertEqual(r['direct_sell']['residuals']['30C'],10.)
        self.assertEqual(r['direct_sell']['worst_pnl'],-5.)
    def test_missing_sale_valuation_waits(self):
        self.assertEqual(self.compare({})['selected'],'WAIT')
    def test_three_scenario_minimax_is_not_global_guarantee(self):
        self.details['proposed_allocation']['32C']={'cost':1.,'shares':10.,'vwap':.1}
        self.details['projected_after']={'30C':1.,'31C':1.,'32C':1.}
        self.details['projected_z']=1.
        r=self.compare()
        self.assertEqual(r['selected'],'MINIMAX')
        self.assertEqual(r['minimax']['worst_pnl'],4.)
        self.assertEqual(r['minimax']['all_outcome_worst_pnl'],-6.)

if __name__=='__main__':unittest.main()
