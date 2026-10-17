"""List static Skill metadata without executing a Skill or contacting a model."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from focusmate.core.skill_registry import SkillRegistry


def main():
    parser = argparse.ArgumentParser(description='扫描并列出有效 Skill 元数据。')
    parser.add_argument('roots', nargs='*', help='包含 Skill 直接子目录的扫描根目录')
    parser.add_argument('--capability', help='精确匹配能力标签')
    parser.add_argument('--applies-to', help='精确匹配适用标签')
    parser.add_argument('--json', action='store_true', help='输出 JSON')
    args = parser.parse_args()
    registry = SkillRegistry()
    report = registry.refresh(args.roots or None)
    report['skills'] = registry.list(args.capability, args.applies_to)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print('刷新成功。' if report['refreshSucceeded'] else '刷新失败，保留原列表。')
        for entry in report['skills']:
            print(f"{entry['skillId']} v{entry['version']}：{entry['description']}")
        for issue in report['issues']:
            print(f"{issue['code']} {issue.get('source', issue['path'])}：{issue['message']}")
        print(f"共 {len(report['skills'])} 个有效版本。")
    return 0 if report['allValid'] and report['refreshSucceeded'] else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
