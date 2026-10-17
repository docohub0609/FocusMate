"""Read pinned business evidence and save one unverified progress estimate."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.progress_estimation import DeepSeekProgressEstimationProvider
from focusmate.adapters.progress_storage import SQLiteProgressResolver
from focusmate.contracts.skill_files import strict_json
from focusmate.contracts.skill_execution import ProviderError
from focusmate.core.progress_estimation import estimate_progress


def save(path,value):
    with Path(path).open('x',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.write('\n');stream.flush();os.fsync(stream.fileno())


def main(argv=None,*,provider_factory=DeepSeekProgressEstimationProvider.from_environment):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--request',type=Path,required=True)
    parser.add_argument('--public-task-db',type=Path)
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args(argv)
    try:args.output_dir.mkdir(parents=True,exist_ok=False)
    except FileExistsError:
        print('结果目录已存在，未读取或覆盖历史记录。');return 1
    try:
        supplied=strict_json(args.request.read_text(encoding='utf-8-sig'))
        request=supplied['request'];records=supplied.get('supplementalRecords',[])
        if not isinstance(request,dict) or not isinstance(records,list):raise ValueError('input shape')
    except (OSError,ValueError,KeyError,TypeError):request={};records=[]
    # Credentials are loaded only after input and budget checks. Invalid input is not copied to disk.
    class LazyProvider:
        def complete(self,**kwargs):
            if args.env_file:load_local_env(args.env_file)
            provider=provider_factory()
            config={k:v for k,v in provider.public_config.items() if k in
                ('provider','model','maxTokens','thinkingEnabled','reasoningEffort','timeoutSeconds','networkAttempts')}
            save(args.output_dir/'config.json',config)
            if config.get('maxTokens',0)>request['budget']['reservedOutputTokens']:raise ProviderError('OUTPUT_BUDGET_TOO_SMALL')
            return provider.complete(**kwargs)
    with SQLiteProgressResolver(args.db,request,public_task_db=args.public_task_db,supplemental_records=records) as resolver:
        report=estimate_progress(request,resolver,LazyProvider())
    save(args.output_dir/'report.json',report)
    print('判断已保存，尚未经验证器确认。' if report['status']=='estimated' else '本次判断未完成：'+report['failure']['reason'])
    print(args.output_dir/'report.json')
    return 0 if report['status']=='estimated' else 1


if __name__=='__main__':raise SystemExit(main())
