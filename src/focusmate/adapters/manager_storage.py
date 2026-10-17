"""SQLite atomic aggregate/command journal. No imports from core orchestration."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

from focusmate.contracts.manager import ManagerError, encode
from focusmate.contracts.schema_validation import validate


class SQLiteManagerStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS manager_sessions(session_id TEXT PRIMARY KEY, state_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS manager_commands(session_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    command_hash TEXT NOT NULL, response_json TEXT NOT NULL, PRIMARY KEY(session_id,request_id));
                CREATE TABLE IF NOT EXISTS manager_decisions(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL, revision INTEGER NOT NULL, command_json TEXT NOT NULL,
                    response_json TEXT NOT NULL, UNIQUE(session_id,revision));
            ''')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def create(self, session_id, state):
        validate('ManagerSession',state)
        with closing(self._connect()) as db, db:
            try: db.execute('INSERT INTO manager_sessions VALUES(?,?)',(session_id,encode(state)))
            except sqlite3.IntegrityError: raise ManagerError('SESSION_EXISTS') from None
        return deepcopy(state)

    def read(self, session_id):
        with closing(self._connect()) as db:
            row = db.execute('SELECT state_json FROM manager_sessions WHERE session_id=?',(session_id,)).fetchone()
            if not row: raise ManagerError('SESSION_NOT_FOUND')
            return json.loads(row[0])

    def change(self, session_id, request_id, command, operation):
        digest=hashlib.sha256(encode(command).encode()).hexdigest()
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT command_hash,response_json FROM manager_commands WHERE session_id=? AND request_id=?', (session_id,request_id)).fetchone()
            if old:
                if old[0]!=digest: raise ManagerError('REQUEST_CONFLICT')
                return json.loads(old[1])
            row=db.execute('SELECT state_json FROM manager_sessions WHERE session_id=?',(session_id,)).fetchone()
            if not row: raise ManagerError('SESSION_NOT_FOUND')
            state=json.loads(row[0])
            before=encode(state)
            result=operation(state)
            if request_id.startswith('internal-') and encode(state)==before:
                return deepcopy(result)
            state['revision']+=1
            validate('ManagerSession',state)
            db.execute('UPDATE manager_sessions SET state_json=? WHERE session_id=?',(encode(state),session_id))
            db.execute('INSERT INTO manager_commands VALUES(?,?,?,?)',(session_id,request_id,digest,encode(result)))
            db.execute('INSERT INTO manager_decisions(session_id,revision,command_json,response_json) VALUES(?,?,?,?)',
                       (session_id,state['revision'],encode(command),encode(result)))
            db.commit()
            return deepcopy(result)
