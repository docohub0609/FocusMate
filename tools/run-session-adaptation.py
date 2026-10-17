"""Interpret user feedback and verified links, then update a local session snapshot."""
import argparse
import json
from pathlib import Path
import sys
from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT/'src'))
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.skill_execution import DeepSeekSkillExecutionProvider
from focusmate.contracts.skill_files import strict_json
from focusmate.core.session_adaptation_flow import run_adaptation


def save(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request',type=Path)
    parser.add_argument('--previous',type=Path)
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args(argv)
    args.output_dir.mkdir(parents=True,exist_ok=False)
    count=0
    def sink(row):
        nonlocal count
        count+=1; save(args.output_dir/f'step-{count:03d}.json',row)
    class LazyProvider:
        instance=None
        def complete(self, **kwargs):
            if self.instance is None:
                if args.env_file: load_local_env(args.env_file)
                self.instance=DeepSeekSkillExecutionProvider.from_environment()
                save(args.output_dir/'model-config.json',self.instance.public_config)
            return self.instance.complete(**kwargs)
    try:
        bundle=strict_json(args.request.read_text(encoding='utf-8-sig'))
        previous=strict_json(args.previous.read_text(encoding='utf-8-sig')) if args.previous else None
        report=run_adaptation(bundle,LazyProvider(),previous=previous,audit_sink=sink)
    except (OSError,ValueError,KeyError,TypeError,ValidationError):
        save(args.output_dir/'failure.json',{'status':'rejected','reason':'INVALID_ENVELOPE_OR_SNAPSHOT'})
        print('输入或上一份快照不合法；未生成新的调整结果。')
        return 1
    save(args.output_dir/'snapshot.json',report)
    print(json.dumps({'contributions':report['ledger']['contributions'],
        'adjustments':report['ledger']['adjustments']},ensure_ascii=False))
    return 1 if any(r['status']=='rejected' for r in report['steps']) else 0


if __name__=='__main__':
    raise SystemExit(main())
