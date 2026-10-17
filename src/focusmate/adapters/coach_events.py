"""Business-side execution links. No model, API or orchestration dependency."""
import json

TABLES = [
    '''CREATE TABLE IF NOT EXISTS coach_workspaces (
       goal_id TEXT PRIMARY KEY, contract_version INTEGER NOT NULL, session_id TEXT NOT NULL UNIQUE,
       code_version INTEGER, targets_json TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE IF NOT EXISTS coach_execution_links (
       kind TEXT NOT NULL, execution_id TEXT NOT NULL, goal_id TEXT NOT NULL, session_id TEXT NOT NULL,
       contract_version INTEGER NOT NULL, code_version INTEGER NOT NULL, targets_json TEXT NOT NULL,
       processed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind,execution_id))''',
    '''CREATE TABLE IF NOT EXISTS coach_final_receipts (
       result_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, report_json TEXT NOT NULL)''',
]


def capture_execution(db, kind, execution_id, goal_id, code_version):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='coach_workspaces'").fetchone(): return
    row=db.execute('SELECT session_id,contract_version,targets_json,paused FROM coach_workspaces WHERE goal_id=?', (goal_id,)).fetchone()
    if not row or row[3] or not json.loads(row[2]): return
    db.execute('INSERT OR IGNORE INTO coach_execution_links VALUES(?,?,?,?,?,?,?,0)',
        (kind,execution_id,goal_id,row[0],row[1],code_version,row[2]))
