import hashlib
import sqlite3

import pytest

from apps.api.context_storage import load_current_code


def test_read_only_code_source_checks_scope_and_hash(tmp_path):
    path = tmp_path / 'source.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE goal_records(goal_id TEXT)')
        db.execute('INSERT INTO goal_records VALUES(?)', ('g',))
        db.execute('CREATE TABLE code_snapshots(goal_id TEXT, code_version INTEGER, code_snapshot_id TEXT, source_code TEXT, content_hash TEXT)')
        db.execute('INSERT INTO code_snapshots VALUES(?,?,?,?,?)', ('g', 2, 'c2', 'dp=[0]', hashlib.sha256(b'dp=[0]').hexdigest()))
    before = path.read_bytes()
    record = load_current_code(path, 'g', 2)
    assert record['content'] == {'snapshotRef': 'c2', 'text': 'dp=[0]'}
    assert record['goalContext']['goalId'] == 'g'
    assert path.read_bytes() == before
    with pytest.raises(ValueError):
        load_current_code(path, 'other', 2)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE code_snapshots SET source_code='changed'")
    with pytest.raises(ValueError):
        load_current_code(path, 'g', 2)


def test_missing_database_not_created(tmp_path):
    path = tmp_path / 'missing.db'
    with pytest.raises(ValueError):
        load_current_code(path, 'g', 1)
    assert not path.exists()
