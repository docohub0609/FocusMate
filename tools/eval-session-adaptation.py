"""Run real interpretation on explicitly simulated cases, keeping expected labels out."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.skill_execution import DeepSeekSkillExecutionProvider
from focusmate.contracts.skill_files import strict_json
from focusmate.core.adaptation_interpretation import interpret_effect, digest, PROMPT_VERSION


def write_new(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def model_input(case):
    return {key: case[key] for key in ('kind', 'sourceRef', 'materials', 'actions', 'verifierResult') if key in case}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    cases = strict_json(args.cases.read_text(encoding='utf-8-sig'))['cases']
    args.output_dir.mkdir(parents=True, exist_ok=False)
    if args.env_file:
        load_local_env(args.env_file)
    provider = DeepSeekSkillExecutionProvider.from_environment()
    write_new(args.output_dir / 'manifest.json', {'schemaVersion': '1.0.0',
        'createdAt': datetime.now(timezone.utc).isoformat(), 'simulatedMaterials': True,
        'casesHash': digest(cases), 'cases': cases, 'promptVersion': PROMPT_VERSION,
        'modelConfig': provider.public_config, 'repetitions': 1,
        'meaning': 'Real model calls on authored cases; not actual users or executed verifier checks.'})
    def run(pair):
        index, case = pair
        started = time.monotonic()
        report = interpret_effect(model_input(case), provider)
        result = report.get('interpretation', {})
        row = {'caseId': case['caseId'], 'report': report,
            'elapsedSeconds': round(time.monotonic() - started, 3),
            'expected': case['expected'], 'matchesExpected': report['status'] == 'interpreted'
            and all(result.get(k) == v for k, v in case['expected'].items())}
        write_new(args.output_dir / f'record-{index:03d}.json', row)
        print(f"{case['caseId']}: {report['status']} matches={row['matchesExpected']}", flush=True)
        return row
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(run, enumerate(cases, 1)))
    summary = {'cases': len(rows), 'matchesExpected': sum(r['matchesExpected'] for r in rows),
        'notInterpreted': sum(r['report']['status'] != 'interpreted' for r in rows),
        'simulatedMaterials': True, 'humanReviewed': False,
        'limitation': 'Label matches do not establish that every explanatory claim is correct.'}
    write_new(args.output_dir / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary['matchesExpected'] == len(rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
