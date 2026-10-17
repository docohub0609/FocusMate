"""Qualify an independently generated challenge before measuring test detection."""
from dataclasses import asdict
from pathlib import Path
import tempfile

from services.oj_worker.judge import outputs_equal
from .cross_check import CrossCheckFailure


def qualify_mutations(checker, cleanup, record, features, public, slow, fast, deadline):
    provider = checker.mutation_provider
    feedback = None
    for attempt in range(2 if checker.allow_mutation_repair else 1):
        checker.mutation_repair_count = attempt
        checker.stage = 'mutationGeneration' if not attempt else 'mutationRepair'
        checker._budget(deadline)
        mutants = provider.generate(record, features, deadline, feedback=feedback)
        checker._budget(deadline)
        checker.latest_mutants = mutants
        audit = {'attempt': attempt, 'candidates': [asdict(m) for m in mutants],
                 'publicFailures': [], 'witnessChecks': []}
        checker.mutation_audit.append(audit)
        public_passed = []
        for mutant in mutants:
            workspace = Path(cleanup.enter_context(tempfile.TemporaryDirectory(
                prefix='focusmate-mutation-', dir=checker.workspace_root)))
            (workspace / 'main.py').write_text(mutant.source, encoding='utf-8')
            checker.stage = 'mutationQualification'
            if not checker._successful(checker._call(workspace, b'', deadline, operation='compile')):
                audit['publicFailures'].append({'mutantKey': mutant.mutant_key, 'status': 'compileFailed'})
                continue
            for index, (stdin, expected) in enumerate(public):
                actual = checker._call(workspace, stdin, deadline)
                if not checker._successful(actual) or not outputs_equal(actual.stdout, expected):
                    audit['publicFailures'].append({'mutantKey': mutant.mutant_key,
                        'exampleIndex': index, 'stdin': stdin.decode('utf-8'),
                        'expectedOutput': expected.decode('utf-8'),
                        'actualOutput': actual.stdout[:2048].decode('utf-8', errors='replace'),
                        'status': 'publicMismatch' if checker._successful(actual) else 'publicExecutionFailed'})
                    break
            else:
                public_passed.append((mutant, workspace))
        decisions = {}
        if public_passed:
            checker.stage = 'mutationWitnessReview'
            decisions = provider.assess_witnesses(record, features, tuple(m for m, _ in public_passed), deadline)
            checker._budget(deadline)
            audit['witnessReviews'] = list(provider.last_witness_reviews)
        qualified = []
        for mutant, workspace in public_passed:
            check = {'mutantKey': mutant.mutant_key, 'legality': decisions.get(mutant.mutant_key, 'unknown')}
            audit['witnessChecks'].append(check)
            if check['legality'] != 'legal' or not mutant.witness_input:
                audit['publicFailures'].append({'mutantKey': mutant.mutant_key, 'status': 'witnessLegalityUnconfirmed'})
                continue
            checker.stage = 'mutationWitnessExecution'
            stdin = mutant.witness_input.encode('utf-8')
            expected = checker._output(slow, stdin, deadline, 'REFERENCE_EXECUTION_FAILED')
            other = checker._output(fast, stdin, deadline, 'REFERENCE_EXECUTION_FAILED')
            if not outputs_equal(expected, other):
                raise CrossCheckFailure('REFERENCE_DISAGREEMENT')
            actual = checker._call(workspace, stdin, deadline)
            if checker._successful(actual) and outputs_equal(actual.stdout, expected):
                check['result'] = 'defectNotReproduced'
                audit['publicFailures'].append({'mutantKey': mutant.mutant_key, 'status': 'witnessDoesNotExposeDefect'})
                continue
            check['result'] = 'defectReproduced'
            qualified.append(workspace)
        if qualified:
            return qualified, attempt
        # Only public failures and the generator's own candidates return to it.
        # Private generated cases and reference source never enter this request.
        feedback = {'priorMutants': [{'mutantKey': m.mutant_key, 'defectTag': m.defect_tag,
            'source': m.source, 'witnessInput': m.witness_input,
            'defectDescription': m.defect_description, 'legalityReason': m.legality_reason}
            for m in mutants], 'publicFailures': audit['publicFailures']}
    if not checker.allow_mutation_repair:
        # No qualified challenge is limited evidence, not a detected test gap.
        # The new protocol records zero repairs honestly; it cannot complete Goal.
        return [], 0
    return [], 1
