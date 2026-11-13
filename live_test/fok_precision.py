"""Pure integer FOK amount rules and sanitized, finite venue rejection reasons."""

from math import gcd, lcm

from live_test import probe

UNIT = 1_000_000

CENT = 10_000

SHARE_STEP = 10_000

def quantity_step(side, limit_ppm):
    probe.require(side in ('BUY', 'SELL') and type(limit_ppm) is int and 0 < limit_ppm < UNIT,
                  'INVALID_FOK_PRICE_OR_SIDE')
    # BUY's maker amount is dollars to cents; shares remain exact at 2dp.
    # SELL's maker is shares (2dp), while the received dollars may be finer.
    return lcm(SHARE_STEP, UNIT * CENT // gcd(limit_ppm, UNIT * CENT)) if side == 'BUY' else SHARE_STEP

def amounts(side, shares_raw, limit_ppm):
    step = quantity_step(side, limit_ppm)
    probe.require(type(shares_raw) is int and shares_raw > 0 and shares_raw % SHARE_STEP == 0,
                  'FOK_SHARE_PRECISION')
    probe.require(shares_raw % step == 0, 'FOK_BUY_NOTIONAL_PRECISION')
    product = shares_raw * limit_ppm
    probe.require(product % UNIT == 0 and product // UNIT > 0, 'FOK_AMOUNT_PRECISION')
    principal = product // UNIT
    return (principal, shares_raw) if side == 'BUY' else (shares_raw, principal)

def rejection_reason(message, code=None):
    """Classify in memory only. Never persist remote strings, bodies or headers."""
    text = str(message).lower()[:4096]
    if code == 'fok_not_filled' or "order couldn't be fully filled" in text:
        return 'FOK_NOT_FILLED'
    if 'invalid amounts' in text and 'maker amount' in text and any(x in text for x in ('accuracy', 'decimals', 'precision')):
        return 'FOK_INVALID_AMOUNTS'
    if code == 'not_enough_balance' or 'not enough balance / allowance' in text:
        return 'INSUFFICIENT_BALANCE_OR_ALLOWANCE'
    if 'invalid signature' in text:
        return 'INVALID_SIGNATURE'
    if 'invalid' in text and ('tick size' in text or 'tick-size' in text):
        return 'INVALID_PRICE_TICK'
    if 'invalid amount for a marketable buy order' in text and 'min size' in text:
        return 'BELOW_MARKET_MINIMUM'
    if code == 'market_not_ready' or text == 'the market is not yet ready to process new orders':
        return 'MARKET_NOT_READY'
    return None

def rejection_details(reason, http_status=None):
    known = {'FOK_NOT_FILLED', 'FOK_INVALID_AMOUNTS', 'INSUFFICIENT_BALANCE_OR_ALLOWANCE',
             'INVALID_SIGNATURE', 'INVALID_PRICE_TICK', 'BELOW_MARKET_MINIMUM', 'MARKET_NOT_READY'}
    probe.require(reason in known, 'UNRECOGNIZED_VENUE_REJECTION')
    value = {'ok': False, 'stage': 'venue_order_rejection', 'error_code': 'VENUE_' + reason,
             'retryable': reason in ('FOK_NOT_FILLED', 'MARKET_NOT_READY')}
    if http_status is not None:
        value['http_status'] = http_status
    return value
