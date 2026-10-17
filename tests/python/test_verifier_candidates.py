from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError, URLError

import pytest

from apps.api import deepseek_provider as deepseek
from apps.api.deepseek_provider import DeepSeekConfig, ProviderResponse
from tests.python.test_submission_storage import frozen_problem


@pytest.fixture
def candidate_module():
    return pytest.importorskip("services.verifier_builder.candidates")


def test_candidate_generation_implementation_exists():
    assert importlib.util.find_spec("services.verifier_builder.candidates") is not None


@pytest.fixture
def context(tmp_path):
    repository, record = frozen_problem(tmp_path / "candidates.db")
    return record, repository.get_feature_set(record["lifecycle"]["confirmedFeatureSetId"])


def response_document(role, context):
    record, features = context
    document = {
        "schemaVersion": "1.0.0", "role": role,
        "problemRef": {"problemId": "problem-submit", "problemVersion": 1},
        "problemContentHash": record["lifecycle"]["contentHash"],
        "featureSetId": features["featureSetId"],
        "source": f"# PRIVATE-{role}\nprint(1)\n",
    }
    if role == "inputGenerator":
        document["coveragePlan"] = [{"tag": "minimumSize", "featureKeys": ["dataScale.n"]}]
    if role == "fastReference":
        document["mutants"] = [{"mutantKey": "off-by-one", "defectTag": "offByOne", "source": "# PRIVATE-mutant\nprint(2)\n"}]
    return document


class FakeTransport:
    def __init__(self, context, change=None, barrier=None):
        self.context = context
        self.change = change
        self.barrier = barrier
        self.calls = []

    def complete(self, *, system_prompt, user_prompt, deadline):
        payload = json.loads(user_prompt)
        role = payload["role"]
        self.calls.append((system_prompt, payload, deadline))
        if self.barrier:
            self.barrier.wait(timeout=2)
        document = response_document(role, self.context)
        if self.change:
            document = self.change(role, document)
        return ProviderResponse("response-" + role, "deepseek-test", document if isinstance(document, str) else json.dumps(document))


def test_roles_are_concurrent_isolated_and_return_immutable_internal_records(candidate_module, context):
    before = deepcopy(context)
    transport = FakeTransport(context, barrier=threading.Barrier(3))
    deadline = time.monotonic() + 5
    result = candidate_module.CandidateProvider(transport).generate(*context, deadline)
    assert len(transport.calls) == 3
    assert {call[1]["role"] for call in transport.calls} == {"inputGenerator", "slowOracle", "fastReference"}
    assert len({call[0] for call in transport.calls}) == 3
    for system, payload, received_deadline in transport.calls:
        assert received_deadline == deadline
        assert payload["problemPackage"] == context[0]["problemPackage"]
        assert payload["problemContentHash"] == context[0]["lifecycle"]["contentHash"]
        assert {f["featureKey"] for f in payload["confirmedFeatures"]} == {"dataScale.n", "input.primary", "output.primary", "domain.array"}
        assert "PRIVATE-" not in system + json.dumps(payload)
        assert payload["responseSchema"]["additionalProperties"] is False
    assert context == before
    assert result.generator.source.startswith("# PRIVATE-inputGenerator")
    assert result.slow_oracle.source.startswith("# PRIVATE-slowOracle")
    assert result.fast_reference.source.startswith("# PRIVATE-fastReference")
    assert result.generator.provider == "deepseek"
    assert result.generator.response_id == "response-inputGenerator"
    assert result.generator.model == "deepseek-test"
    assert result.generator.prompt_version
    assert result.problem_id == "problem-submit" and result.problem_version == 1
    assert result.feature_set_id == context[1]["featureSetId"]
    assert result.coverage_plan[0].feature_keys == ("dataScale.n",)
    assert len(result.mutants) == 1
    with pytest.raises(FrozenInstanceError):
        result.generator.source = "changed"
    with pytest.raises(FrozenInstanceError):
        result.coverage_plan[0].tag = "changed"
    assert "PRIVATE-" not in repr(result)


@pytest.mark.parametrize("bad", ["unknownFeature", "unconfirmedFeature", "wrongId", "wrongVersion", "wrongHash", "wrongFeatureSet", "extra", "extraNested", "wrongRole", "tooManyMutants", "duplicateMutant", "emptySource", "sourceBytes", "mutantBytes", "json", "duplicateJson", "nan", "utf8", "oversizedResponse"])
def test_untrusted_candidates_pause_safely_after_bounded_format_correction(candidate_module, context, bad, caplog, capsys):
    def change(role, document):
        if bad == "unknownFeature" and role == "inputGenerator":
            document["coveragePlan"][0]["featureKeys"] = ["invented.fact"]
        elif bad == "unconfirmedFeature" and role == "inputGenerator":
            document["coveragePlan"][0]["featureKeys"] = ["resource.primary"]
        elif bad in {"wrongId", "wrongVersion", "wrongHash", "wrongFeatureSet", "extra", "wrongRole", "emptySource", "sourceBytes", "json", "duplicateJson", "nan", "utf8", "oversizedResponse"} and role == "slowOracle":
            if bad == "wrongId": document["problemRef"]["problemId"] = "other"
            if bad == "wrongVersion": document["problemRef"]["problemVersion"] = 2
            if bad == "wrongHash": document["problemContentHash"] = "a" * 64
            if bad == "wrongFeatureSet": document["featureSetId"] = "other"
            if bad == "extra": document["privateCases"] = "SECRET-SENTINEL"
            if bad == "wrongRole": document["role"] = "fastReference"
            if bad == "emptySource": document["source"] = ""
            if bad == "sourceBytes": document["source"] = "中" * 87382
            if bad == "json": return "SECRET-SENTINEL invalid json"
            if bad == "duplicateJson": return json.dumps(document)[:-1] + ',"source":"SECRET-SENTINEL"}'
            if bad == "nan": return json.dumps(document)[:-1] + ',"extra":NaN}'
            if bad == "utf8": document["source"] = "\ud800"
            if bad == "oversizedResponse": return " " * (2 * 1024 * 1024 + 1)
        elif role == "fastReference":
            if bad == "extraNested": document["mutants"][0]["explanation"] = "SECRET-SENTINEL"
            if bad == "tooManyMutants": document["mutants"] *= 4
            if bad == "duplicateMutant": document["mutants"] *= 2
            if bad == "mutantBytes": document["mutants"][0]["source"] = "a" * (256 * 1024 + 1)
        return document
    transport = FakeTransport(context, change=change)
    with pytest.raises(candidate_module.CandidateFailure) as caught:
        candidate_module.CandidateProvider(transport).generate(*context, time.monotonic() + 5)
    assert caught.value.code == "CANDIDATE_SCHEMA_INVALID"
    assert caught.value.retryable is True
    assert len(transport.calls) <= 6  # At most one format correction per independent role.
    captured = capsys.readouterr()
    safe = str(caught.value) + repr(caught.value) + caplog.text + captured.out + captured.err
    assert "SECRET-SENTINEL" not in safe and "PRIVATE-" not in safe
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("bad", ["draft", "hash", "sourceChanged", "featureId", "featureHash", "featureVersion", "duplicateKey", "inputUnknown", "outputHypothesis", "scaleUnknown", "inputConflict", "reviewRequired"])
def test_context_and_critical_confirmed_facts_checked_before_any_call(candidate_module, context, bad):
    record, features = context
    if bad == "draft": record["lifecycle"]["status"] = "draft"
    if bad == "hash": record["lifecycle"]["contentHash"] = "a" * 64
    if bad == "sourceChanged": record["problemPackage"]["statement"] += " changed"
    if bad == "featureId": features["featureSetId"] = "other"
    if bad == "featureHash": features["problemContentHash"] = "a" * 64
    if bad == "featureVersion": features["problemRef"]["problemVersion"] = 2
    if bad == "duplicateKey": features["features"].append(deepcopy(features["features"][0]))
    if bad in {"inputUnknown", "outputHypothesis", "scaleUnknown", "inputConflict", "reviewRequired"}:
        category = {"inputUnknown": "inputStructure", "outputHypothesis": "outputStructure", "scaleUnknown": "dataScale", "inputConflict": "inputStructure", "reviewRequired": "inputStructure"}[bad]
        item = next(f for f in features["features"] if f["category"] == category)
        if bad == "reviewRequired":
            item["review"] = {"status": "required", "issue": "locallyUnverifiable", "message": "review", "allowedActions": ["confirmFeature"]}
        elif bad == "outputHypothesis":
            item.update(status="hypothesis", confidence=0.5)
        elif bad == "inputConflict":
            value = item.pop("value")
            item.pop("confidence")
            item.update(status="conflict", candidates=[
                {"value": value, "evidence": deepcopy(item["evidence"])},
                {"value": "alternative", "evidence": deepcopy(item["evidence"])},
            ])
        else:
            item.update(status="unknown", evidence=[])
            item.pop("confidence")
            item.pop("value")
    transport = FakeTransport(context)
    with pytest.raises(candidate_module.CandidateFailure) as caught:
        candidate_module.CandidateProvider(transport).generate(record, features, time.monotonic() + 5)
    assert caught.value.code == "PROBLEM_FACTS_INSUFFICIENT"
    assert not transport.calls


def test_deadline_returns_without_waiting_for_stuck_transport(candidate_module, context):
    release = threading.Event()
    started = threading.Event()
    class StuckTransport(FakeTransport):
        def complete(self, **kwargs):
            started.set()
            release.wait(timeout=3)
            return super().complete(**kwargs)
    provider = candidate_module.CandidateProvider(StuckTransport(context))
    start = time.monotonic()
    try:
        with pytest.raises(candidate_module.CandidateFailure) as caught:
            provider.generate(*context, start + 0.15)
        assert caught.value.code == "BUILD_BUDGET_EXCEEDED"
        assert time.monotonic() - start < 0.8
        assert started.is_set()
    finally:
        release.set()


def test_expired_deadline_makes_no_calls(candidate_module, context):
    transport = FakeTransport(context)
    with pytest.raises(candidate_module.CandidateFailure) as caught:
        candidate_module.CandidateProvider(transport).generate(*context, time.monotonic() - 1)
    assert caught.value.code == "BUILD_BUDGET_EXCEEDED"
    assert not transport.calls


class HttpResponse:
    def __init__(self, raw): self.raw = raw
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def read(self, limit=-1): return self.raw if limit < 0 else self.raw[:limit]


@pytest.mark.parametrize("failure", [429, 500, 503, "network", 401, 402, 400, 502, "malformed"])
def test_structured_transport_retries_only_transient_transport_failures(candidate_module, monkeypatch, failure):
    calls = []
    def open_fake(request, timeout):
        calls.append((json.loads(request.data), timeout))
        if len(calls) == 1:
            if failure == "network": raise URLError("SECRET-SENTINEL")
            if failure == "malformed": return HttpResponse(b"SECRET-SENTINEL")
            raise HTTPError(request.full_url, failure, "SECRET-SENTINEL", {}, None)
        return HttpResponse(b'{"id":"response-ok","model":"deepseek-test","choices":[{"message":{"content":"{}"}}]}')
    monkeypatch.setattr(deepseek, "urlopen", open_fake)
    transport = deepseek.StructuredChatTransport(DeepSeekConfig("SECRET-KEY", "https://unit.invalid", "deepseek-test"))
    deadline = time.monotonic() + 2
    if failure in {429, 500, 503, "network"}:
        assert transport.complete(system_prompt="role", user_prompt="{}", deadline=deadline).response_id == "response-ok"
        assert len(calls) == 2
        assert calls[1][1] < calls[0][1] <= 2
    else:
        with pytest.raises(deepseek.ProviderFailure) as caught:
            transport.complete(system_prompt="role", user_prompt="{}", deadline=deadline)
        assert len(calls) == 1
        assert "SECRET" not in str(caught.value)
        assert caught.value.__cause__ is None
    assert calls[0][0]["response_format"] == {"type": "json_object"}


def test_transport_does_not_retry_when_budget_expires(candidate_module, monkeypatch):
    calls = []
    def open_fake(request, timeout):
        calls.append(timeout)
        raise URLError("SECRET")
    monkeypatch.setattr(deepseek, "urlopen", open_fake)
    transport = deepseek.StructuredChatTransport(DeepSeekConfig("key", "https://unit.invalid", "model"))
    with pytest.raises(deepseek.ProviderFailure) as caught:
        transport.complete(system_prompt="role", user_prompt="{}", deadline=time.monotonic() + 0.05)
    assert caught.value.code == "BUILD_BUDGET_EXCEEDED"
    assert len(calls) == 1


@pytest.mark.parametrize("raw", [
    b'{"id":"first","id":"second","choices":[{"message":{"content":"{}"}}]}',
    b'{"id":"first","usage":NaN,"choices":[{"message":{"content":"{}"}}]}',
    b'{"id":"first","choices":[{"message":{"content":"{}"}}],"usage":Infinity}',
    b'[]', b'{}', b'\xff', b'x' * (2 * 1024 * 1024 + 1),
], ids=["duplicate-key", "nan", "infinity", "array", "missing-choice", "invalid-utf8", "oversized"])
def test_structured_transport_rejects_non_strict_or_oversized_envelopes(candidate_module, monkeypatch, raw):
    calls = []
    def open_fake(request, timeout):
        calls.append(request)
        return HttpResponse(raw)
    monkeypatch.setattr(deepseek, "urlopen", open_fake)
    transport = deepseek.StructuredChatTransport(DeepSeekConfig("key", "https://unit.invalid", "model"))
    with pytest.raises(deepseek.ProviderFailure) as caught:
        transport.complete(system_prompt="role", user_prompt="{}", deadline=time.monotonic() + 2)
    assert caught.value.code == "LLM_OUTPUT_INVALID"
    assert len(calls) == 1


@pytest.mark.parametrize("mutant_count", [0, 3])
def test_exact_source_byte_limit_and_zero_to_three_mutants_are_accepted(candidate_module, context, mutant_count):
    def change(role, document):
        document["source"] = "a" * (256 * 1024)
        if role == "fastReference":
            document["mutants"] = [{"mutantKey": f"mutant-{index}", "defectTag": "offByOne", "source": "a" * (256 * 1024)} for index in range(mutant_count)]
        return document
    result = candidate_module.CandidateProvider(FakeTransport(context, change)).generate(*context, time.monotonic() + 5)
    assert len(result.generator.source.encode()) == 256 * 1024
    assert len(result.mutants) == mutant_count


@pytest.mark.parametrize("code", ["LLM_UNAVAILABLE", "LLM_OUTPUT_INVALID", "BUILD_BUDGET_EXCEEDED"])
def test_provider_failures_are_replaced_with_safe_messages(candidate_module, context, code):
    class FailingTransport:
        def complete(self, **kwargs):
            raise deepseek.ProviderFailure(code, "SECRET-SENTINEL private source prompt key", True)
    with pytest.raises(candidate_module.CandidateFailure) as caught:
        candidate_module.CandidateProvider(FailingTransport()).generate(*context, time.monotonic() + 5)
    assert caught.value.code == {"LLM_UNAVAILABLE": "LLM_TEMPORARILY_UNAVAILABLE", "LLM_OUTPUT_INVALID": "CANDIDATE_SCHEMA_INVALID", "BUILD_BUDGET_EXCEEDED": "BUILD_BUDGET_EXCEEDED"}[code]
    assert "SECRET" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_repeated_expired_calls_keep_inflight_roles_bounded(candidate_module, context):
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0
    class StuckTransport(FakeTransport):
        def complete(self, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                release.wait(timeout=3)
                return super().complete(**kwargs)
            finally:
                with lock:
                    active -= 1
    provider = candidate_module.CandidateProvider(StuckTransport(context))
    try:
        for _ in range(3):
            with pytest.raises(candidate_module.CandidateFailure):
                provider.generate(*context, time.monotonic() + 0.1)
        assert maximum == 3
    finally:
        release.set()


@pytest.mark.parametrize("code", ["LLM_CONFIGURATION_MISSING", "LLM_AUTHENTICATION_FAILED", "LLM_PAYMENT_REQUIRED", "LLM_REQUEST_REJECTED"])
def test_permanent_provider_failure_requires_configuration_in_persisted_status(candidate_module, context, tmp_path, code):
    from services.verifier_builder.build_jobs import VerifierBuildJobRepository

    class FailingTransport:
        def complete(self, **kwargs):
            raise deepseek.ProviderFailure(code, "SECRET-SENTINEL key prompt source", False)

    with pytest.raises(candidate_module.CandidateFailure) as caught:
        candidate_module.CandidateProvider(FailingTransport()).generate(*context, time.monotonic() + 5)
    assert caught.value.code == "LLM_CONFIGURATION_REQUIRED"
    assert caught.value.retryable is False
    repository = VerifierBuildJobRepository(tmp_path / "candidates.db")
    claim = repository.discover_and_claim("builder-test", 30)
    status = repository.pause(claim, caught.value.code)
    assert status["allowedAction"] == "configureProvider"
    assert status["retryable"] is False
    assert "SECRET" not in str(caught.value) + json.dumps(status)


def test_missing_environment_configuration_is_a_safe_candidate_failure(candidate_module, monkeypatch):
    def missing():
        raise deepseek.ProviderFailure("LLM_CONFIGURATION_MISSING", "SECRET-SENTINEL", False)
    monkeypatch.setattr(deepseek.DeepSeekConfig, "from_environment", missing)
    with pytest.raises(candidate_module.CandidateFailure) as caught:
        candidate_module.CandidateProvider.from_environment()
    assert caught.value.code == "LLM_CONFIGURATION_REQUIRED"
    assert caught.value.retryable is False
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize("category", ["dataScale", "inputStructure", "outputStructure"])
@pytest.mark.parametrize("status", ["unknown", "hypothesis"])
def test_optional_uncertain_fact_does_not_block_confirmed_core_facts(candidate_module, context, category, status):
    features = context[1]["features"]
    optional = deepcopy(next(item for item in features if item["category"] == category))
    optional.update(featureKey=category + ".optional", status=status)
    if status == "unknown":
        optional.pop("value")
        optional.pop("confidence")
        optional["evidence"] = []
    else:
        optional["confidence"] = 0.5
    features.append(optional)
    transport = FakeTransport(context)
    result = candidate_module.CandidateProvider(transport).generate(*context, time.monotonic() + 5)
    assert optional["featureKey"] not in result.confirmed_feature_keys
    assert len(transport.calls) == 3


@pytest.mark.parametrize("operation", ["validation", "hash", "candidateValidation", "promptLoading"])
def test_internal_failures_do_not_blame_problem_facts_or_leak_details(candidate_module, context, tmp_path, monkeypatch, operation):
    from services.verifier_builder.build_jobs import VerifierBuildJobRepository

    real_validate = candidate_module.validate
    def broken_validate(title, value):
        if operation == "validation" or title.startswith("Verifier"):
            raise RuntimeError("SECRET-SENTINEL internal schema path")
        return real_validate(title, value)
    def broken_hash(value):
        raise RuntimeError("SECRET-SENTINEL internal hash path")
    if operation == "promptLoading":
        monkeypatch.setattr(candidate_module, "ROOT", tmp_path / "SECRET-SENTINEL-missing-schema")
    elif operation == "hash":
        monkeypatch.setattr(candidate_module, "problem_content_hash", broken_hash)
    else:
        monkeypatch.setattr(candidate_module, "validate", broken_validate)
    with pytest.raises(candidate_module.CandidateFailure) as caught:
        candidate_module.CandidateProvider(FakeTransport(context)).generate(*context, time.monotonic() + 5)
    assert caught.value.code == "VERIFIER_BUILDER_INTERNAL_ERROR"
    assert caught.value.retryable is True
    repository = VerifierBuildJobRepository(tmp_path / "candidates.db")
    claim = repository.discover_and_claim("builder-test", 30)
    status = repository.pause(claim, caught.value.code)
    assert status["allowedAction"] == "retry"
    assert status["retryable"] is True
    assert "SECRET" not in str(caught.value) + json.dumps(status)


@pytest.mark.parametrize("field,value", [
    ("base_url", ""), ("base_url", "SECRET-SENTINEL-invalid-url"),
    ("base_url", "ftp://SECRET-SENTINEL.invalid"), ("base_url", "https://"),
    ("api_key", " "), ("model", " "),
    ("api_key", "SECRET-SENTINEL-中文"), ("api_key", "SECRET-SENTINEL-\x00"),
    ("api_key", "SECRET-SENTINEL-\t"), ("api_key", "SECRET-SENTINEL-\x7f"),
])
def test_invalid_transport_configuration_requires_configuration(candidate_module, context, tmp_path, monkeypatch, field, value):
    from services.verifier_builder.build_jobs import VerifierBuildJobRepository

    values = dict(api_key="SECRET-SENTINEL-key", base_url="https://unit.invalid", model="model")
    values[field] = value
    monkeypatch.setattr(deepseek.DeepSeekConfig, "from_environment", lambda: DeepSeekConfig(**values))
    def no_network(*args, **kwargs):
        pytest.fail("invalid provider configuration must not make a network request")
    monkeypatch.setattr(deepseek, "urlopen", no_network)
    with pytest.raises(candidate_module.CandidateFailure) as caught:
        provider = candidate_module.CandidateProvider.from_environment()
        provider.generate(*context, time.monotonic() + 5)
    assert caught.value.code == "LLM_CONFIGURATION_REQUIRED"
    repository = VerifierBuildJobRepository(tmp_path / "candidates.db")
    claim = repository.discover_and_claim("builder-config", 30)
    status = repository.pause(claim, caught.value.code)
    assert status["allowedAction"] == "configureProvider"
    assert status["retryable"] is False
    assert "SECRET" not in str(caught.value) + json.dumps(status)


def test_slow_http_body_obeys_absolute_deadline_and_releases_transport_thread():
    stop = threading.Event()
    payload = b'{"id":"response-ok","model":"model","choices":[{"message":{"content":"{}"}}]}'
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            raw = b" " * 40 + payload if self.path.startswith("/slow/") else payload
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                if self.path.startswith("/slow/"):
                    for byte in raw:
                        if stop.wait(0.025):
                            break
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                else:
                    self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    pool = ThreadPoolExecutor(max_workers=1)
    base = f"http://127.0.0.1:{server.server_port}"
    transport = deepseek.StructuredChatTransport(DeepSeekConfig("key", base + "/slow", "model"))
    started = time.monotonic()
    try:
        future = pool.submit(transport.complete, system_prompt="role", user_prompt="{}", deadline=started + 0.12)
        with pytest.raises(deepseek.ProviderFailure) as caught:
            future.result(timeout=0.45)
        assert caught.value.code == "BUILD_BUDGET_EXCEEDED"
        assert 0.09 <= time.monotonic() - started < 0.45
        # The same single thread must be free to execute a subsequent request.
        fast = deepseek.StructuredChatTransport(DeepSeekConfig("key", base + "/fast", "model"))
        response = pool.submit(fast.complete, system_prompt="role", user_prompt="{}", deadline=time.monotonic() + 1).result(timeout=1)
        assert response.response_id == "response-ok"
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        pool.shutdown(wait=True)
        server_thread.join(timeout=1)


def test_generator_and_oracle_share_bounded_small_case_policy(candidate_module, context):
    transport=FakeTransport(context)
    candidate_module.CandidateProvider(transport).generate(*context,time.monotonic()+5)
    policies=[payload['smallCasePolicy'] for _,payload,_ in transport.calls]
    assert len(policies)==3 and all(policy==policies[0] for policy in policies)
    assert policies[0]['maxEnumerationDimension']==8
    assert policies[0]['wallTimeSeconds']==3
    assert all('smallCasePolicy' in system for system,_,_ in transport.calls)
