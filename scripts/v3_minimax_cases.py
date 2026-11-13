"""Offline public-frame format adapter, retained for replay compatibility."""
import json
from pathlib import Path
from scripts import v3_backtest as base
OUT=base.ROOT/'research_outputs'


def weather_input(frame, fee_rate):
    prices, books, markets = (json.loads(frame[k]) for k in
                             ('raw_prices_json', 'books_json', 'markets_json'))
    if json.loads(frame['errors_json']):
        raise ValueError('WEATHER_FRAME_ERRORS')
    tokens = {}
    for name, book in books.items():
        assert name in prices and prices[name] is not None
        assert book.get('tick_size') and book.get('min_order_size')
        tokens[name] = book['asset_id']
        # Content identity, rather than changing wall clock / exchange hash.
        import hashlib
        book['hash'] = hashlib.sha256(json.dumps([tokens[name], book['bids'], book['asks']],
                                                sort_keys=True).encode()).hexdigest()
        if fee_rate:
            markets[name]['feesEnabled'] = True
            markets[name]['feeSchedule'] = {'rate': fee_rate, 'exponent': 1}
    return prices, books, markets, tokens, {'actual_gamma_frames': 1}
