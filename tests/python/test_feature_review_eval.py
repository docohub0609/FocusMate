import importlib.util
import json
from pathlib import Path
from apps.api.deepseek_provider import ProviderResponse

ROOT = Path(__file__).resolve().parents[2]


def test_labelled_evaluation_counts_disagreement_instead_of_claiming_all_generated_pass():
    spec = importlib.util.spec_from_file_location('feature_eval', ROOT / 'tools/eval-feature-review.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = json.loads((ROOT / 'examples/problem-feature-review/cases.json').read_text(encoding='utf-8'))
    class AlwaysSupports:
        def review(self, problem, *, features, evidence_catalog):
            return ProviderResponse('fake-response','fake-review',json.dumps({'items':[
                {'itemId':f['itemId'],'verdict':'supported','issue':'none','evidenceRefs':f['evidenceRefs']}
                for f in features
            ]}))
    result = module.evaluate_cases(cases, AlwaysSupports())
    assert result['caseCount'] == 8
    assert result['matched'] == 4
    assert result['unavailable'] == 0
    assert result['falseSupported'] == 4
