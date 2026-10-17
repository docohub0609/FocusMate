"""Private input repair receipts, fenced by the owning build claim."""
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
from apps.api.problem_intake import timestamp
from .candidates import CandidateFailure
from .input_repair import REPAIR_PROMPT_VERSION


def _review_subject(candidate):
    # Mutation checking may update mutants after input review. The frozen problem,
    # generator, coverage plan, and both independent references must still match.
    return json.dumps({key: value for key, value in candidate.items() if key != 'mutants'},
                      sort_keys=True, separators=(',', ':'))


def restore_review(jobs, claim, candidates):
    """Carry a repaired generator's review obligation into an authorized new claim.

    Never reuse the old verdict: the newly executed generator's selected inputs
    must be reviewed in this claim, even if the prior claim saved a legal verdict.
    """
    candidate = asdict(candidates)
    subject = _review_subject(candidate)
    with closing(jobs._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        jobs.guard(claim, db)
        rows = db.execute('''SELECT * FROM verifier_case_repairs
            WHERE job_id=? AND repaired_json IS NOT NULL ORDER BY updated_at DESC''',
            (claim.job_id,)).fetchall()
        previous = next((row for row in rows
            if _review_subject(json.loads(row['repaired_json'])) == subject), None)
        if previous is None:
            if candidates.generator.prompt_version == REPAIR_PROMPT_VERSION:
                raise CandidateFailure('GENERATOR_INPUT_LEGALITY_UNCONFIRMED',
                    'The repaired generator has no matching persisted review context.', True)
            return False
        changed = db.execute('''INSERT OR IGNORE INTO verifier_case_repairs
            (job_id,claim_token,status,feedback_json,original_json,repaired_json,updated_at)
            VALUES(?,?,'generated',?,?,?,?)''',
            (claim.job_id, claim.claim_token, previous['feedback_json'], previous['original_json'],
             json.dumps(candidate), timestamp(jobs.clock()))).rowcount
        jobs._require_owned_change(changed)
        return True


def reserve(jobs, claim, feedback, candidates):
    with closing(jobs._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        jobs.guard(claim, db)
        return db.execute('''INSERT OR IGNORE INTO verifier_case_repairs
            (job_id,claim_token,status,feedback_json,original_json,updated_at)
            VALUES(?,?,'requested',?,?,?)''',
            (claim.job_id,claim.claim_token,json.dumps(feedback),json.dumps(asdict(candidates)),
             timestamp(jobs.clock()))).rowcount == 1


def save_generator(jobs, claim, candidates):
    with closing(jobs._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        jobs.guard(claim, db)
        changed=db.execute('''UPDATE verifier_case_repairs SET status='generated',
            repaired_json=?,updated_at=? WHERE job_id=? AND claim_token=? AND status='requested' ''',
            (json.dumps(asdict(candidates)),timestamp(jobs.clock()),claim.job_id,claim.claim_token)).rowcount
        jobs._require_owned_change(changed)
        db.execute('''UPDATE verifier_candidate_sets SET candidate_json=?,prechecked_json=NULL,
            updated_at=? WHERE job_id=?''',
            (json.dumps(asdict(candidates)),timestamp(jobs.clock()),claim.job_id))


def validate_review(review, cases):
    inputs = [{'caseKey': case.case_key, 'stdin': case.stdin.decode('utf-8')} for case in cases]
    digest = 'sha256:' + hashlib.sha256(json.dumps(inputs, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    reviewed = review.get('cases', [])
    if (review.get('caseSetHash') != digest or len(reviewed) != len(inputs)
            or {item['caseKey'] for item in reviewed} != {item['caseKey'] for item in inputs}
            or any(item['status'] != 'legal' for item in reviewed)):
        raise CandidateFailure('GENERATOR_REPAIR_INVALID',
            'The input review does not match all selected inputs.', True)
    return review


def save_review(jobs, claim, review, cases):
    with closing(jobs._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        jobs.guard(claim, db)
        validate_review(review, cases)
        changed=db.execute('''UPDATE verifier_case_repairs SET status='checked',review_json=?,
            updated_at=? WHERE job_id=? AND claim_token=? AND status='generated' ''',
            (json.dumps(review),timestamp(jobs.clock()),claim.job_id,claim.claim_token)).rowcount
        jobs._require_owned_change(changed)
