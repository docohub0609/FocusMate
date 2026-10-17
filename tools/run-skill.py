"""Run one pinned Skill without business state writes; preserve visible audit files."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.skill_execution import DeepSeekSkillExecutionProvider
from focusmate.contracts.skill_files import strict_json
from focusmate.core.skill_execution import execute_skill


def _write(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


def main(argv=None, *, provider_factory=DeepSeekSkillExecutionProvider.from_environment):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--skills-root', type=Path, default=ROOT / 'skills')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--context-window-tokens', type=int, default=65536)
    parser.add_argument('--review-context-window-tokens', type=int, default=131072)
    parser.add_argument('--reserved-output-tokens', type=int, default=16384)
    parser.add_argument('--safety-margin-tokens', type=int, default=4096)
    parser.add_argument('--database', type=Path, help='只读获取已有 Goal 的代码快照。')
    parser.add_argument('--code-version', type=int)
    args = parser.parse_args(argv)
    if args.reserved_output_tokens < 16384:
        parser.error('当前模型配置最多输出 16384 tokens，预留不能小于此值。')
    args.output_dir.mkdir(parents=True, exist_ok=False)
    stage_count = 0
    request_count = 0
    context_count = 0
    def save_stage(value):
        nonlocal stage_count, request_count, context_count
        if value.get('kind') == 'context':
            context_count += 1
            _write(args.output_dir / f'context-{context_count:02d}.json', value)
            return
        if value.get('kind') == 'request':
            request_count += 1
            _write(args.output_dir / f'request-{request_count:02d}.json', value)
            return
        stage_count += 1
        _write(args.output_dir / f'stage-{stage_count:02d}-{value["stage"]}.json', value)
    try:
        request = strict_json(args.request.read_text(encoding='utf-8-sig'))
        if bool(args.database) != (args.code_version is not None):
            raise ValueError('database and code-version must be paired')
        if args.database:
            from apps.api.context_storage import load_current_code
            record = load_current_code(args.database, request['goalContext']['goalId'], args.code_version)
            request['inputs']['code'] = record['content']
    except Exception:
        report = {'schemaVersion': '1.0.0', 'status': 'rejected', 'trail': [],
                  'failure': {'stage': 'input', 'reason': 'INVALID_INPUT'}}
    else:
        # Preflight through the executor before initializing any configured transport.
        class LazyProvider:
            instance = None
            def complete(self, **kwargs):
                if self.instance is None:
                    if args.env_file:
                        load_local_env(args.env_file)
                    self.instance = provider_factory()
                    _write(args.output_dir / 'config.json', getattr(self.instance, 'public_config', {'provider': 'injected'}))
                return self.instance.complete(**kwargs)
        lazy = LazyProvider()
        report = execute_skill(request, lazy, skills_root=args.skills_root, audit_sink=save_stage,
            context_options={'budget': {'contextWindowTokens': args.context_window_tokens,
                'reservedOutputTokens': args.reserved_output_tokens,
                'safetyMarginTokens': args.safety_margin_tokens},
                'reviewBudget': {'contextWindowTokens': args.review_context_window_tokens,
                'reservedOutputTokens': args.reserved_output_tokens,
                'safetyMarginTokens': args.safety_margin_tokens}})
        # Only the schema-approved request is persisted: invalid envelopes might contain secrets.
        if report.get('failure', {}).get('stage') != 'input':
            _write(args.output_dir / 'request.json', request)
    _write(args.output_dir / 'report.json', report)
    print(str(args.output_dir / 'report.json'))
    return 0 if report['status'] == 'accepted' else 1


if __name__ == '__main__':
    raise SystemExit(main())
