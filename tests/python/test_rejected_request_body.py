"""Wrong content type must not close over an ordinary unread request body."""
from email.message import Message
from io import BytesIO
from types import SimpleNamespace

import pytest

from apps.api.__main__ import HealthHandler


def handler(body=b'not JSON', declared=None):
    value = object.__new__(HealthHandler)
    value.headers = Message()
    value.headers['Content-Type'] = 'text/plain'
    value.headers['Content-Length'] = str(len(body) if declared is None else declared)
    value.rfile = BytesIO(body)
    value.connection = SimpleNamespace(gettimeout=lambda: None, settimeout=lambda timeout: None)
    return value


@pytest.mark.parametrize('method', ['_handle_problem_intake', '_handle_problem_feature_extraction', '_request_json'])
def test_small_rejected_body_consumed_before_415(method):
    value = handler()
    statuses = []
    def error(status, *args):
        assert value.rfile.tell() == len(b'not JSON')
        statuses.append(status)
    value._send_problem_error = error
    value._send_feature_error = error
    getattr(value, method)(*(['request:1'] if method == '_request_json' else []))
    assert statuses == [415]


@pytest.mark.parametrize('declared', [-1, 0, 1024*1024+1])
def test_invalid_or_oversize_body_not_drained(declared):
    value = handler(declared=declared)
    value._discard_rejected_body()
    assert value.rfile.tell() == 0
    assert value.close_connection


def test_incomplete_body_timeout_does_not_hide_415():
    value = handler()
    class Incomplete:
        def read1(self, count): raise TimeoutError('incomplete upload')
    value.rfile = Incomplete()
    statuses = []
    value._send_problem_error = lambda status, *args: statuses.append(status)
    value._handle_problem_intake()
    assert statuses == [415]


def test_trickle_read_has_total_deadline(monkeypatch):
    import apps.api.__main__ as module
    ticks = iter([0, 0.1, 0.3])
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    value = handler(body=b'x'*100)
    reads = []
    class Trickle:
        def read1(self, count):
            reads.append(count)
            return b'x'
    value.rfile = Trickle()
    value._discard_rejected_body()
    assert len(reads) == 1  # A second byte cannot reset the absolute deadline.
