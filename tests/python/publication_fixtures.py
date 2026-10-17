"""Synthetic review inputs for fake-provider tests, not real approval records."""
import hashlib
import json
from pathlib import Path
from tools.skill_evaluation import digest
from tools.skill_evaluation_cases import build_cases

def audit_args(root, *, approval=False, reviewer=False):
    base = Path(__file__).resolve().parents[2]
    audit = {'reviewer': 'AI-independent', 'testOnly': True,
        'reviewedSourceSha256': hashlib.sha256((base/'tools/skill_evaluation_cases.py').read_text(encoding='utf-8').encode()).hexdigest(),
        'cases': [{'id': c['id'], 'status': 'accepted', 'reason': 'Simulated review for fake transport test only'} for c in build_cases()]}
    path = root/'mock-case-audit.json'; path.write_text(json.dumps(audit), encoding='utf-8')
    args = ['--case-audit', str(path)]
    if approval:
        path = root/'mock-approval.json'; path.write_text(json.dumps({'representativeScaleApproved': True, 'testOnly': True}), encoding='utf-8')
        args += ['--approval', str(path)]
    if reviewer:
        fixtures = json.loads((base/'tools/fixtures/skill-reviewer-cases.json').read_text(encoding='utf-8'))
        record = {'reviewer': 'AI-independent', 'testOnly': True, 'fixturesHash': digest(fixtures),
            'cases': [{'id': c['id'], 'status': 'accepted', 'reason': 'Simulated review only'} for c in fixtures]}
        path = root/'mock-fixture-audit.json'; path.write_text(json.dumps(record), encoding='utf-8')
        args += ['--fixture-audit', str(path)]
    return args
