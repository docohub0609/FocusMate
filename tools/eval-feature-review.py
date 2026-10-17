"""Run a small labelled semantic-review evaluation; no business database is touched."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.api.deepseek_provider import DeepSeekProvider, ProviderFailure, load_local_env
from apps.api.problem_evidence import build_evidence_catalog
from apps.api.problem_feature_review import review_problem_features
from apps.api.problem_intake import create_problem_package


def evaluate_cases(cases, provider):
    results = []
    for case in cases:
        started = time.monotonic()
        problem = create_problem_package(case['problem'], id_factory=lambda: 'review-eval-' + case['name'])
        catalog = {item['evidenceId']: item for item in build_evidence_catalog(problem)}
        feature = deepcopy(case['candidate'])
        feature['evidence'] = [catalog[ref] for ref in feature.pop('evidenceRefs')]
        report = review_problem_features(problem, [feature], provider,
                                        created_at=datetime.now(timezone.utc).isoformat())
        actual = report['items'][0]['verdict'] if report['status'] == 'completed' else 'unavailable'
        results.append({'name':case['name'], 'expected':case['expected'], 'actual':actual,
                        'matched':actual == case['expected'], 'review':report,
                        'elapsedSeconds':round(time.monotonic()-started, 2)})
        print(f"{case['name']}: expected={case['expected']}, actual={actual}", flush=True)
    return {'caseCount':len(cases), 'matched':sum(row['matched'] for row in results),
            'unavailable':sum(row['actual']=='unavailable' for row in results),
            'falseSupported':sum(row['actual']=='supported' and row['expected']!='supported' for row in results),
            'labelSource':'developer-authored-regression-cases',
            'scope':'Small known-case check, not a general semantic-accuracy benchmark.',
            'casesHash':hashlib.sha256(json.dumps(cases,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
            'results':results}


def main(argv=None):
    parser=argparse.ArgumentParser(description='真实模型核对八个已标注案例，记录命中和误报。')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--env-file',type=Path)
    args=parser.parse_args(argv)
    if args.output.exists():
        print('输出文件已存在，请使用新路径以保留历史。')
        return 1
    try:
        if args.env_file: load_local_env(args.env_file)
        cases=json.loads((ROOT/'examples/problem-feature-review/cases.json').read_text(encoding='utf-8'))
        result=evaluate_cases(cases,DeepSeekProvider.from_environment())
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as target:
            json.dump(result,target,ensure_ascii=False,indent=2)
            target.write('\n')
        print(f"核对一致 {result['matched']}/{result['caseCount']}，审核未完成 {result['unavailable']}，错误支持 {result['falseSupported']}。")
        return 0 if result['matched']==result['caseCount'] else 1
    except (OSError, ValueError, ProviderFailure):
        print('评测未完成：请检查配置、输入或输出路径。')
        return 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
