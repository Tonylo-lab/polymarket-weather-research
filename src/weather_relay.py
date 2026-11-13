"""Offline relay schema only; discovery, HTTP and background runners excluded."""
import json
DONE=('EXITED_WAIT_RESOLUTION','SETTLED')
SKIPPED=('RELAY_SKIPPED_UNTRADED','RELAY_EXPIRED_UNTRADED','RELAY_VOIDED_ERROR')
POLICY='OFFLINE_RESEARCH_NO_RELAY'
class EventClient:
    def __init__(self,*args,**kwargs):
        raise RuntimeError('NETWORK_RELAY_EXCLUDED')
def _observe_remaining(*args,**kwargs):
    raise RuntimeError('NETWORK_RELAY_EXCLUDED')


def schema(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS paper_relay_events(
          target_key TEXT PRIMARY KEY,event_id TEXT UNIQUE,slug TEXT NOT NULL UNIQUE,
          contract_date TEXT NOT NULL,city_slug TEXT NOT NULL,city_name TEXT NOT NULL,
          timezone TEXT NOT NULL,legacy_names INTEGER NOT NULL,
          selected_at TEXT,selection_liquidity REAL,selection_main_probability REAL,
          outcome_labels_json TEXT NOT NULL,outcome_identity_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS paper_relay_control(
          id INTEGER PRIMARY KEY CHECK(id=1),phase TEXT NOT NULL,
          active_target_key TEXT,last_completed_key TEXT,last_scan_at TEXT,last_error TEXT);
        CREATE TABLE IF NOT EXISTS paper_relay_scans(
          id INTEGER PRIMARY KEY,timestamp TEXT NOT NULL,total_events INTEGER NOT NULL,
          eligible_events INTEGER NOT NULL,selected_key TEXT,candidates_json TEXT NOT NULL,
          errors_json TEXT NOT NULL);
    ''')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(paper_relay_control)')}
    if 'anchor_target_key' not in columns:
        conn.execute('ALTER TABLE paper_relay_control ADD COLUMN anchor_target_key TEXT')


def _metadata(row):
    item = dict(row)
    item['outcome_labels'] = json.loads(item.pop('outcome_labels_json'))
    item['outcome_identity'] = json.loads(item.pop('outcome_identity_json'))
    return item


def metadata_for(conn, key):
    row = conn.execute('SELECT * FROM paper_relay_events WHERE target_key=?', (key,)).fetchone()
    return _metadata(row) if row else None


def anchor_complete(conn):
    row = conn.execute('SELECT t.state FROM paper_relay_control c JOIN paper_targets t ON t.target_date=c.anchor_target_key WHERE c.id=1').fetchone()
    return bool(row and row['state'] in DONE)


def _finish(conn, key, timestamp):
    from src import paired_ladder as ladder
    control = conn.execute('SELECT active_target_key FROM paper_relay_control WHERE id=1').fetchone()
    if not control or control['active_target_key'] != key or not anchor_complete(conn):
        raise RuntimeError('Global relay cannot release before its Hong Kong anchor completes')
    conn.execute("UPDATE paper_relay_control SET phase='SEARCHING',active_target_key=NULL,last_completed_key=? WHERE id=1", (key,))
    conn.execute('UPDATE paper_cash_account SET active_target_date=NULL WHERE id=1')
    ladder._event(conn, timestamp, key, 'GLOBAL_RELAY_RELEASED', {'next_action': 'search local-today events'})
