from concurrent.futures import ThreadPoolExecutor
from threading import Event, Barrier
import sqlite3
import subprocess
import sys
import os
from pathlib import Path
import pytest
from test_session_manager import setup, ask, candidate
from focusmate.core.session_manager import SessionManager
from focusmate.adapters.manager_storage import SQLiteManagerStore


@pytest.mark.parametrize('command,payload',[('pause',{}),('message',{'text':'补充条件'}),('code_changed',{'codeVersion':4})])
def test_committed_change_fences_other_connection_publisher(tmp_path,command,payload):
    m,c=setup(tmp_path);ask(m);candidate(m);committed=Event()
    def update():
        other=SessionManager(SQLiteManagerStore(m.store.path),clock=c)
        other.command('s1','change',command,payload);committed.set()
    def publish():
        other=SessionManager(SQLiteManagerStore(m.store.path),clock=c)
        assert committed.wait(5)
        return other.publish('s1')
    with ThreadPoolExecutor(2) as pool:
        p=pool.submit(publish);u=pool.submit(update);u.result();assert p.result() is None
    assert not m.read('s1')['outbox']


def test_two_publishers_one_stable_action(tmp_path):
    m,c=setup(tmp_path);ask(m);candidate(m);barrier=Barrier(2)
    def publish():
        other=SessionManager(SQLiteManagerStore(m.store.path),clock=c);barrier.wait();return other.publish('s1')
    with ThreadPoolExecutor(2) as pool:answers=list(pool.map(lambda _:publish(),range(2)))
    assert sum(a is not None for a in answers)==1
    assert len(m.read('s1')['outbox'])==1


def test_database_failure_rolls_back_state_command_and_decision(tmp_path):
    m,c=setup(tmp_path);before=m.read('s1')
    with sqlite3.connect(m.store.path) as db:
        db.execute("CREATE TRIGGER fail_journal BEFORE INSERT ON manager_decisions BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):ask(m)
    assert m.read('s1')==before
    with sqlite3.connect(m.store.path) as db:
        assert db.execute('SELECT count(*) FROM manager_commands').fetchone()[0]==0
        db.execute('DROP TRIGGER fail_journal')
    assert ask(m)


def test_separate_process_crash_after_claim_does_not_replay(tmp_path):
    m,c=setup(tmp_path);r=ask(m)
    code="""import os,sys
sys.path.insert(0,sys.argv[2])
from focusmate.core.session_manager import SessionManager
from focusmate.adapters.manager_storage import SQLiteManagerStore
m=SessionManager(SQLiteManagerStore(sys.argv[1]),clock=lambda:1000)
assert m.claim('s1','crashing-worker',lease_seconds=1)
os._exit(17)
"""
    # The child is a fresh interpreter, not pytest's already-configured process.
    # Supply its import root explicitly so the crash test exercises the claim.
    source_root=Path(__file__).resolve().parents[2]/'src'
    env=dict(os.environ);env.pop('PYTHONPATH',None)
    result=subprocess.run([sys.executable,'-c',code,str(m.store.path),str(source_root)],capture_output=True,text=True,env=env)
    assert result.returncode==17,result.stderr
    c.value=1002;m.recover('s1')
    assert m.read('s1')['rounds'][r]['status']=='interrupted'
    assert m.claim('s1','replacement') is None


def test_idle_poll_does_not_grow_journal(tmp_path):
    m,c=setup(tmp_path);ask(m);candidate(m);m.publish('s1')
    before=m.read('s1')['revision']
    for _ in range(10):m.recover('s1');m.claim('s1','w');m.publish('s1')
    assert m.read('s1')['revision']==before
