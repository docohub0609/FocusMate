"""Safe failure facts. Never store raw exception messages or testcase content."""
import json
import sqlite3
from contextlib import closing


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS submission_failures (submission_id TEXT PRIMARY KEY, diagnostic_json TEXT NOT NULL)')


def get_failure(path, submission_id):
    with closing(sqlite3.connect(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='submission_failures'").fetchone():
            return None
        row = db.execute('SELECT diagnostic_json FROM submission_failures WHERE submission_id=?', (submission_id,)).fetchone()
        return json.loads(row[0]) if row else None
