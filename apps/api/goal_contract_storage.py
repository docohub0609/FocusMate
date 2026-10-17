"""Durable Goal contract drafts, independent reviews and immutable frozen versions.

Existing Goal/progress and problem rows are read only. Semantic calls run outside
SQLite write transactions; revision/hash/current-version compare-and-swap decides
whether their results may freeze a contract.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import sqlite3
from uuid import uuid4

from focusmate.contracts.goal_contract_review import GoalContractReviewError
from focusmate.contracts.schema_validation import validate
from focusmate.core.goal_contract_review import review_goal_contract, validate_review_input
from focusmate.core.goal_template import build_generic_algorithm_goal_contract


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _error(code, message):
    return GoalContractReviewError(code, message)


def _linked_strategies(candidates, nodes):
    """Current projection omits retired node links; the history is never edited."""
    present = {node['subgoalId'] for node in nodes}
    result = []
    for candidate in candidates:
        linked = [node_id for node_id in candidate['subgoalIds'] if node_id in present]
        if linked:
            result.append({**deepcopy(candidate), 'subgoalIds': linked})
    return result


def _read_inputs(db, goal_id):
    """Read authoritative business inputs from the caller's SQLite snapshot."""
    try:
        goal_row = db.execute('SELECT * FROM goal_records WHERE goal_id=?', (goal_id,)).fetchone()
        if goal_row is None:
            raise ValueError('missing goal')
        problem_row = db.execute(
            'SELECT * FROM problem_versions WHERE problem_id=? AND version=?',
            (goal_row['problem_id'], goal_row['problem_version']),
        ).fetchone()
        if problem_row is None:
            raise ValueError('missing problem')
        feature_row = db.execute('SELECT result_json FROM problem_feature_sets WHERE feature_set_id=?',
                                 (problem_row['confirmed_feature_set_id'],)).fetchone()
        if feature_row is None:
            raise ValueError('missing confirmed report')
        examples = db.execute(
            'SELECT input_text, output_text FROM problem_examples WHERE problem_id=? AND version=? ORDER BY example_index',
            (goal_row['problem_id'], goal_row['problem_version']),
        ).fetchall()
        ref = {'problemId': goal_row['problem_id'], 'problemVersion': goal_row['problem_version']}
        goal = {'goalId': goal_row['goal_id'], 'schemaVersion': goal_row['schema_version'],
                'createdAt': goal_row['created_at'], 'problemRef': ref, 'status': goal_row['status']}
        problem = {
            'problemId': problem_row['problem_id'], 'schemaVersion': problem_row['schema_version'],
            'version': problem_row['version'], 'createdAt': problem_row['created_at'], 'source': 'userProvided',
            'statement': problem_row['statement'], 'inputSpec': problem_row['input_spec'],
            'outputSpec': problem_row['output_spec'], 'constraints': problem_row['constraints_text'],
            'examples': [{'input': row[0], 'output': row[1]} for row in examples],
            'languagePolicy': json.loads(problem_row['language_policy_json']), 'ioMode': problem_row['io_mode'],
            'verifierAssetStatus': problem_row['verifier_asset_status'],
        }
        lifecycle = {'status': problem_row['lifecycle_status'], 'draftRevision': problem_row['draft_revision'],
                     'contentHash': problem_row['content_hash'], 'updatedAt': problem_row['updated_at']}
        if problem_row['frozen_at']:
            lifecycle.update(frozenAt=problem_row['frozen_at'], confirmedFeatureSetId=problem_row['confirmed_feature_set_id'])
        return goal, {'problemPackage': problem, 'lifecycle': lifecycle}, json.loads(feature_row['result_json'])
    except (sqlite3.Error, ValueError, KeyError, IndexError, TypeError):
        raise _error('GOAL_INPUT_UNAVAILABLE', '无法读取这次做题对应的已确认题目。') from None


def load_inputs(path: str | Path, goal_id: str) -> tuple[dict, dict, dict]:
    """Never create a missing database or initialize business repositories."""
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            return _read_inputs(db, goal_id)
    except (sqlite3.Error, OSError, ValueError):
        raise _error('GOAL_INPUT_UNAVAILABLE', '无法读取这次做题对应的已确认题目。') from None


class GoalContractRepository:
    def __init__(self, path: str | Path, *, clock=lambda: datetime.now(timezone.utc)):
        self.path = Path(path).resolve()
        self.clock = clock
        if not self.path.is_file():
            raise _error('GOAL_INPUT_UNAVAILABLE', '已有做题数据库不存在。')
        with self._transaction(write=True) as db:
            required = {'goal_records', 'problem_versions', 'problem_examples', 'problem_feature_sets'}
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not required <= tables:
                raise _error('GOAL_INPUT_UNAVAILABLE', '已有做题数据库缺少必要数据表。')
            # Execute individually: executescript would commit the caller's transaction.
            for statement in _TABLES:
                db.execute(statement)

    def _now(self):
        value = self.clock()
        return value.isoformat() if isinstance(value, datetime) else value

    @contextmanager
    def _transaction(self, *, write=False):
        try:
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                with db:
                    db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                    yield db
        except sqlite3.Error:
            raise _error('GOAL_CONTRACT_STORAGE_FAILED', '合同记录暂时无法读取或保存。') from None

    @staticmethod
    def _row(db, goal_id, version):
        row = db.execute('SELECT * FROM goal_contract_versions WHERE goal_id=? AND version=?', (goal_id, version)).fetchone()
        if row is None:
            raise _error('GOAL_CONTRACT_NOT_FOUND', '找不到指定合同版本。')
        return row

    @staticmethod
    def _current_version(db, goal_id):
        row = db.execute("SELECT version FROM goal_contract_versions WHERE goal_id=? AND status='frozen'", (goal_id,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _next_version(db, goal_id):
        return db.execute('SELECT COALESCE(MAX(version), 0) + 1 FROM goal_contract_versions WHERE goal_id=?', (goal_id,)).fetchone()[0]

    @staticmethod
    def _compilation(db, row):
        value = json.loads(row['compilation_json'])
        strategies = db.execute('SELECT candidates_json FROM goal_contract_strategy_history WHERE goal_id=? AND version=? ORDER BY strategy_revision DESC LIMIT 1',
                                (row['goal_id'], row['version'])).fetchone()
        value['strategyCandidates'] = _linked_strategies(json.loads(strategies[0]) if strategies else [], value['contract'].get('subgoals', []))
        return value

    def _record(self, db, row):
        reviews = db.execute('SELECT * FROM goal_contract_reviews WHERE goal_id=? AND version=? ORDER BY review_id',
                             (row['goal_id'], row['version'])).fetchall()
        result = {
            'goalId': row['goal_id'], 'version': row['version'], 'draftRevision': row['draft_revision'],
            'status': row['status'], 'contentHash': row['content_hash'], 'compilation': self._compilation(db, row),
            'basedOnVersion': row['based_on_version'], 'changeReason': row['change_reason'],
            'createdAt': row['created_at'], 'updatedAt': row['updated_at'], 'frozenAt': row['frozen_at'],
            'supersededAt': row['superseded_at'], 'originalGeneration': json.loads(row['original_json']),
            'reviews': [{'reviewId': item['review_id'], 'draftRevision': item['draft_revision'], 'stale': bool(item['stale']),
                         'result': json.loads(item['result_json'])} for item in reviews],
        }
        try:
            validate('GoalContractRecordV2' if result['compilation']['contract']['schemaVersion'] == '2.0.0' else 'GoalContractRecord', result)
        except Exception:
            raise _error('INVALID_GOAL_CONTRACT_RECORD', '已保存的合同记录不符合读取格式。') from None
        return result

    def get_version(self, goal_id, version):
        with self._transaction() as db:
            return self._record(db, self._row(db, goal_id, version))

    def get_current(self, goal_id):
        with self._transaction() as db:
            version = self._current_version(db, goal_id)
            return None if version is None else self._record(db, self._row(db, goal_id, version))

    def ensure_minimal(self, goal_id):
        """Freeze only confirmed requirements; no synthetic model-generation record."""
        from focusmate.core.action_planning import REQUEST_POLICY
        with self._transaction(write=True) as db:
            current = self._current_version(db, goal_id)
            if current is not None:
                return json.loads(self._row(db, goal_id, current)['compilation_json'])['contract']
            goal, problem, _ = _read_inputs(db, goal_id)
            if problem['lifecycle']['status'] != 'frozen':
                raise _error('PROBLEM_NOT_FROZEN', '请先确认题目。')
            now, version = self._now(), self._next_version(db, goal_id)
            contract = {'schemaVersion': '2.0.0', 'goalId': goal_id, 'version': version,
                'createdAt': now, 'problemRef': goal['problemRef'],
                'problemContentHash': problem['lifecycle']['contentHash'],
                'objective': '完成已确认题目，提交通过可信判题的 Python 标准输入输出程序。',
                'finalCriterion': {'verifierType': 'finalOj', 'requiredVerdict': 'AC'},
                'coachPolicy': deepcopy(REQUEST_POLICY), 'contractStatus': 'frozen'}
            validate('GoalContractV2', contract)
            compilation = {'contract': contract, 'strategyCandidates': []}
            provenance = {'source': 'confirmedProblem', 'problemContentHash': contract['problemContentHash']}
            db.execute('''INSERT INTO goal_contract_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (goal_id, version, 1, 'frozen', hashlib.sha256(_json(compilation).encode()).hexdigest(),
                 _json(compilation), _json(provenance), None, None, now, now, now, None))
            self._save_draft_snapshot(db, self._record(db, self._row(db, goal_id, version)))
            return contract

    def list_versions(self, goal_id):
        with self._transaction() as db:
            rows = db.execute('SELECT * FROM goal_contract_versions WHERE goal_id=? ORDER BY version', (goal_id,)).fetchall()
            return [self._record(db, row) for row in rows]

    def get_draft_revision(self, goal_id, version, draft_revision):
        with self._transaction() as db:
            row = db.execute('SELECT snapshot_json FROM goal_contract_draft_revisions WHERE goal_id=? AND version=? AND draft_revision=?',
                             (goal_id, version, draft_revision)).fetchone()
            if row is None:
                raise _error('GOAL_CONTRACT_NOT_FOUND', '找不到指定草稿修订。')
            return json.loads(row[0])

    def _insert_draft(self, db, goal_id, compilation, original, inputs, *, based_on, reason):
        version, now = self._next_version(db, goal_id), self._now()
        value = deepcopy(compilation)
        value['contract'].update(version=version, contractStatus='draft', createdAt=now)
        snapshot = validate_review_input(*inputs, value)
        strategies = value.pop('strategyCandidates')
        db.execute('''INSERT INTO goal_contract_versions
            (goal_id,version,draft_revision,status,content_hash,compilation_json,original_json,based_on_version,change_reason,created_at,updated_at)
            VALUES (?,?,1,'draft',?,?,?,?,?,?,?)''',
            (goal_id, version, snapshot['contentHash'], _json(value), _json(original), based_on, reason, now, now))
        db.execute('INSERT INTO goal_contract_strategy_history (goal_id,version,strategy_revision,candidates_json,created_at) VALUES (?,?,1,?,?)',
                   (goal_id, version, _json(strategies), now))
        result = self._record(db, self._row(db, goal_id, version))
        self._save_draft_snapshot(db, result)
        return result

    @staticmethod
    def _save_draft_snapshot(db, record):
        db.execute('INSERT INTO goal_contract_draft_revisions (goal_id,version,draft_revision,snapshot_json) VALUES (?,?,?,?)',
                   (record['goalId'], record['version'], record['draftRevision'], _json(record)))

    def import_draft(self, goal_id, compilation):
        try:
            validate('GoalCompilation', compilation)
        except Exception:
            raise _error('INVALID_GOAL_CONTRACT_INPUT', '导入的目标草稿不符合合同格式。') from None
        with self._transaction(write=True) as db:
            inputs = _read_inputs(db, goal_id)
            if self._current_version(db, goal_id) is not None:
                raise _error('GOAL_CONTRACT_REVISION_REQUIRED', '已有正式合同，请注明原版本和修改原因后创建新版。')
            return self._insert_draft(db, goal_id, compilation, compilation, inputs, based_on=None, reason=None)

    @staticmethod
    def _editable(compilation):
        public = {node['subgoalId']: node for node in compilation['publicGoals']}
        rubrics = {node['subgoalId']: node for node in compilation['rubrics']}
        nodes = []
        for node in compilation['contract']['subgoals']:
            rubric = rubrics[node['subgoalId']]
            nodes.append({
                'key': node['subgoalId'], 'stage': node['stage'], 'objective': node['objective'],
                'acceptableEvidence': deepcopy(node['acceptableEvidence']), 'userGoal': public[node['subgoalId']]['text'],
                'sourceRefs': deepcopy(rubric['sourceRefs']),
                'checks': [{key: deepcopy(check[key]) for key in ('check', 'sourceRefs')} for check in rubric['checks']],
                'dependencies': [{'key': edge['subgoalId'], 'reason': edge['reason'], 'sourceRefs': deepcopy(edge['sourceRefs'])}
                                 for edge in rubric['dependencies']],
            })
        return {'subgoals': nodes, 'unknowns': deepcopy(compilation['generation']['unknowns'])}

    def export_editable(self, goal_id, version):
        return self._editable(self.get_version(goal_id, version)['compilation'])

    @staticmethod
    def _apply_edit(compilation, editable, goal):
        """Local keys are not persisted; server IDs and authoritative sources are."""
        try:
            if (not isinstance(editable, dict) or not {'subgoals', 'unknowns'} <= set(editable)
                    or set(editable) - {'objective', 'subgoals', 'unknowns'}):
                raise ValueError('protected or missing fields')
            if editable.get('objective', compilation['contract']['objective']) != compilation['contract']['objective']:
                raise ValueError('fixed overall objective')
            if not isinstance(editable['subgoals'], list) or not 5 <= len(editable['subgoals']) <= 25:
                raise ValueError('invalid nodes')
            required = {'key', 'stage', 'objective', 'userGoal', 'sourceRefs', 'checks', 'dependencies'}
            existing = {node['subgoalId']: node for node in compilation['contract']['subgoals']}
            ids = {}
            for node in editable['subgoals']:
                if not isinstance(node, dict) or not required <= set(node) or set(node) - required - {'acceptableEvidence'}:
                    raise ValueError('protected node fields')
                key = node['key']
                if not isinstance(key, str) or not key.strip() or key in ids:
                    raise ValueError('invalid key')
                if key in existing:
                    ids[key] = key
                elif key.startswith('new:') and key[4:].strip():
                    ids[key] = f"{goal['goalId']}:subgoal:{uuid4().hex}"
                else:
                    raise ValueError('foreign identifier')
            catalog = {source['evidenceId']: source for source in compilation['sourceCatalog']}
            def resolve(refs):
                if not isinstance(refs, list) or not refs or len(refs) != len(set(refs)):
                    raise ValueError('invalid refs')
                return [deepcopy(catalog[ref]) for ref in refs]
            defaults = {node['stage']: node['acceptableEvidence'] for node in
                        build_generic_algorithm_goal_contract(goal, created_at=compilation['contract']['createdAt'])['subgoals']}
            value = deepcopy(compilation)
            value['contract']['subgoals'], value['publicGoals'], value['rubrics'] = [], [], []
            for node in editable['subgoals']:
                key, node_id = node['key'], ids[node['key']]
                evidence = node.get('acceptableEvidence', existing.get(key, {}).get('acceptableEvidence', defaults[node['stage']]))
                checks, dependencies = [], []
                if not isinstance(node['checks'], list) or not isinstance(node['dependencies'], list):
                    raise ValueError('invalid checks/dependencies')
                for check in node['checks']:
                    if set(check) != {'check', 'sourceRefs'}:
                        raise ValueError('protected check fields')
                    checks.append({**deepcopy(check), 'sources': resolve(check['sourceRefs'])})
                for edge in node['dependencies']:
                    if set(edge) != {'key', 'reason', 'sourceRefs'}:
                        raise ValueError('protected dependency fields')
                    dependencies.append({'subgoalId': ids[edge['key']], 'reason': edge['reason'],
                                         'sourceRefs': deepcopy(edge['sourceRefs']), 'sources': resolve(edge['sourceRefs'])})
                value['contract']['subgoals'].append({'subgoalId': node_id, 'stage': node['stage'], 'objective': node['objective'],
                    'acceptableEvidence': deepcopy(evidence), 'dependsOn': [edge['subgoalId'] for edge in dependencies]})
                value['publicGoals'].append({'subgoalId': node_id, 'stage': node['stage'], 'text': node['userGoal']})
                value['rubrics'].append({'goalId': goal['goalId'], 'subgoalId': node_id, 'sourceRefs': deepcopy(node['sourceRefs']),
                    'sources': resolve(node['sourceRefs']), 'checks': checks, 'dependencies': dependencies})
            value['generation']['unknowns'] = deepcopy(editable['unknowns'])
            value['strategyCandidates'] = _linked_strategies(value['strategyCandidates'], value['contract']['subgoals'])
            return value
        except (KeyError, TypeError, ValueError):
            raise _error('INVALID_GOAL_CONTRACT_EDIT', '只能修改目标、公开文案、细则、依赖、证据选择和未知项；请检查引用。') from None

    @staticmethod
    def _ensure_draft(row, expected_revision):
        if row['status'] != 'draft':
            raise _error('GOAL_CONTRACT_IMMUTABLE', '正式合同正文不能修改，请创建新版草稿。')
        if type(expected_revision) is not int or row['draft_revision'] != expected_revision:
            raise _error('STALE_GOAL_CONTRACT_DRAFT', '草稿已发生修改，请重新读取后操作。')

    def edit_draft(self, goal_id, version, editable, expected_draft_revision):
        with self._transaction(write=True) as db:
            row = self._row(db, goal_id, version)
            self._ensure_draft(row, expected_draft_revision)
            inputs = _read_inputs(db, goal_id)
            value = self._apply_edit(self._compilation(db, row), editable, inputs[0])
            snapshot = validate_review_input(*inputs, value)
            value.pop('strategyCandidates')
            db.execute('UPDATE goal_contract_versions SET draft_revision=draft_revision+1,content_hash=?,compilation_json=?,updated_at=? WHERE goal_id=? AND version=?',
                       (snapshot['contentHash'], _json(value), self._now(), goal_id, version))
            result = self._record(db, self._row(db, goal_id, version))
            self._save_draft_snapshot(db, result)
            return result

    def create_revision(self, goal_id, based_on_version, reason, *, coach_policy=None):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise _error('INVALID_GOAL_CONTRACT_CHANGE_REASON', '创建新版需说明具体修改原因。')
        with self._transaction(write=True) as db:
            row = self._row(db, goal_id, based_on_version)
            if row['status'] != 'frozen' or self._current_version(db, goal_id) != based_on_version:
                raise _error('STALE_GOAL_CONTRACT_BASE', '只能基于当前正式合同创建新版。')
            compilation = self._compilation(db, row)
            if coach_policy is not None:
                # An explicit revision request changes only the new draft. The
                # current frozen row remains authoritative until review/freeze.
                compilation['contract']['coachPolicy'] = deepcopy(coach_policy)
            return self._insert_draft(db, goal_id, compilation, json.loads(row['original_json']),
                                     _read_inputs(db, goal_id), based_on=based_on_version, reason=reason.strip())

    def review_and_freeze(self, goal_id, version, provider, expected_draft_revision):
        with self._transaction() as db:
            row = self._row(db, goal_id, version)
            if row['status'] in ('frozen', 'superseded'):
                return self._record(db, row)
            self._ensure_draft(row, expected_draft_revision)
            inputs = _read_inputs(db, goal_id)
            compilation = self._compilation(db, row)
            snapshot = validate_review_input(*inputs, compilation)
            base_version = self._current_version(db, goal_id)
            if row['based_on_version'] != base_version:
                raise _error('STALE_GOAL_CONTRACT_BASE', '当前正式版已变化，请基于它重新创建草稿。')
            revision, digest = row['draft_revision'], snapshot['contentHash']
        # The external model must never hold SQLite locks across its network request.
        review = review_goal_contract(*inputs, compilation, provider, created_at=self._now())
        try:
            validate('GoalContractReview', review)
            if any(review.get(key) != snapshot[key] for key in
                   ('goalId', 'contractVersion', 'problemRef', 'problemContentHash', 'featureSetId', 'contentHash')):
                raise ValueError('review context mismatch')
        except Exception:
            raise _error('INVALID_GOAL_CONTRACT_REVIEW', '审核结果与本次合同快照不一致。') from None
        stale = False
        with self._transaction(write=True) as db:
            latest = self._row(db, goal_id, version)
            if latest['status'] in ('frozen', 'superseded') and latest['content_hash'] == digest:
                # Another identical in-flight review already froze this version.
                # Retain this independent model result without applying it again.
                db.execute('INSERT INTO goal_contract_reviews (goal_id,version,draft_revision,stale,result_json,created_at) VALUES (?,?,?,1,?,?)',
                           (goal_id, version, revision, _json(review), self._now()))
                return self._record(db, latest)
            stale = (latest['status'] != 'draft' or latest['draft_revision'] != revision or latest['content_hash'] != digest
                     or self._current_version(db, goal_id) != base_version)
            # Re-read authoritative sources after the call; asset availability is intentionally excluded.
            try:
                fresh = validate_review_input(*_read_inputs(db, goal_id), self._compilation(db, latest))
                stale = stale or fresh['contentHash'] != digest
            except GoalContractReviewError:
                stale = True
            now = self._now()
            if not stale and review['status'] == 'passed':
                if base_version is not None:
                    db.execute("UPDATE goal_contract_versions SET status='superseded',superseded_at=?,updated_at=? WHERE goal_id=? AND version=? AND status='frozen'",
                               (now, now, goal_id, base_version))
                frozen = json.loads(latest['compilation_json'])
                frozen['contract']['contractStatus'] = 'frozen'
                db.execute("UPDATE goal_contract_versions SET status='frozen',compilation_json=?,frozen_at=?,updated_at=? WHERE goal_id=? AND version=?",
                           (_json(frozen), now, now, goal_id, version))
            db.execute('INSERT INTO goal_contract_reviews (goal_id,version,draft_revision,stale,result_json,created_at) VALUES (?,?,?,?,?,?)',
                       (goal_id, version, revision, int(stale), _json(review), now))
            if not stale:
                db.execute('UPDATE goal_contract_versions SET updated_at=? WHERE goal_id=? AND version=?', (now, goal_id, version))
            result = self._record(db, self._row(db, goal_id, version))
        if stale:
            raise _error('STALE_GOAL_CONTRACT_REVIEW', '审核期间草稿或当前正式版已变化，旧审核已保存但未用于冻结。')
        return result

    def resolve_contract_reference(self, goal_id, version, subgoal_id=None):
        record = self.get_version(goal_id, version)
        if record['status'] not in ('frozen', 'superseded'):
            raise _error('GOAL_CONTRACT_NOT_FROZEN', '验证引用只能指向已冻结的合同版本。')
        result = {'goalId': goal_id, 'goalContractVersion': version, 'contentHash': record['contentHash'], 'status': record['status']}
        if subgoal_id is not None:
            subgoal = next((node for node in record['compilation']['contract']['subgoals'] if node['subgoalId'] == subgoal_id), None)
            if subgoal is None:
                raise _error('GOAL_CONTRACT_SUBGOAL_NOT_FOUND', '指定子目标不属于这份合同版本。')
            result['subgoal'] = deepcopy(subgoal)
        return result


_TABLES = [
    '''CREATE TABLE IF NOT EXISTS goal_contract_versions (
        goal_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version > 0),
        draft_revision INTEGER NOT NULL CHECK(draft_revision > 0),
        status TEXT NOT NULL CHECK(status IN ('draft','frozen','superseded')),
        content_hash TEXT NOT NULL, compilation_json TEXT NOT NULL, original_json TEXT NOT NULL,
        based_on_version INTEGER, change_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        frozen_at TEXT, superseded_at TEXT, PRIMARY KEY(goal_id,version),
        FOREIGN KEY(goal_id) REFERENCES goal_records(goal_id),
        FOREIGN KEY(goal_id,based_on_version) REFERENCES goal_contract_versions(goal_id,version))''',
    "CREATE UNIQUE INDEX IF NOT EXISTS goal_contract_one_frozen ON goal_contract_versions(goal_id) WHERE status='frozen'",
    '''CREATE TABLE IF NOT EXISTS goal_contract_draft_revisions (
        goal_id TEXT NOT NULL,version INTEGER NOT NULL,draft_revision INTEGER NOT NULL,snapshot_json TEXT NOT NULL,
        PRIMARY KEY(goal_id,version,draft_revision),
        FOREIGN KEY(goal_id,version) REFERENCES goal_contract_versions(goal_id,version))''',
    '''CREATE TABLE IF NOT EXISTS goal_contract_strategy_history (
        goal_id TEXT NOT NULL,version INTEGER NOT NULL,strategy_revision INTEGER NOT NULL,candidates_json TEXT NOT NULL,created_at TEXT NOT NULL,
        PRIMARY KEY(goal_id,version,strategy_revision),
        FOREIGN KEY(goal_id,version) REFERENCES goal_contract_versions(goal_id,version))''',
    '''CREATE TABLE IF NOT EXISTS goal_contract_reviews (
        review_id INTEGER PRIMARY KEY AUTOINCREMENT,goal_id TEXT NOT NULL,version INTEGER NOT NULL,
        draft_revision INTEGER NOT NULL,stale INTEGER NOT NULL CHECK(stale IN (0,1)),result_json TEXT NOT NULL,created_at TEXT NOT NULL,
        FOREIGN KEY(goal_id,version,draft_revision) REFERENCES goal_contract_draft_revisions(goal_id,version,draft_revision))''',
]
