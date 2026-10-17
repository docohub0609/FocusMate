"""Read-only authoring check; this command never activates a Skill."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from focusmate.core.skill_package import validate_skill_package


def main():
    parser = argparse.ArgumentParser(description='离线检查 Skill 包格式、引用和内容指纹。')
    parser.add_argument('directory')
    parser.add_argument('--json', action='store_true', help='输出结构化检查报告')
    args = parser.parse_args()
    report = validate_skill_package(args.directory)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print('Skill 包格式检查通过。' if report['packageValid'] else 'Skill 包格式检查未通过。')
        for issue in report['issues']:
            print(f"{issue['path']}：{issue['message']}（{issue['code']}）")
        if report['computedHash']:
            print('计算出的内容指纹：' + report['computedHash'])
        print('未审核正文含义或辅导效果，未检查工具可用性，也未执行 Skill。')
    return 0 if report['packageValid'] else 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
