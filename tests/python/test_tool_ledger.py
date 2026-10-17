from focusmate.adapters.tool_ledger import SQLiteToolLedger


def test_context_call_history_and_reservation_are_persistent(tmp_path):
    path = tmp_path / 'ledger.sqlite3'
    ledger = SQLiteToolLedger(path)
    first, prior = ledger.start_call('loop', 'context', 'c1', 'request')
    assert prior == []
    ledger.finish_call(first, '{"status":"succeeded"}')
    reopened = SQLiteToolLedger(path)
    second, prior = reopened.start_call('loop', 'context', 'c2', 'request2')
    assert second != first
    assert prior == [('request', '{"status":"succeeded"}')]
    assert reopened.start_call('loop', 'changed', 'c3', 'request3') is None
    assert ledger.reserve_run('loop', 'identity')
    assert reopened.reserve_run('loop', 'identity')
    assert not reopened.reserve_run('loop', 'other')


def test_competing_run_reservations_only_one_identity_wins(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    path = tmp_path / 'ledger.sqlite3'
    ledger = SQLiteToolLedger(path)
    barrier = Barrier(2)
    def reserve(identity):
        barrier.wait()
        return ledger.reserve_run('loop', identity)
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(reserve, ['one', 'two']))
    assert sorted(answers) == [False, True]


def test_gateway_can_use_memory_ledger_without_storage_imports():
    import runpy
    from pathlib import Path
    from focusmate.core.tool_gateway import ToolGateway
    helper = runpy.run_path(str(Path(__file__).with_name('test_tool_gateway.py')))
    class MemoryLedger:
        def __init__(self): self.results = []
        def start_call(self, *args): return 1, []
        def reserve_run(self, *args): return True
        def finish_call(self, identity, result): self.results.append(result)
    ledger = MemoryLedger()
    gateway = ToolGateway(helper['Backend'](), ledger)
    result = gateway.invoke(helper['context'](), helper['call']('read_submission_result', {'submissionId': 's1'}))
    assert result['status'] == 'succeeded'
    assert len(ledger.results) == 1
