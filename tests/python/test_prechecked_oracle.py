from dataclasses import replace

import pytest

from apps.api.trusted_assets import OracleEvaluationFailure
from services.verifier_builder import worker as worker_module
from services.verifier_builder.cross_check import CheckedCase, PrecheckedMaterial, content_hash


SOURCE = "# SECRET-source\nprint(input())\n"


def material() -> PrecheckedMaterial:
    cases = (
        CheckedCase("case-a", "small", ("minimumSize",), b"SECRET-input-a\n", b"SECRET-output-a\n"),
        CheckedCase("case-b", "small", ("randomSmall",), b"SECRET-input-b\n", b"SECRET-output-b\n"),
    )
    return PrecheckedMaterial(
        problem_id="problem-submit",
        problem_version=1,
        problem_content_hash="sha256:" + "1" * 64,
        feature_set_id="features-1",
        cases=cases,
        fast_reference_source=SOURCE,
        seed=123,
        generator_hash="sha256:" + "2" * 64,
        slow_oracle_hash="sha256:" + "3" * 64,
        fast_reference_hash=content_hash(SOURCE.encode("utf-8")),
        small_count=2,
        large_count=0,
        agreement_count=2,
        mutation_attempted=1,
        mutation_eligible=1,
        mutation_killed=1,
        elapsed_ms=1,
        safe_failure_summaries=(),
    )


def test_prechecked_oracle_returns_exact_immutable_outputs_once() -> None:
    checked = material()
    evaluator = worker_module._FreshPrecheckedOracleEvaluator(checked)

    outputs = evaluator.evaluate(SOURCE, tuple(case.stdin for case in checked.cases))

    assert type(outputs) is tuple
    assert all(type(output) is bytes for output in outputs)
    assert outputs == tuple(case.expected_output for case in checked.cases)
    with pytest.raises(OracleEvaluationFailure) as caught:
        evaluator.evaluate(SOURCE, tuple(case.stdin for case in checked.cases))
    assert caught.value.code == "ORACLE_PRECHECK_REUSE_ALREADY_USED"
    assert "SECRET" not in caught.value.message


@pytest.mark.parametrize("damage", ["sourceBytes", "sourceHash", "inputBytes", "inputOrder", "inputCount"])
def test_prechecked_oracle_rejects_any_source_or_ordered_input_mismatch(damage: str) -> None:
    checked = material()
    source = SOURCE
    inputs = tuple(case.stdin for case in checked.cases)
    if damage == "sourceBytes":
        source += "# changed\n"
    elif damage == "sourceHash":
        checked = replace(checked, fast_reference_hash="sha256:" + "0" * 64)
    elif damage == "inputBytes":
        inputs = (b"SECRET-altered\n", inputs[1])
    elif damage == "inputOrder":
        inputs = tuple(reversed(inputs))
    else:
        inputs = inputs[:-1]

    evaluator = worker_module._FreshPrecheckedOracleEvaluator(checked)

    with pytest.raises(OracleEvaluationFailure) as caught:
        evaluator.evaluate(source, inputs)
    assert caught.value.code == "ORACLE_PRECHECK_REUSE_MISMATCH"
    assert "SECRET" not in caught.value.message
