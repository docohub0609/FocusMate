"""Recognize one pinned code snapshot's stage and save an independent audit directory."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.stage_recognition import DeepSeekStageRecognitionProvider
from focusmate.contracts.skill_files import strict_json
from focusmate.core.context_builder import MapContextResolver
from focusmate.core.stage_recognition import recognize_stage, render_stage_report


def _write(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


def _public_config(provider):
    value = getattr(provider, 'public_config', {'provider': 'injected'})
    allowed = {'provider', 'model', 'maxTokens', 'thinkingEnabled', 'reasoningEffort',
               'timeoutSeconds', 'deadlineSeconds', 'networkAttempts'}
    return {key: value[key] for key in allowed if key in value}


def main(argv=None, *, provider_factory=DeepSeekStageRecognitionProvider.from_environment):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--env-file', type=Path)
    args = parser.parse_args(argv)
    if args.output_dir.exists():
        print('结果目录已存在，未发送请求。'); return 1
    args.output_dir.mkdir(parents=True, exist_ok=False)
    try:
        supplied = strict_json(args.input.read_text(encoding='utf-8-sig'))
        request = supplied['request']; records = supplied['records']
        if not isinstance(request, dict) or not isinstance(records, list): raise ValueError('shape')
    except Exception:
        supplied = {}
        request = {}
        records = []
    _write(args.output_dir / 'input.json', supplied)
    provider_holder = {'value': None}
    class LazyProvider:
        def complete(self, **kwargs):
            if provider_holder['value'] is None:
                if args.env_file: load_local_env(args.env_file)
                provider_holder['value'] = provider_factory()
                _write(args.output_dir / 'config.json', _public_config(provider_holder['value']))
                maximum = getattr(provider_holder['value'], 'public_config', {}).get('maxTokens')
                if type(maximum) is int and request.get('budget', {}).get('reservedOutputTokens', 0) < maximum:
                    from focusmate.contracts.skill_execution import ProviderError
                    raise ProviderError('OUTPUT_BUDGET_TOO_SMALL')
            return provider_holder['value'].complete(**kwargs)
    def audit(value):
        if value['kind'] == 'context': _write(args.output_dir / 'context.json', value['snapshot'])
        elif value['kind'] == 'request': _write(args.output_dir / 'request.json', {k:v for k,v in value.items() if k != 'kind'})
        elif value['kind'] == 'response': _write(args.output_dir / 'response.json', {k:v for k,v in value.items() if k != 'kind'})
    try: resolver = MapContextResolver(records)
    except Exception: resolver = MapContextResolver([])
    report = recognize_stage(request, resolver, LazyProvider(), audit_sink=audit)
    _write(args.output_dir / 'report.json', report)
    _write(args.output_dir / 'display.json', {'text': render_stage_report(report)})
    print(render_stage_report(report))
    print(str(args.output_dir / 'report.json'))
    return 0 if report['status'] == 'recognized' else 1


if __name__ == '__main__':
    raise SystemExit(main())
