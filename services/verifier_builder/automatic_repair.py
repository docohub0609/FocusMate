"""Attempt-scoped generator repair; candidates stay private until all gates pass."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace
import json
import time
from jsonschema.exceptions import ValidationError

from apps.api.deepseek_provider import ProviderResponse, StructuredChatTransport
from apps.api.problem_intake import timestamp
from apps.api.problem_storage import StorageFailure
from focusmate.core.repair_agent import RepairExecutor, RepairValidation, new_repair_record
from .candidates import CandidateFailure, CandidateProvider, CoverageClaim, ROOT, SMALL_CASE_POLICY, _context
from .cross_check import CrossCheckFailure, CheckedCase, PrecheckedMaterial
from .input_repair import InputRepairProvider
from .model_wire import response_schema


PROMPT_VERSION = 'verifier-restricted-generator-repair-v1'
REPAIRABLE = frozenset({'CANDIDATE_SCHEMA_INVALID', 'CANDIDATE_COMPILE_FAILED',
    'GENERATOR_CASES_INVALID', 'GENERATOR_NON_DETERMINISTIC',
    'GENERATOR_COVERAGE_INSUFFICIENT', 'SMALL_REFERENCE_TIMEOUT',
    'REFERENCE_EXECUTION_FAILED', 'GENERATOR_INPUT_LEGALITY_UNCONFIRMED'})


def eligible(error):
    if error.code not in REPAIRABLE:
        return False
    if isinstance(error, CandidateFailure):
        return bool(error.repair_payload and error.repair_payload.get('role') == 'inputGenerator')
    return bool(error.repair_feedback)


def load(jobs, claim):
    with closing(jobs._connect()) as db:
        jobs.guard(claim, db)
        row = db.execute('SELECT record_json FROM verifier_auto_repairs WHERE job_id=? AND attempt=?',
            (claim.job_id, claim.attempt)).fetchone()
    return None if row is None else json.loads(row[0])


def save(jobs, claim, value):
    """The receipt and its one candidate cannot be replaced by another worker."""
    with closing(jobs._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        jobs.guard(claim, db)
        previous = db.execute('SELECT record_json FROM verifier_auto_repairs WHERE job_id=? AND attempt=?',
            (claim.job_id, claim.attempt)).fetchone()
        if previous:
            previous = json.loads(previous[0])
            for key in ('repairId', 'owner', 'materials', 'original', 'failure'):
                if previous.get(key) != value.get(key):
                    raise StorageFailure('REPAIR_RECEIPT_CONFLICT', 'The repair receipt changed its frozen input.', 409, [])
            if previous.get('candidate') is not None and previous['candidate'] != value.get('candidate'):
                raise StorageFailure('REPAIR_CANDIDATE_CONFLICT', 'A repair attempt cannot replace its submitted candidate.', 409, [])
            if value.get('modelCalls',0) < previous.get('modelCalls',0):
                raise StorageFailure('REPAIR_BUDGET_CONFLICT', 'A repair attempt cannot restore spent budget.', 409, [])
            if previous.get('status') in {'succeeded','failed','abstained','exhausted','interrupted'}:
                if previous != value:
                    raise StorageFailure('REPAIR_TERMINAL_CONFLICT', 'A completed repair cannot be overwritten.', 409, [])
                return
        db.execute('''INSERT INTO verifier_auto_repairs VALUES(?,?,?,?)
            ON CONFLICT(job_id,attempt) DO UPDATE SET record_json=excluded.record_json,updated_at=excluded.updated_at''',
            (claim.job_id, claim.attempt, json.dumps(value, ensure_ascii=False, allow_nan=False), timestamp(jobs.clock())))
        accepted = (value.get('validation') or {}).get('result') or {}
        if value.get('status') == 'succeeded' and accepted.get('candidates') and accepted.get('material'):
            # Accept exactly once with the successful receipt; a crash before Worker
            # sealing must not lose the accepted generator or request another repair.
            db.execute('''INSERT INTO verifier_candidate_sets VALUES(?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET candidate_json=excluded.candidate_json,
                prechecked_json=excluded.prechecked_json,updated_at=excluded.updated_at''',
                (claim.job_id,json.dumps(accepted['candidates'],ensure_ascii=False),
                 json.dumps(accepted['material'],ensure_ascii=False),timestamp(jobs.clock())))


def restore_material(value):
    value = deepcopy(value)
    value['cases'] = tuple(CheckedCase(**{**case, 'tags': tuple(case['tags']),
        'stdin': case['stdin'].encode('utf-8'), 'expected_output': case['expected_output'].encode('utf-8')})
        for case in value['cases'])
    from .cross_check import SafeFailureSummary
    value['safe_failure_summaries'] = tuple(SafeFailureSummary(**{**item, 'tags': tuple(item['tags'])})
        for item in value['safe_failure_summaries'])
    value['warning_codes'] = tuple(value['warning_codes'])
    return PrecheckedMaterial(**value)


def run(jobs, claim, problem, features, original, error, *, candidate_provider,
        agent_provider, checker, deadline, active, make_candidates=None, review_provider=None):
    """One candidate plus one independent legality review, within the old deadline."""
    if not eligible(error):
        raise error
    context = _context(problem, features)
    schema = response_schema(json.loads((ROOT / 'schemas/execution/verifier-input-generator-candidate.schema.json')
        .read_text(encoding='utf-8')), context, 'inputGenerator')
    original_artifact = ({'source': original.generator.source, 'coveragePlan': [
        {'tag': item.tag, 'featureKeys': list(item.feature_keys)} for item in original.coverage_plan]}
        if original is not None else error.repair_payload['originalResponse'])
    feedback = (error.repair_payload if isinstance(error, CandidateFailure) else error.repair_feedback)
    receipt = load(jobs, claim)
    if receipt is None:
        receipt = new_repair_record(repair_id=f'{claim.job_id}:attempt:{claim.attempt}',
            owner={'kind':'verifierBuild', 'taskId':claim.job_id, 'attempt':claim.attempt,
                'goalId':None, 'subgoalIds':[], 'problemRef':context['problemRef'],
                'problemContentHash':context['problemContentHash'], 'featureSetId':context['featureSetId']},
            failure={'code':error.code, 'reason':error.message}, original=original_artifact,
            materials={'problem':context, 'generator':original_artifact,
                'failure':feedback, 'smallCasePolicy':SMALL_CASE_POLICY,
                'instructions':('Repair only generator source and coveragePlan. Preserve legal inputs and effective coverage. '
                    'Never modify references, public examples, problem requirements or validation rules. '
                    'Small generated inputs must fit the independent reference execution budget. '
                    'Exit 137 alone does not prove timeout or out-of-memory. You may report inability to repair.')},
            review_calls=1)
        save(jobs, claim, receipt)
    if review_provider is None:
        transport = StructuredChatTransport(candidate_provider.transport.config, attempts=1)
        review_provider = InputRepairProvider(CandidateProvider(transport))

    def validate(candidate, budget):
        active()
        reviewed_result = {}
        try:
            try:
                parsed, document = candidate_provider._parse_role('inputGenerator',
                    ProviderResponse('repair-candidate', 'repair-agent', json.dumps(candidate)), context)
            except (ValidationError, ValueError, UnicodeError, RecursionError):
                return RepairValidation(False, {'code':'GENERATOR_REPAIR_INVALID'}, 'GENERATOR_REPAIR_INVALID')
            if make_candidates:
                candidates = make_candidates(parsed, document)
            else:
                candidates = replace(original, generator=parsed, coverage_plan=tuple(
                    CoverageClaim(item['tag'], tuple(item['featureKeys'])) for item in document['coveragePlan']))
            candidates = replace(candidates, generator=replace(candidates.generator, prompt_version=PROMPT_VERSION))
            def review(cases):
                active()
                budget.model_call('inputLegalityReview')
                review_started = time.monotonic()
                try:
                    reviewed = review_provider.review_cases(problem, features, cases, deadline)
                    budget.record['calls'][-1]['status'] = 'returned'
                except Exception:
                    budget.record['calls'][-1]['status'] = 'failed'
                    raise
                finally:
                    budget.record['calls'][-1]['elapsedMs'] = round((time.monotonic()-review_started)*1000)
                    budget.persist()
                # Independently recheck coverage/hash: review providers cannot omit selected cases.
                from .repair_storage import validate_review
                validate_review(reviewed, cases)
                active()
                reviewed_result.update(reviewed)
            checker.case_review = review
            checked = checker.precheck(problem, features, candidates, deadline)
            active()
            candidates = replace(candidates, mutants=tuple(checker.latest_mutants))
            from .worker import _checkpoint_material, VerifierBuilderWorker
            VerifierBuilderWorker._validate_checked(claim,candidates,checked)
            return RepairValidation(True, {'candidates':asdict(candidates),
                'material':_checkpoint_material(checked), 'inputReview':reviewed_result})
        except (CandidateFailure, CrossCheckFailure) as failed:
            return RepairValidation(False, {'code':failed.code}, failed.code)
        finally:
            checker.case_review = None

    receipt = RepairExecutor(agent_provider).execute(receipt, candidate_schema=schema,
        validate=validate, checkpoint=lambda value:save(jobs,claim,value), guard=active, deadline=deadline)
    if receipt['status'] != 'succeeded':
        validation = receipt.get('validation') or {}
        failure_code = (validation.get('result') or {}).get('code')
        if failure_code:
            raise CrossCheckFailure(failure_code)
        raise CandidateFailure('GENERATOR_REPAIR_INVALID', 'The automatic generator repair did not pass its checks.', True)
    from .worker import _restore_candidates
    accepted = receipt['validation']['result']
    return _restore_candidates(accepted['candidates']), restore_material(accepted['material'])


def restored_review(jobs, claim, candidates):
    """A saved legality decision is usable only for the identical frozen inputs."""
    if candidates.generator.prompt_version != PROMPT_VERSION:
        return None
    with closing(jobs._connect()) as db:
        jobs.guard(claim, db)
        rows = db.execute('SELECT record_json FROM verifier_auto_repairs WHERE job_id=? ORDER BY attempt DESC',
            (claim.job_id,)).fetchall()
    expected = asdict(candidates)
    from .repair_storage import _review_subject, validate_review
    for row in rows:
        receipt = json.loads(row[0])
        value = (receipt.get('validation') or {}).get('result') or {}
        if (receipt.get('status') == 'succeeded' and value.get('candidates')
                and _review_subject(value['candidates']) == _review_subject(expected)):
            review = value.get('inputReview')
            if review:
                return lambda cases:validate_review(review, cases)
    raise CandidateFailure('GENERATOR_INPUT_LEGALITY_UNCONFIRMED',
        'The saved repaired generator has no matching verified input review.', True)
