"""Anonymous shapes of the 2026-10-14 live failures, without network calls."""
from copy import deepcopy
import json

import pytest

from focusmate.core.dynamic_coach_execution import DynamicCoachExecutor
from tests.python.test_coach_compact_flow import compact, approved, run_step
from tests.python.test_dynamic_coach_manager import manager
from tests.python.test_dynamic_coach_loop import materials


class TextProvider:
    public_config = {"model": "explicit-replay", "maxTokens": 8192}

    def __init__(self, text):
        self.text = text
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": self.text, "toolCalls": [],
                "responseAudit": {"finishReason": "stop"}}


def execute_text(m, text):
    step = m.claim("s", "replay", materials=materials(m))
    result = DynamicCoachExecutor(TextProvider(text)).execute(m.read("s"), step)
    m.complete("s", step["stepId"], step["token"], result)
    return result


def test_unique_complete_object_with_one_extra_closer_keeps_help_for_review(tmp_path):
    m = manager(tmp_path, workflow_version=2)
    m.command("s", "ask", "ask", {"text": "这道题如何读入两行数据？"})
    value = compact(content="第一行用 input().split() 读取数字，第二行读取目标数。")
    original = json.dumps(value, ensure_ascii=False) + "}"
    result = execute_text(m, original)
    state = m.read("s")
    assert state["rounds"][state["currentRoundId"]]["phase"] == "review"
    assert result["value"] == value
    assert result["responseNormalization"]["original"]["text"] == original
    assert result["responseNormalization"]["kind"] == "redundantTrailingCloser"
    assert not state["outbox"]  # Syntax recovery must not bypass semantic review.
    run_step(m, approved)
    state = m.read("s")
    assert state["rounds"][state["currentRoundId"]]["counts"]["model"] == 2
    assert len(state["outbox"]) == 1 and not state["workGoals"]


@pytest.mark.parametrize("suffix", ["{}", "[]", "true", "reply", ",", "]}"])
def test_extra_data_with_any_content_is_not_removed(tmp_path, suffix):
    m = manager(tmp_path, workflow_version=2)
    m.command("s", "ask", "ask", {"text": "怎样读入？"})
    result = execute_text(m, json.dumps(compact()) + suffix)
    assert result["kind"] == "failure"
    assert "responseNormalization" not in result
    assert not m.read("s")["outbox"]


@pytest.mark.parametrize("text", ['{"choice":"help","choice":"wait"}}', '{"choice":"help"',
                                   '{"choice":"help","value":1e999}}'])
def test_duplicates_truncation_and_nonfinite_values_remain_rejected(tmp_path, text):
    m = manager(tmp_path, workflow_version=2)
    m.command("s", "ask", "ask", {"text": "怎样读入？"})
    assert execute_text(m, text)["kind"] == "failure"
    assert not m.read("s")["outbox"]


def test_followup_target_mismatch_names_both_fields_in_repair_request(tmp_path):
    m = manager(tmp_path, workflow_version=2,repair_policy_version=0)
    m.command("s", "ask", "ask", {"text": "我的输入对吗？"})
    bad = compact(content="请把 split(input()) 改成 input().split()。")
    bad["targets"] = []
    bad["help"]["followUp"] = {"kind": "awaitEvidence", "reason": "检查修改后的输入。",
        "expectedUserAction": "改好后再检查。", "checks": ["codeStructure"], "targets": ["work-old-dp"]}
    result = execute_text(m, json.dumps(bad, ensure_ascii=False))
    assert result["kind"] == "failure"
    assert "help.followUp.targets" in result["formatError"]
    assert "work-old-dp" in result["formatError"] and "顶层 targets" in result["formatError"]
    assert "不相关" in result["formatError"]
    assert not m.read("s")["outbox"]
    # The model chooses an ordinary answer; the server never invents a linkage.
    good = deepcopy(bad)
    good["help"].pop("followUp")
    step = m.claim("s", "repair", materials=materials(m))
    provider = TextProvider(json.dumps(good, ensure_ascii=False))
    result = DynamicCoachExecutor(provider).execute(m.read("s"), step)
    request = json.loads(provider.calls[0]["messages"][-1]["content"])
    assert json.loads(request["previousRejectedResponse"]) == bad
    assert "help.followUp.targets" in request["formatError"]
    m.complete("s", step["stepId"], step["token"], result)
    run_step(m, approved)
    state = m.read("s")
    assert state["rounds"][state["currentRoundId"]]["status"] == "finished"
    assert not state["workGoals"] and len(state["outbox"]) == 1


def test_transport_truncation_is_not_accepted_as_a_trailing_brace_typo(tmp_path):
    m = manager(tmp_path, workflow_version=2)
    m.command("s", "ask", "ask", {"text": "怎样读入？"})
    step = m.claim("s", "replay", materials=materials(m))
    provider = TextProvider(json.dumps(compact()) + "}")
    complete = provider.complete
    def truncated(**kwargs):
        reply = complete(**kwargs)
        reply["responseAudit"]["finishReason"] = "length"
        return reply
    provider.complete = truncated
    result = DynamicCoachExecutor(provider).execute(m.read("s"), step)
    assert result["kind"] == "failure" and "responseNormalization" not in result


def test_recovered_syntax_cannot_bypass_evidence_or_full_solution_policy(tmp_path):
    for suffix, change, expected in [
        ("forged", {"understanding": {"question": "q", "finding": "f", "evidenceRefs": ["fabricated"]}}, "INVALID_EVIDENCE_REFERENCE"),
        ("solution", {"scope": "fullSolution"}, "FULL_SOLUTION_NOT_AUTHORIZED"),
    ]:
        part = tmp_path / suffix
        part.mkdir()
        m = manager(part, workflow_version=2)
        m.command("s", "ask", "ask", {"text": "怎样读入？"})
        value = compact()
        if "understanding" in change:
            value["understanding"] = change["understanding"]
        else:
            value["help"]["answerScope"] = change["scope"]
        with pytest.raises(ValueError, match=expected):
            execute_text(m, json.dumps(value) + "}")
        assert not m.read("s")["outbox"]
