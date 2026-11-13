"""H1-H3 hypothesis runner (round 5). Independent OUT dir; does NOT touch
S1/S2/S3 runs. Reuses v4_continuous's frozen engine (replay_one/run_config) and
the H1/H2/H3 gate branches added to make_gate. Rules pre-frozen in
data/diagnostics/v4_hypotheses_20261031/PREREGISTRATION.md.

All configs: fixed_bankroll=200 (capital-invariant), p80_85_90_cap30 ladder,
same execution/fee/cash/settlement basis as S2.
"""

import io, json, time

from pathlib import Path

from scripts import v4_continuous as v4

OUT = v4.ROOT / 'research_outputs'

# Output directories are created only by an explicit run.

BASE = {'ladder': 'p80_85_90_cap30', 'fixed_bankroll': 200.0,
        'max_spread': 0.03, 'min_depth': 5}

HCONFIGS = {
    # H1: longer confirm window (S2 extension)
    'H1a_confirm3': {**BASE, 'gate': 'both_spread_confirm', 'confirm_ticks': 3},
    'H1b_confirm5': {**BASE, 'gate': 'both_spread_confirm', 'confirm_ticks': 5},
    # H2: late-entry time-of-day gate (HK local clock >= 14:00)
    'H2_late14':    {**BASE, 'gate': 'both_spread_late', 'after_hour': 14},
    # H3: Main prob trend/variance gate (last 10 distinct frames slope>=0, std<=0.03)
    'H3_trend':     {**BASE, 'gate': 'both_spread_trend', 'trend_k': 10, 'max_std': 0.03},
    # --- H2 sensitivity (round 6, pre-frozen; stability check, NOT cherry-pick) ---
    # 4 time variants around 14:00 (minute granularity), all else identical to H2:
    'HS_t1300':     {**BASE, 'gate': 'both_spread_late', 'after_minute': 13 * 60},
    'HS_t1330':     {**BASE, 'gate': 'both_spread_late', 'after_minute': 13 * 60 + 30},
    'HS_t1430':     {**BASE, 'gate': 'both_spread_late', 'after_minute': 14 * 60 + 30},
    'HS_t1500':     {**BASE, 'gate': 'both_spread_late', 'after_minute': 15 * 60},
    # 2 execution-robustness variants on the 14:00 rule:
    'HS_adverse1tick': {**BASE, 'gate': 'both_spread_late', 'after_minute': 14 * 60, 'adverse_ticks': 1},
    'HS_delay1min':    {**BASE, 'gate': 'both_spread_late', 'after_minute': 14 * 60, 'exec_delay_min': 1},
}

PARAM_KEYS = ('min_price', 'lead', 'max_spread', 'min_depth', 'confirm_ticks',
              'after_hour', 'after_minute', 'trend_k', 'max_std',
              'adverse_ticks', 'exec_delay_min')

def run_one(cfg_id, cfg, cap, days):
    # Register so v4.run_config can resolve the config id, then run it.
    v4.CONFIGS[cfg_id] = cfg
    return v4.run_config(cfg_id, cfg, cap, days, store=True)
