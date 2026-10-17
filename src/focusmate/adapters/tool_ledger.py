"""SQLite persistence for the injected tool ledger; no core orchestration import."""
from contextlib import closing
from pathlib import Path
import sqlite3


class SQLiteToolLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tool_loops(loop_id TEXT PRIMARY KEY,context TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tool_run_reservations(loop_id TEXT PRIMARY KEY,identity TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tool_calls(id INTEGER PRIMARY KEY,loop_id TEXT NOT NULL,call_id TEXT NOT NULL,request TEXT NOT NULL,result TEXT);
            ''')

    def start_call(self, loop_id, context, call_id, request):
        """Atomically bind the loop and append an attempt; None means context conflict."""
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT context FROM tool_loops WHERE loop_id=?', (loop_id,)).fetchone()
            if row and row[0] != context:
                return None
            db.execute('INSERT OR IGNORE INTO tool_loops VALUES(?,?)', (loop_id, context))
            prior = db.execute('SELECT request,result FROM tool_calls WHERE loop_id=? ORDER BY id', (loop_id,)).fetchall()
            identity = db.execute('INSERT INTO tool_calls(loop_id,call_id,request) VALUES(?,?,?)',
                                  (loop_id, call_id, request)).lastrowid
            db.commit()
            return identity, prior

    def reserve_run(self, loop_id, identity):
        """Atomically retain one run identity; the same identity is idempotent."""
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT identity FROM tool_run_reservations WHERE loop_id=?', (loop_id,)).fetchone()
            if row and row[0] != identity:
                return False
            db.execute('INSERT OR IGNORE INTO tool_run_reservations VALUES(?,?)', (loop_id, identity))
            db.commit()
            return True

    def finish_call(self, identity, result):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE tool_calls SET result=? WHERE id=?', (result, identity))
