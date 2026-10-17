"""Read existing code for a context request without initializing business tables."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3


def load_current_code(path, goal_id, code_version):
    """Caller supplies an authorized goal; this is not a frontend auth endpoint."""
    if type(code_version) is not int or code_version < 1:
        raise ValueError('INVALID_CODE_VERSION')
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            if db.execute('SELECT goal_id FROM goal_records WHERE goal_id=?', (goal_id,)).fetchone() is None:
                raise ValueError('GOAL_NOT_FOUND')
            row = db.execute('SELECT * FROM code_snapshots WHERE goal_id=? AND code_version=?',
                             (goal_id, code_version)).fetchone()
            if row is None:
                raise ValueError('CODE_NOT_FOUND')
            if hashlib.sha256(row['source_code'].encode('utf-8')).hexdigest() != row['content_hash']:
                raise ValueError('SOURCE_HASH_MISMATCH')
            content = {'snapshotRef': row['code_snapshot_id'], 'text': row['source_code']}
            digest = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
            return {'ref': row['code_snapshot_id'], 'goalContext': {'goalId': goal_id, 'scope': 'goal'},
                    'kind': 'code', 'version': str(code_version), 'content': content,
                    'contentHash': 'sha256:' + digest}
    except (sqlite3.Error, OSError):
        raise ValueError('CONTEXT_SOURCE_UNAVAILABLE') from None
