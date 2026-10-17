"""Explicitly create a policy revision draft; never change a frozen row in place."""
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from apps.api.goal_contract_storage import GoalContractRepository
from focusmate.core.action_planning import REQUEST_POLICY


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path,required=True);p.add_argument('--goal-id',required=True)
    p.add_argument('--based-on-version',type=int,required=True);p.add_argument('--reason',required=True)
    a=p.parse_args()
    if not a.db.is_file():raise ValueError('BUSINESS_DATABASE_REQUIRED')
    value=GoalContractRepository(a.db).create_revision(a.goal_id,a.based_on_version,a.reason,coach_policy=REQUEST_POLICY)
    print(f"已创建版本{value['version']}草稿，仍需审核冻结；当前正式合同未改变。")


if __name__=='__main__':main()
