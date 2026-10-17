"""Inspect event receipts or repair public-run event delivery; no execution."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from focusmate.adapters.raw_events import SQLiteEventStore
from focusmate.adapters.public_run_events import backfill_public_run_events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    sub = parser.add_subparsers(dest='operation', required=True)
    query = sub.add_parser('list')
    query.add_argument('--goal', required=True)
    query.add_argument('--after', type=int, default=0)
    query.add_argument('--limit', type=int, default=100)
    repair = sub.add_parser('backfill-public')
    repair.add_argument('--task-database', type=Path, required=True)
    if not (args := parser.parse_args()).database.is_file():
        parser.error('business database does not exist')
    if args.operation == 'list':
        result = SQLiteEventStore(args.database).list_goal(args.goal, after_sequence=args.after, limit=args.limit)
    else:
        result = backfill_public_run_events(args.database, args.task_database)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
