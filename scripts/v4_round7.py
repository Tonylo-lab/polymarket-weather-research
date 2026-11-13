"""Round-7 candidate runner. Independent OUT dir; does NOT touch H2/HS/S1-3.
Reuses v4_continuous's engine (replay_one + the H2 both_spread_late gate) and adds
single-mechanism switches per PREREGISTRATION_R7.md. Each switch is applied via a
patch and its effect is asserted by an event/skip counter in the ledger.

Mechanisms:
  A1 no_lowvalue_addons : skip non-first-entry _buy legs with budget < min_addon_budget
  A2 protection_off_tail : drop PROTECTION legs + enable S3 early-risk-exit (tail ctrl)
  A3 protection_off_naked: drop PROTECTION legs, no stop (tail-risk lower bound)
  B1 earlier_if_stable   : time gate 13:00 + require Main-prob std<=0.02 over last k=10
  C1 account_scaled_budget: opening_bankroll = current cash-on-hand (NOT fixed 200)
  C2 fixed_risk_per_event : first-entry total invest capped at risk_cap U (fixed size)
"""

from __future__ import annotations

import io, json, time, math

from contextlib import redirect_stdout, ExitStack

from pathlib import Path

from unittest.mock import patch

from scripts import v4_continuous as v4

from scripts import v3_tier_search as tier

OUT = v4.ROOT / 'research_outputs'

# Output directories are created only by an explicit run.

BASE = {'gate': 'both_spread_late', 'after_minute': 840,  # H2 14:00
        'ladder': 'p80_85_90_cap30', 'fixed_bankroll': 200.0,
        'max_spread': 0.03, 'min_depth': 5}

CONFIGS = {
    'H2_baseline':               {**BASE},
    'A1_no_lowvalue_addons':     {**BASE, 'min_addon_budget': 2.0},
    'A2_protection_off_tail':    {**BASE, 'drop_protection': True, 'early_trim': True},
    'A3_protection_off_naked':   {**BASE, 'drop_protection': True},
    'B1_earlier_if_stable':      {**BASE, 'after_minute': 780, 'stable_k': 10, 'stable_std': 0.02},
    'C1_account_scaled_budget':  {**BASE, 'fixed_bankroll': None},  # opening=cash-on-hand
    'C2_fixed_risk_per_event':   {**BASE, 'risk_cap': 4.0},
}

def run_config_r7(cfg_id, cfg, cap, days):
    """Continuous single account, like v4.run_config, but threading our extra
    switches into each replay_one via patches. Mirrors v4.run_config settlement."""
    from scripts import v3_minimax_cases as engine
    WIN = v4.WINNER
    sources = {s['day']: s for s in tier_sources()}
    cash_on_hand = float(cap)
    ledger = []
    cash_binding = 0
    _ladder = v4._ladder
    planner = v4.base.load_engine()[0]
    cashmod = planner.cash

    gate = cfg['gate']
    params = {k: cfg[k] for k in ('min_price', 'lead', 'max_spread', 'min_depth',
              'confirm_ticks', 'after_hour', 'after_minute', 'trend_k', 'max_std',
              'stable_k', 'stable_std') if k in cfg}
    min_addon = cfg.get('min_addon_budget')
    drop_prot = cfg.get('drop_protection', False)
    risk_cap = cfg.get('risk_cap')
    early_trim = cfg.get('early_trim', False)
    fixed_bankroll = cfg.get('fixed_bankroll', 200.0)

    # marker for A1: set True only during first-entry allocate so add-ons are
    # distinguishable (add-on = a _buy fired when NOT in first entry).
    flags = {'in_first_entry': False, 'addon_skipped': 0, 'prot_skipped': 0,
             'risk_capped': 0, 'stable_wait': 0}

    orig_buy = _ladder._buy

    def buy_hook(conn, timestamp, day, name, budget, books, prices, probs, reason, *, shares=None):
        if drop_prot and reason == 'PROTECTION':
            flags['prot_skipped'] += 1
            _ladder._event(conn, timestamp, day, 'MECH_PROTECTION_SKIPPED', {'outcome': name})
            return 0.0
        if min_addon is not None and not flags['in_first_entry'] and budget + v4._EPS < min_addon:
            flags['addon_skipped'] += 1
            _ladder._event(conn, timestamp, day, 'MECH_ADDON_SKIPPED',
                           {'outcome': name, 'budget': budget, 'min_addon': min_addon})
            return 0.0
        return orig_buy(conn, timestamp, day, name, budget, books, prices, probs, reason, shares=shares)

    for day in days:
        src = sources[day]
        with ExitStack() as st:
            st.enter_context(patch.object(_ladder, '_buy', buy_hook))
            # B1 stability gate + A1 first-entry marker are layered by wrapping the gate.
            base_gate = v4.make_gate(gate, params)
            st.enter_context(patch.object(v4.policy_mod.CashPolicy, '_allocate_original',
                                          _wrap_gate(base_gate, cfg, flags, _ladder)))
            with redirect_stdout(io.StringIO()):
                r = v4.replay_one(src, min(cash_on_hand, 1e9) if fixed_bankroll is None else cash_on_hand,
                                  'p80_85_90_cap30', None, params,  # gate=None: our wrap handles it
                                  fixed_bankroll=(cash_on_hand if fixed_bankroll is None else fixed_bankroll),
                                  early_trim=early_trim)
        winner = WIN.get(day)
        assumed = day in v4.ASSUMED
        r['_assumed'] = assumed
        sd, basis = v4.settle_event(r, winner, assumed_ok=True)
        peak_buy = r['buy_cost_including_fees']
        cb = peak_buy > cash_on_hand + 1e-6
        if cb:
            cash_binding += 1
        rec = {'day': day, 'winner': winner, 'winner_assumed': assumed, 'settlement_basis': basis,
               'funded_with': round(cash_on_hand, 6), 'peak_buy_cost_incl_fees': round(peak_buy, 4),
               'cash_binding': cb, 'realized_cash_change': round(r['cash_change'], 4),
               'residual_shares': r['residual_shares'],
               'window_end_liquidation_pnl': round(r['window_end_liquidation_estimate_pnl'], 4),
               'settled_delta': round(sd, 6),
               'cash_before': round(cash_on_hand, 6), 'cash_after': round(cash_on_hand + sd, 6),
               'buy_orders': r.get('buy_orders'), 'sell_orders': r.get('sell_orders'),
               'fees': round(r.get('fees', 0), 4), 'exit_trigger': v4.classify(r)}
        ledger.append(rec)
        cash_on_hand = round(cash_on_hand + sd, 6)
    net = round(cash_on_hand - cap, 4)
    return {'config': cfg_id, 'capital': cap, 'start_capital': cap, 'final_cash': round(cash_on_hand, 6),
            'net_profit': net, 'return_pct': round(100 * net / cap, 4), 'events': len(ledger),
            'cash_binding_events': cash_binding, 'mech_flags': dict(flags), 'ledger': ledger}

def _wrap_gate(base_gate, cfg, flags, _ladder):
    """Wrap the H2 first-entry gate to (a) set in_first_entry marker around the
    allocate so A1 can tell add-ons apart, (b) layer B1 stability pre-filter,
    (c) apply C2 risk_cap by shrinking `target` for the first entry."""
    stable_k = cfg.get('stable_k'); stable_std = cfg.get('stable_std')
    risk_cap = cfg.get('risk_cap')
    hist_state = {}

    def wrapped(self, conn, state, timestamp, day, prices, probs, books, target, case):
        first = v4._first_entry(conn, state, day, case)
        if not first:
            return base_gate(self, conn, state, timestamp, day, prices, probs, books, target, case)
        # B1 stability pre-filter (decision-time only)
        if stable_k is not None:
            main = state['main']
            pm, hist, lts = hist_state.get(day, (None, [], None))
            if main != pm:
                hist = []
            cur = probs.get(main)
            if cur is not None and timestamp != lts:
                hist = (hist + [float(cur)])[-stable_k:]
            hist_state[day] = (main, hist, timestamp)
            if len(hist) < stable_k:
                flags['stable_wait'] += 1
                _ladder._event(conn, timestamp, day, 'ENTRY_STABLE_WAIT',
                               {'main': main, 'n': len(hist), 'need': stable_k})
                return
            mean = sum(hist) / len(hist)
            std = (sum((x - mean) ** 2 for x in hist) / len(hist)) ** 0.5
            if std > stable_std + v4._EPS:
                flags['stable_wait'] += 1
                _ladder._event(conn, timestamp, day, 'ENTRY_STABLE_WAIT',
                               {'main': main, 'std': std, 'max_std': stable_std})
                return
        # C2 risk cap: shrink the first-entry budget to risk_cap (fixed size)
        tgt = target
        if risk_cap is not None:
            tgt = min(target, risk_cap)
            if tgt < target:
                flags['risk_capped'] += 1
        flags['in_first_entry'] = True
        try:
            return base_gate(self, conn, state, timestamp, day, prices, probs, books, tgt, case)
        finally:
            flags['in_first_entry'] = False
    return wrapped

def tier_sources():
    m = json.loads((tier.OUT / 'inputs.json').read_text(encoding='utf-8'))
    return m['sources']
