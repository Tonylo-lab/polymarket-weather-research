"""Continuous-account replay (research only).

Unlike v3_true_fee_replay (which resets 200U/500U PER EVENT and the summaries
sum those resets), this threads ONE account across the 68 chronological Hong
Kong daily events in date order:

  * cash_on_hand starts at the capital (200 or 500);
  * each event is funded ONLY by the cash available when it opens;
  * the event's full strategy path runs with INITIAL_CASH = cash_on_hand, so
    sizing reflects the real budget (a low balance really does shrink/skip);
  * at the event's settlement the capital is released: cash_on_hand updates by
    the event's SETTLED delta (realized cash_change + residual shares settled at
    the official/assumed winner payout, or window-end liquidation when unknown);
  * HK events are daily and disjoint, so event N fully settles before N+1 opens
    (no overlapping capital lock) -- we assert the peak buy cost <= cash_on_hand.

Everything else is the corrected true-fee/VWAP economics from v3_true_fee and the
conservation-checked ledger from v3_backtest.collect. No network, no live account.

Settlement accounting (per event):
  settled_delta = cash_change + sum_over_residual(shares * payout(winner))
    - winner known (official): payout = 1.0 if that outcome is the winner else 0.0
    - winner assumed-high-price (user-permitted sensitivity): same, but flagged;
      reported both WITH and WITHOUT assumed days.
    - winner unknown: residuals valued at window-end liquidation estimate
      (already inside cash_change? no -- cash_change is realized only; we add the
      window-end liquidation of residuals as the release proxy and flag it).
"""

from __future__ import annotations

import argparse, json, time

from collections import Counter

from contextlib import ExitStack, closing, redirect_stdout

from copy import deepcopy

from pathlib import Path

import io, sqlite3, tempfile

from unittest.mock import patch

from scripts import v3_backtest as base

from scripts import v3_minimax_cases as engine

from scripts import v3_tier_search as tier

from scripts import v3_true_fee as tf

ROOT = base.ROOT

OUT = ROOT / 'research_outputs'

TIER = tier.OUT

_INTRADAY_REC = None  # when set to a list, replay_one appends per-tick mark-to-market equity

WIN = {} # Explicit offline labels only; imports never read evidence files.

WINNER = {d: w.get('winner') for d, w in WIN.items()}

WINNER.update(tier.ASSUMED)

ASSUMED = set(tier.ASSUMED)

CONFIGS = {
    'C0_no_trade':            {'ladder': None, 'gate': 'no_trade'},
    'C1_original':            {'ladder': None, 'gate': None},
    'C2_cap30':               {'ladder': 'p80_85_90_cap30', 'gate': None},
    'C3_cap30_both_legs':     {'ladder': 'p80_85_90_cap30', 'gate': 'both_legs'},
    'C4_cap30_minprice20':    {'ladder': 'p80_85_90_cap30', 'gate': 'minprice', 'min_price': 0.20},
    'C5_cap30_minprice35':    {'ladder': 'p80_85_90_cap30', 'gate': 'minprice', 'min_price': 0.35},
    'C6_cap30_mainlead10':    {'ladder': 'p80_85_90_cap30', 'gate': 'mainlead', 'lead': 0.10},
    'C7_cap30_spread_depth':  {'ladder': 'p80_85_90_cap30', 'gate': 'spread_depth', 'max_spread': 0.03, 'min_depth': 5.0},
    'C8_cap30_both_minp35':   {'ladder': 'p80_85_90_cap30', 'gate': 'both_minprice', 'min_price': 0.35},
    'C9_cap15':               {'ladder': 'p80_85_90_cap15', 'gate': None},
    # --- Round 2 (evidence-based revision): abstention pushed to its limit +
    # dropping the confirmed negative-edge protection leg. ---
    # R1: cap30 + ALL THREE entry gates composed (both-legs AND spread<=.03/depth>=5
    #     AND main-lead>=.10) = maximum abstention on the zero-edge signal.
    'R1_cap30_all_gates':     {'ladder': 'p80_85_90_cap30', 'gate': 'all_gates',
                               'max_spread': 0.03, 'min_depth': 5.0, 'lead': 0.10},
    # R2: cap30 Main-only (protection leg dropped) + both-legs-equivalent entry
    #     still requires an executable Main; protection buys are suppressed.
    'R2_cap30_mainonly':      {'ladder': 'p80_85_90_cap30', 'gate': None, 'no_protection': True},
    # R3: cap15 Main-only (smallest position + no protection drag).
    'R3_cap15_mainonly':      {'ladder': 'p80_85_90_cap15', 'gate': None, 'no_protection': True},
    # R4: cap30 + the two best round-1 gates combined (both-legs + spread/depth).
    'R4_cap30_both_spread':   {'ladder': 'p80_85_90_cap30', 'gate': 'both_spread',
                               'max_spread': 0.03, 'min_depth': 5.0},
}

ROUND2 = ['R1_cap30_all_gates', 'R2_cap30_mainonly', 'R3_cap15_mainonly', 'R4_cap30_both_spread']

CONFIGS.update({
    'S1_capinv_both_spread':  {'ladder': 'p80_85_90_cap30', 'gate': 'both_spread',
                               'max_spread': 0.03, 'min_depth': 5.0, 'fixed_bankroll': 200.0},
    'S2_capinv_delayconfirm': {'ladder': 'p80_85_90_cap30', 'gate': 'both_spread_confirm',
                               'max_spread': 0.03, 'min_depth': 5.0, 'fixed_bankroll': 200.0,
                               'confirm_ticks': 2},
    'S3_capinv_earlytrim':    {'ladder': 'p80_85_90_cap30', 'gate': 'both_spread',
                               'max_spread': 0.03, 'min_depth': 5.0, 'fixed_bankroll': 200.0,
                               'early_trim': True},
    'S4_capinv_noreentry':    {'ladder': 'p80_85_90_cap30', 'gate': 'both_spread',
                               'max_spread': 0.03, 'min_depth': 5.0, 'fixed_bankroll': 200.0,
                               'no_reentry': True},
})

ROUND3 = ['S1_capinv_both_spread', 'S2_capinv_delayconfirm', 'S3_capinv_earlytrim', 'S4_capinv_noreentry']

EXTRA_LADDERS = {
    'p80_85_90_cap15': {'thresholds': [0.80, 0.85, 0.90], 'budgets': [0.05, 0.10, 0.15]},
}

def manifest():
    return json.loads((TIER / 'inputs.json').read_text(encoding='utf-8'))

def _read_frames(path, attempts=6):
    """Read the 360 frames from a frozen source, retrying transient DatabaseError
    blips (the live WeatherArchive task writes a separate 2GB db concurrently)."""
    import time as _t
    last = None
    for i in range(attempts):
        try:
            with closing(base.readonly(path)) as c:
                return [dict(r) for r in c.execute('SELECT * FROM frames ORDER BY minute_us')]
        except sqlite3.DatabaseError as e:
            last = e
            _t.sleep(0.5 * (i + 1))
    # Diagnostic dump before giving up.
    try:
        from pathlib import Path as _P
        hdr = _P(path).read_bytes()[:16]
        (OUT / 'read_fail_diag.txt').write_text(
            f'path={path}\nuri={_P(path).resolve().as_uri()}\nsize={_P(path).stat().st_size}\n'
            f'header={hdr!r}\nerr={last!r}\n', encoding='utf-8')
    except Exception as _d:
        pass
    raise last

def ladder_tuple(cfg_id):
    """Return the TIERS tuple for a named ladder candidate (from manifest or extra)."""
    m = manifest()
    cand = next((x for x in m['candidates'] if x['id'] == cfg_id), None)
    if cand is None and cfg_id in EXTRA_LADDERS:
        cand = EXTRA_LADDERS[cfg_id]
    thresholds, budgets = cand['thresholds'], cand['budgets']
    return tuple((t, b, next((c for ot, _, c in reversed(tier.ORIGINAL) if t + 1e-7 >= ot), .15))
                 for t, b in zip(thresholds, budgets))

from src import paired_cash_policy as policy_mod

from src import paired_ladder as _ladder

from src.market_only import EPS as _EPS

_ORIG = policy_mod.CashPolicy._allocate_original

def _first_entry(conn, state, day, case):
    _, deployed = _ladder._portfolio(conn, day)
    return case == 0 and deployed <= _EPS

def _best_prices(book, side):
    """Best bid/ask price from a book, or None."""
    rows = book.get('asks' if side == 'ask' else 'bids', [])
    vals = []
    for r in rows:
        try:
            p = float(r['price']); q = float(r['size'])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < p < 1:
            vals.append((p, q))
    if not vals:
        return None, 0.0
    if side == 'ask':
        p, q = min(vals, key=lambda x: x[0])
    else:
        p, q = max(vals, key=lambda x: x[0])
    return p, q

def _ask_depth(book):
    return sum(float(r['size']) for r in book.get('asks', []) if 0 < float(r['price']) < 1)

_ORIG_BUY = _ladder._buy

def _buy_no_protection(conn, timestamp, day, name, budget, books, prices, probs, reason, *, shares=None):
    if reason == 'PROTECTION':
        _ladder._event(conn, timestamp, day, 'MECH_PROTECTION_SKIPPED', {'outcome': name, 'budget': budget})
        return 0.0
    return _ORIG_BUY(conn, timestamp, day, name, budget, books, prices, probs, reason, shares=shares)

def _s3_risk_exit(cash, conn, day, timestamp, executable_books):
    """S3 early risk-exit: for the current Main leg, if the executable liquidation
    value has fallen to <= HALF the entry cost (unrealized loss >= 50% of what was
    paid for that leg -- entry-time worst case is losing the full cost), sell the
    Main leg NOW at the current executable bid (any positive price, NOT the 0.998
    floor). Threshold is fixed at entry economics; reuses the true-fee SELL quote.
    Returns True if it sold (so the day's normal exit logic is bypassed after)."""
    from src import paper_execution as pe
    row = conn.execute("SELECT main FROM paired_state WHERE target_date=?", (day,)).fetchone()
    if not row or not row['main']:
        return False
    main = row['main']
    pos = conn.execute('SELECT shares FROM positions WHERE target_date=? AND outcome=?', (day, main)).fetchone()
    held = pos['shares'] if pos else 0.
    if held <= _EPS:
        return False
    entry_cost = conn.execute(
        "SELECT COALESCE(SUM(cost),0) FROM fills WHERE target_date=? AND outcome=?", (day, main)).fetchone()[0]
    if entry_cost <= _EPS:
        return False
    book = executable_books.get(main)
    if not book or not book.get('_paper_fee'):
        return False
    # executable liquidation at ANY positive bid (true-fee SELL)
    sell = pe.quote(book, shares=held, side='SELL')
    liq = sell['net'] if sell and sell.get('shares', 0) > _EPS else 0.
    # worst-case loss at entry = entry_cost (leg -> 0). Exit when current loss >= half.
    if entry_cost - liq >= 0.5 * entry_cost - _EPS:
        # sell the Main leg now at the executable bid
        from src.market_only import consume_book as _cb  # noqa (depth accounting inside _move path)
        key = f'S3EXIT:{day}:{timestamp}:{main}'
        details = {'shares': sell['shares'], 'vwap': sell['vwap'], 'policy': 'V4_S3_EARLY_RISK_EXIT',
                   'entry_cost': entry_cost, 'liquidation': liq, 'threshold_frac': 0.5,
                   'consumed': sell.get('consumed'), 'fee_schedule': book['_paper_fee'],
                   'minimum_exit_bid': 0.0}
        if cash._move(conn, key, timestamp, day, main, 'SELL', sell['net'], sell['gross'], sell['fee'], details):
            conn.execute('UPDATE positions SET shares=MAX(0,shares-?) WHERE target_date=? AND outcome=?',
                         (sell['shares'], day, main))
            _ladder._event(conn, timestamp, day, 'S3_EARLY_RISK_EXIT',
                           dict(details, outcome=main, net=sell['net'], fee=sell['fee']))
            return True
    return False

def make_gate(gate, params):
    """Build a patched _allocate_original enforcing one first-entry gate."""
    confirm_state = {}   # per (day) -> (main, consecutive_passes); S2 only
    def patched(self, conn, state, timestamp, day, prices, probs, books, target, case):
        if not _first_entry(conn, state, day, case):
            return _ORIG(self, conn, state, timestamp, day, prices, probs, books, target, case)
        main, protection = state['main'], state['protection']
        m_book, p_book = books.get(main, {}), books.get(protection, {})
        blocked, reason, detail = False, None, {}
        if gate == 'no_trade':
            blocked, reason = True, 'no_trade_baseline'
        elif gate == 'both_spread_confirm':
            # S2: delayed-confirm first entry. The both_spread quality (spread<=max,
            # depth>=min, both legs feasible) must PASS on `confirm_ticks` CONSECUTIVE
            # decision ticks with the SAME Main, using only decision-time info. A fail,
            # a missing/unusable book, or a Main identity change RESETS the streak.
            # No future-look: each tick reads only the current frame.
            need = int(params.get('confirm_ticks', 2))
            m_ask, _ = _best_prices(m_book, 'ask')
            m_bid, _ = _best_prices(m_book, 'bid')
            depth = _ask_depth(m_book)
            spread = (m_ask - m_bid) if (m_ask is not None and m_bid is not None) else None
            quality_ok = (spread is not None and spread <= params['max_spread'] + _EPS
                          and depth + _EPS >= params['min_depth'])
            prev_main, streak, last_ts = confirm_state.get(day, (None, 0, None))
            # DEDUP: the engine calls allocate more than once within a single frame
            # (profit_floor -> cash_policy, plus the legacy/minimax wrappers). Count one
            # DECISION TICK per distinct timestamp -- a repeat call at the same timestamp
            # must NOT advance the confirm streak (that would satisfy an N-tick confirm
            # inside one real minute). On a repeat, reuse the streak already computed.
            if timestamp == last_ts and main == prev_main:
                pass  # same frame, same Main: streak unchanged
            elif not quality_ok or main != prev_main:
                streak = 1 if quality_ok else 0
            else:
                streak = streak + 1
            confirm_state[day] = (main, streak, timestamp)
            detail = {'spread': spread, 'ask_depth': depth, 'quality_ok': quality_ok,
                      'streak': streak, 'need': need, 'main': main}
            if streak < need:
                _ladder._event(conn, timestamp, day, 'ENTRY_CONFIRM_WAIT',
                               dict(detail, gate=gate, policy='V4_S2_DELAY_CONFIRM'))
                return
            # confirmed: require both legs executable now
            return _no_main_only(self, conn, state, timestamp, day, prices, probs, books, target, case,
                                 extra_reason='confirmed_require_both_legs')
        elif gate == 'both_spread_late':
            # H2: time-of-day gate. First entry only allowed once the HK local clock is
            # at/after a fixed cutoff. params['after_minute'] (minutes since local
            # midnight) takes precedence; else params['after_hour']*60 (default 14:00).
            # Uses ONLY the current frame's HK-local clock (timestamp is HK ISO) -- a
            # fixed wall-clock cutoff, NOT the data's last frame and NOT the
            # settlement/close time. Then require both_spread quality + both legs.
            # RELEASE NOTE: the historical comment overstates the code below.
            # This branch calls only _no_main_only; no explicit spread/depth test.
            after_min = int(params['after_minute']) if 'after_minute' in params \
                else int(params.get('after_hour', 14)) * 60
            try:
                cur_min = int(timestamp[11:13]) * 60 + int(timestamp[14:16])  # HK HH:MM
            except (ValueError, IndexError, TypeError):
                cur_min = None
            detail = {'hk_minute': cur_min, 'after_minute': after_min}
            if cur_min is None or cur_min < after_min:
                _ladder._event(conn, timestamp, day, 'ENTRY_TIME_WAIT',
                               dict(detail, gate=gate, policy='V4_H2_LATE_ENTRY'))
                return
            return _no_main_only(self, conn, state, timestamp, day, prices, probs, books, target, case,
                                 extra_reason='after_hour_require_both_legs')
        elif gate == 'both_spread_trend':
            # H3: Main normalized-probability trend/variance gate. Require the last k
            # DISTINCT-timestamp valid frames of the SAME Main to have slope>=0
            # (last-first >= -tol) AND stdev<=max_std. Decision-time only (past+current
            # probs). A Main-identity change clears the per-day history. Repeated calls
            # at the same timestamp do NOT append (dedup). Insufficient history waits.
            k = int(params.get('trend_k', 10))
            max_std = float(params.get('max_std', 0.03))
            cur_p = probs.get(main)
            prev_main, hist, last_ts = confirm_state.get(day, (None, [], None))
            if main != prev_main:
                hist = []  # Main changed -> reset the probability history
            if cur_p is not None and timestamp != last_ts:
                hist = (hist + [float(cur_p)])[-k:]  # one sample per distinct timestamp
            confirm_state[day] = (main, hist, timestamp)
            if len(hist) < k:
                _ladder._event(conn, timestamp, day, 'ENTRY_TREND_WAIT',
                               {'main': main, 'n_hist': len(hist), 'need': k,
                                'gate': gate, 'policy': 'V4_H3_TREND'})
                return
            slope = hist[-1] - hist[0]
            mean = sum(hist) / len(hist)
            std = (sum((x - mean) ** 2 for x in hist) / len(hist)) ** 0.5
            detail = {'main': main, 'slope': slope, 'std': std, 'max_std': max_std, 'k': k}
            if slope < -1e-9 or std > max_std + _EPS:
                _ladder._event(conn, timestamp, day, 'ENTRY_TREND_WAIT',
                               dict(detail, gate=gate, policy='V4_H3_TREND'))
                return
            return _no_main_only(self, conn, state, timestamp, day, prices, probs, books, target, case,
                                 extra_reason='trend_ok_require_both_legs')
        elif gate == 'both_legs':
            # require both legs executable together (reuse tf.ablation semantics)
            return _no_main_only(self, conn, state, timestamp, day, prices, probs, books, target, case)
        elif gate in ('minprice', 'both_minprice'):
            mp = params['min_price']
            m_ask, _ = _best_prices(m_book, 'ask')
            detail = {'main_ask': m_ask, 'min_price': mp}
            if m_ask is None or m_ask < mp:
                blocked, reason = True, 'main_price_below_floor'
            elif gate == 'both_minprice':
                return _no_main_only(self, conn, state, timestamp, day, prices, probs, books, target, case,
                                     extra_reason='price_floor_ok_require_both_legs')
        elif gate == 'mainlead':
            lead = params['lead']
            ranked = sorted(probs.values(), reverse=True)
            margin = ranked[0] - ranked[1] if len(ranked) > 1 else ranked[0]
            detail = {'main_prob': ranked[0], 'second_prob': ranked[1] if len(ranked) > 1 else None,
                      'margin': margin, 'required_lead': lead}
            if margin + _EPS < lead:
                blocked, reason = True, 'main_lead_below_margin'
        elif gate == 'spread_depth':
            m_ask, _ = _best_prices(m_book, 'ask')
            m_bid, _ = _best_prices(m_book, 'bid')
            depth = _ask_depth(m_book)
            spread = (m_ask - m_bid) if (m_ask is not None and m_bid is not None) else None
            detail = {'main_ask': m_ask, 'main_bid': m_bid, 'spread': spread, 'ask_depth': depth,
                      'max_spread': params['max_spread'], 'min_depth': params['min_depth']}
            if spread is None or spread > params['max_spread'] + _EPS or depth + _EPS < params['min_depth']:
                blocked, reason = True, 'spread_or_depth_fail'
        elif gate in ('all_gates', 'both_spread'):
            # Composite: spread/depth (+ lead for all_gates) checked first on the
            # Main book; if they pass, require BOTH legs executable together.
            m_ask, _ = _best_prices(m_book, 'ask')
            m_bid, _ = _best_prices(m_book, 'bid')
            depth = _ask_depth(m_book)
            spread = (m_ask - m_bid) if (m_ask is not None and m_bid is not None) else None
            ranked = sorted(probs.values(), reverse=True)
            margin = ranked[0] - ranked[1] if len(ranked) > 1 else ranked[0]
            detail = {'main_ask': m_ask, 'main_bid': m_bid, 'spread': spread, 'ask_depth': depth,
                      'margin': margin, 'max_spread': params.get('max_spread'),
                      'min_depth': params.get('min_depth'), 'required_lead': params.get('lead')}
            if spread is None or spread > params['max_spread'] + _EPS or depth + _EPS < params['min_depth']:
                blocked, reason = True, 'spread_or_depth_fail'
            elif gate == 'all_gates' and margin + _EPS < params['lead']:
                blocked, reason = True, 'main_lead_below_margin'
            if not blocked:
                return _no_main_only(self, conn, state, timestamp, day, prices, probs, books, target, case,
                                     extra_reason=f'{gate}_quality_ok_require_both_legs')
        if blocked:
            _ladder._event(conn, timestamp, day, 'ENTRY_GATE_BLOCK',
                           dict(detail, gate=gate, reason=reason, main=main, protection=protection,
                                policy='V4_ENTRY_GATE'))
            return
        return _ORIG(self, conn, state, timestamp, day, prices, probs, books, target, case)
    return patched

def _no_main_only(self, conn, state, timestamp, day, prices, probs, books, target, case, extra_reason=None):
    """Both-legs-required first entry (same economics as tf.ablation)."""
    import math as _m
    held, deployed = _ladder._portfolio(conn, day)
    main, protection = state['main'], state['protection']
    available = max(0., target - deployed)
    p_book, m_book = books.get(protection, {}), books.get(main, {})
    desired = max(0., .9 * target - held.get(protection, 0.))
    p_cost = _ladder._cost_for_shares(p_book, desired) if desired > _EPS else 0.
    if not _m.isfinite(p_cost):
        p_cost = _ladder._quote(p_book)['depth_cost']
    if 0 < p_cost < _ladder._min_cost(p_book) <= available + _EPS:
        p_cost = _ladder._min_cost(p_book)
    p_quote = _ladder._quote(p_book, cost=min(available, p_cost))
    p_min = float(p_book.get('min_order_size') or _ladder.MIN_ORDER_SHARES)
    actual_p = p_quote['cost'] if p_quote['shares'] + _EPS >= p_min else 0.
    m_quote = _ladder._quote(m_book, cost=max(0., available - actual_p))
    m_price, p_price = m_quote['vwap'], p_quote['vwap']
    price_sum = m_price + p_price if m_price is not None and p_price is not None else None
    both_ok = (price_sum is not None and price_sum <= 1. + _EPS and actual_p > 0
               and m_quote['shares'] + _EPS >= float(m_book.get('min_order_size') or _ladder.MIN_ORDER_SHARES))
    if both_ok:
        _ladder._allocate(conn, state, timestamp, day, prices, probs, books, target, case)
        conn.execute('DELETE FROM paper_entry_wait WHERE target_date=?', (day,))
        return
    _ladder._event(conn, timestamp, day, 'ENTRY_BOTH_LEGS_REQUIRED_WAIT',
                   {'main': main, 'protection': protection, 'price_sum': price_sum,
                    'protection_cost': actual_p, 'policy': 'V4_BOTH_LEGS',
                    'reason': extra_reason or 'first_entry_requires_both_legs'})

def settle_event(result, winner, assumed_ok):
    """Compute the SETTLED release delta for a finished event.
    Returns (settled_delta, basis) where basis in known/assumed/window_end_proxy."""
    cash_change = result['cash_change']
    residuals = result['residual_shares'] or {}
    if winner is not None and (assumed_ok or winner not in []):  # winner present
        payout = sum(q for name, q in residuals.items() if name == winner)
        return cash_change + payout, ('assumed' if result.get('_assumed') else 'official')
    # unknown winner: release residuals at window-end liquidation estimate.
    # window_end_liquidation_estimate_pnl = cash_change + liquidation of residuals.
    return result['window_end_liquidation_estimate_pnl'], 'window_end_proxy'

def run_config(cfg_id, cfg, capital, days, *, store=True):
    """Thread ONE account across `days` (date-ordered) for one config/capital."""
    m = manifest()
    sources = {s['day']: s for s in m['sources']}
    ladder_id = cfg['ladder']
    gate = cfg['gate']
    no_protection = bool(cfg.get('no_protection'))
    params = {k: cfg[k] for k in ('min_price', 'lead', 'max_spread', 'min_depth', 'confirm_ticks',
                                  'after_hour', 'after_minute', 'trend_k', 'max_std') if k in cfg}
    adverse_ticks = int(cfg.get('adverse_ticks') or 0)
    exec_delay_min = int(cfg.get('exec_delay_min') or 0)
    fixed_bankroll = cfg.get('fixed_bankroll')
    early_trim = bool(cfg.get('early_trim'))
    no_reentry = bool(cfg.get('no_reentry'))
    cash_on_hand = float(capital)
    ledger = []
    cash_binding_events = 0
    for day in days:
        source = sources[day]
        # Fund THIS event with cash on hand. Floor at 0 (bankrupt => no trade).
        fund = max(0.0, cash_on_hand)
        res = replay_one(source, fund, ladder_id, gate, params, no_protection=no_protection,
                         fixed_bankroll=fixed_bankroll, early_trim=early_trim, no_reentry=no_reentry,
                         adverse_ticks=adverse_ticks, exec_delay_min=exec_delay_min)
        peak_buy = res['buy_cost_including_fees']
        binding = peak_buy > fund + 1e-6
        if binding:
            cash_binding_events += 1
        w = WINNER.get(day)
        assumed = day in ASSUMED
        res['_assumed'] = assumed
        settled_delta, basis = settle_event(res, w, assumed_ok=True)
        cash_before = cash_on_hand
        cash_on_hand = cash_on_hand + settled_delta
        ledger.append({
            'day': day, 'winner': w, 'winner_assumed': assumed, 'settlement_basis': basis,
            'funded_with': round(fund, 4), 'peak_buy_cost_incl_fees': round(peak_buy, 4),
            'cash_binding': binding, 'realized_cash_change': round(res['cash_change'], 4),
            'residual_shares': res['residual_shares'],
            'window_end_liquidation_pnl': round(res['window_end_liquidation_estimate_pnl'], 4),
            'settled_delta': round(settled_delta, 4),
            'cash_before': round(cash_before, 4), 'cash_after': round(cash_on_hand, 4),
            'exit_trigger': classify(res), 'buy_orders': res['buy_orders'],
            'sell_orders': res['sell_orders'], 'fees': round(res['fees'], 4),
        })
    return {'config': cfg_id, 'capital': capital, 'ladder': ladder_id, 'gate': gate,
            'params': params, 'start_capital': capital,
            'final_cash': round(cash_on_hand, 4),
            'net_profit': round(cash_on_hand - capital, 4),
            'return_pct': round((cash_on_hand - capital) / capital * 100, 4),
            'events': len(days), 'cash_binding_events': cash_binding_events,
            'ledger': ledger}

def classify(res):
    """Compact exit trigger from the per-event result's exit_state + categories."""
    return res.get('exit_state', '?')

def replay_one(source, fund, ladder_id, gate, params, *, no_protection=False,
               fixed_bankroll=None, early_trim=False, no_reentry=False,
               adverse_ticks=0, exec_delay_min=0):
    """One event's full true-fee strategy path with INITIAL_CASH=fund and the
    chosen ladder + first-entry gate patched in. Returns v3_backtest.collect()."""
    planner, risk, legacy, parse_name = base.load_engine()
    cash, ladder, pe = planner.cash, planner.ladder, planner.pe
    day = source['day']
    kind = source['kind']
    if kind == 'V3':
        frames = _read_frames(source['path'])
        assert len(frames) == 360 and all(r['quality'] == 'COMPLETE_REPLAY' for r in frames)
    else:
        frames = json.loads(Path(source['path']).read_text(encoding='utf-8'))
    skipped, inferred = Counter(), Counter()
    cache, cost_cache = {}, {}
    tq, tc = tf.true_executable_quote, tf.true_cost_for_shares

    def quote(book, *, cost=None, shares=None, side='BUY'):
        key = base.quote_key(book, cost, shares, side, pe.fee)
        if key not in cache:
            cache[key] = tq(book, cost=cost, shares=shares, side=side)
        return deepcopy(cache[key])

    def cost_quote(book, shares):
        key = base.quote_key(book or {}, None, shares, 'COST', pe.fee)
        if key not in cost_cache:
            cost_cache[key] = tc(book, shares)
        return cost_cache[key]

    class Client:
        current = None
        def get_official_resolution(self):
            return {'is_officially_resolved': False, 'is_terminal': False}
        def fetch_clob_book(self, asset):
            return deepcopy(next(b for b in self.current[1].values() if b['asset_id'] == asset))
    client = Client()

    def market_inputs(_, details):
        details.update(deepcopy(client.current[2]))
        return dict(client.current[0]), dict(client.current[3]), False

    last = None
    from datetime import datetime, timezone
    with tempfile.TemporaryDirectory(prefix='hko_v4_') as folder:
        directory = Path(folder).resolve()
        with patch.object(cash, 'INITIAL_CASH', fund), redirect_stdout(io.StringIO()):
            cash.initialize(directory, enable_relay=False)
        conn = sqlite3.connect(':memory:')
        with closing(sqlite3.connect(ladder.path(directory))) as initial:
            initial.backup(conn)
        conn.row_factory = sqlite3.Row
        with conn:
            conn.execute("INSERT INTO paper_targets(target_date,state) VALUES(?,'ACTIVE')", (day,))
            conn.execute('UPDATE paper_cash_account SET cash=?,initial_cash=?,active_target_date=?', (fund, fund, day))
            risk.schema(conn); legacy.schema(conn)
        proxy = base.SharedConnection(conn)
        epoch = json.loads((directory / 'paired_ladder_epoch.json').read_text(encoding='utf-8'))
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(cash, 'connect', return_value=proxy))
                stack.enter_context(patch.object(ladder, 'connect', return_value=proxy))
                stack.enter_context(patch.object(ladder, 'initialize', return_value=epoch))
                stack.enter_context(legacy.adapters())
                stack.enter_context(risk.adapters())
                stack.enter_context(patch.object(planner, 'executable_quote', quote))
                stack.enter_context(patch.object(pe, 'quote', quote))
                stack.enter_context(patch.object(ladder, '_cost_for_shares', cost_quote))
                stack.enter_context(patch.object(cash, 'market_inputs', side_effect=market_inputs))
                stack.enter_context(patch.object(ladder, 'solve_handover', tf.true_executable_minimax))
                if ladder_id is not None:
                    tiers = ladder_tuple(ladder_id)
                    from src.paired_cash_policy import CashPolicy
                    original_recovery = CashPolicy.recovery
                    def recovery(self, conn_, state, timestamp, day_, prices, probs, books, _tier):
                        old_tier = sum(probs[state['main']] + 1e-7 >= t for t, _, _ in tier.ORIGINAL)
                        with patch.object(ladder, 'TIERS', tier.ORIGINAL):
                            return original_recovery(self, conn_, state, timestamp, day_, prices, probs, books, old_tier)
                    stack.enter_context(patch.object(ladder, 'TIERS', tiers))
                    stack.enter_context(patch.object(CashPolicy, 'recovery', recovery))
                if gate:
                    stack.enter_context(patch.object(policy_mod.CashPolicy, '_allocate_original',
                                                     make_gate(gate, params)))
                if no_protection:
                    stack.enter_context(patch.object(_ladder, '_buy', _buy_no_protection))
                if fixed_bankroll is not None:
                    # Capital-invariant sizing: both accounts size every event from the
                    # SAME reference bankroll, so the gate decision + absolute order sizes
                    # are identical across caps. opening_bankroll feeds bankroll_before
                    # (budget = bankroll_before x tier%) and the recovery cash cap.
                    from src.paired_cash_policy import CashPolicy as _CP
                    stack.enter_context(patch.object(_CP, 'opening_bankroll',
                                                     lambda self, conn_, day_, _v=float(fixed_bankroll): _v))
                for _fi, frame in enumerate(frames):
                    cache.clear(); cost_cache.clear()
                    try:
                        if kind == 'V3':
                            value = base.inputs_for_frame(json.loads(frame['books_json']),
                                                          source['report']['identity'], .05, parse_name)
                            timestamp = datetime.fromtimestamp(frame['minute_us'] / 1e6, timezone.utc).astimezone(base.HK).isoformat()
                        else:
                            value = engine.weather_input(frame, .05)
                            timestamp = datetime.fromisoformat(frame['received_at']).astimezone(base.HK).isoformat()
                    except ValueError as error:
                        skipped[str(error)] += 1
                        continue
                    if exec_delay_min:
                        # Execution lag: the account only ACTS one frame (~1 min) later,
                        # so gate + fill are evaluated on the NEXT frame's book. No
                        # look-ahead: the next frame is strictly later wall-time and is
                        # exactly what the book would be when a 1-min-delayed order lands.
                        # The last frame has no successor -> skip (cannot fill later).
                        if _fi + 1 >= len(frames):
                            continue
                        try:
                            nxt = base.inputs_for_frame(json.loads(frames[_fi + 1]['books_json']),
                                                        source['report']['identity'], .05, parse_name)
                            value = (value[0], nxt[1], nxt[2], value[3], value[4]) + tuple(value[5:])
                        except (ValueError, KeyError):
                            skipped['exec_delay_next_frame_unreadable'] += 1
                            continue
                    if adverse_ticks:
                        # Strictly adverse fills: shift every ask UP and every bid DOWN by
                        # adverse_ticks * tick_size (buys pay more, sells get less),
                        # clamped to [0,1]. Operates on the standard book format.
                        books_adv = {}
                        for nm, bk in value[1].items():
                            try:
                                tk = float(bk.get('tick_size') or 0.001) * int(adverse_ticks)
                            except (TypeError, ValueError):
                                tk = 0.001 * int(adverse_ticks)
                            def _shift(levels, sign):
                                out = []
                                for lv in levels:
                                    p = min(1.0, max(0.0, float(lv['price']) + sign * tk))
                                    out.append({**lv, 'price': f'{p:.4f}'})
                                return out
                            books_adv[nm] = {**bk, 'asks': _shift(bk.get('asks', []), +1),
                                             'bids': _shift(bk.get('bids', []), -1)}
                        value = (value[0], books_adv, value[2], value[3], value[4]) + tuple(value[5:])
                    client.current = value; inferred.update(value[4])
                    with redirect_stdout(io.StringIO()):
                        cash._observe(directory, day, timestamp, client, True, {})
                        if early_trim:
                            raw_books, markets = value[1], value[2]
                            exe = cash._executable_books(conn, day, raw_books, markets, {})
                            _s3_risk_exit(cash, conn, day, timestamp, exe)
                        if _INTRADAY_REC is not None:
                            # Mark-to-market: cash + liquidation value of ALL held
                            # positions at current executable bids (true-fee SELL),
                            # using real depth. Held shares with no executable bid are
                            # flagged as liquidity risk (cannot be sold now).
                            raw_books, markets = value[1], value[2]
                            exe2 = cash._executable_books(conn, day, raw_books, markets, {})
                            cash_now = conn.execute('SELECT cash FROM paper_cash_account').fetchone()[0]
                            liq = 0.0; liq_risk = 0
                            for prow in conn.execute('SELECT outcome,shares FROM positions WHERE target_date=? AND shares>?', (day, _EPS)):
                                bk = exe2.get(prow['outcome'])
                                if bk and bk.get('_paper_fee'):
                                    q = pe.quote(bk, shares=prow['shares'], side='SELL')
                                    if q and q.get('shares', 0) > _EPS:
                                        liq += q['net']
                                        if q['shares'] + _EPS < prow['shares']:
                                            liq_risk += 1  # partial-only liquidation
                                    else:
                                        liq_risk += 1  # no executable bid at all
                                else:
                                    liq_risk += 1  # no book -> cannot liquidate
                            _INTRADAY_REC.append({'day': day, 'ts': timestamp,
                                                  'equity': float(cash_now) + liq,
                                                  'cash': float(cash_now), 'liq': liq,
                                                  'liq_risk': liq_risk})
                    last = (timestamp, value)
            result = base.collect(conn, day, fund, .05, last, skipped, inferred)
            assert result['conservation_verified']
            return result
        finally:
            conn.close()
