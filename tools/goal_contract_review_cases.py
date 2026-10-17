"""Complete synthetic T042 cases; no compiler, model call, or database writes.

The six development mutation *judgments* were approved as representatives.
Other cases and all feature records are AI-prepared fixtures, not human audits.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from apps.api.problem_features import build_human_feature_set
from apps.api.problem_intake import create_problem_package
from focusmate.contracts.problem_source import build_evidence_catalog, problem_content_hash
from focusmate.contracts.schema_validation import validate
from focusmate.core.goal_template import build_generic_algorithm_goal_contract

CREATED_AT = '2026-09-12T03:00:00Z'
DEVELOPMENT = ('two-sum', 'graph-connectivity', 'merge-intervals')
HOLDOUT = ('array-deduplication', 'bracket-matching', 'grid-path')
STAGES = ('problemUnderstanding', 'feasibleStrategy', 'coreStateAndCorrectness',
          'coreImplementation', 'boundaryAndTesting')


def _clock():
    return datetime(2026, 9, 12, 3, tzinfo=timezone.utc)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _check(text, *refs):
    return {'check': text, 'sourceRefs': list(refs)}


def _node(title, checks):
    return {'title': title, 'checks': checks}


def _nodes(name, problem):
    """Acceptance text is explicit; evidence quotations are hydrated separately."""
    if name == 'two-sum':
        understanding = _node('说明数组、目标值和结果下标的含义', [
            _check('输入第一行包含n与target，第二行包含n个整数；数组位置从0编号。', 'inputSpec:0', 'inputSpec:1', 'statement:0'),
            _check('每个合法输入恰有一组答案。', 'statement:1'),
        ])
        strategy = _node('提出符合数组规模的方案并估算开销', [
            _check('说明输入规模为2 <= n <= 100000。', 'constraints:0'),
            _check('数组元素和target均在-1000000000至1000000000之间。', 'constraints:1'),
            _check('估算所选方案的时间与空间开销，并结合题面输入规模判断可行性。', 'constraints:0'),
        ])
        correctness = _node('说明所选方案怎样满足答案条件', [
            _check('两个返回位置对应数值之和等于target。', 'statement:0'),
            _check('两个返回位置必须不同，不能重复使用同一位置；两个位置上的数值可以相同。', 'statement:0', 'statement:1'),
        ])
        implementation = _node('实现约定的输入读取和结果输出', [
            _check('读取规定的两行输入，并输出两个下标，小的下标在前，以空格分隔。', 'inputSpec:0', 'inputSpec:1', 'outputSpec:0'),
        ])
        boundaries = _check('设计n取最小值、含负数或0、数值取边界及n取上限的检查，逐例说明结果应满足的题目条件。', 'constraints:0', 'constraints:1')
    elif name == 'graph-connectivity':
        understanding = _node('说明无向图输入及路径存在性的含义', [
            _check('判断无向图中从s到t是否存在路径，顶点从0编号。', 'statement:0'),
            _check('输入第一行n、m、s、t，随后m行无向边u、v。', 'inputSpec:0', 'inputSpec:1'),
            _check('图可不连通，可含自环与重复边；s等于t时存在路径。', 'statement:1'),
        ])
        strategy = _node('提出符合图规模的方案并估算开销', [
            _check('说明1 <= n <= 100000、0 <= m <= 200000，所有顶点编号均大于等于0且小于n。', 'constraints:0', 'constraints:1', 'constraints:2'),
            _check('估算所选图连通性判定方案的时间和空间开销，并结合n和m判断可行性。', 'constraints:0', 'constraints:1'),
        ])
        # A broad title is acceptable when its actual rubric is observable.
        correctness = _node('充分掌握图的连通性', [
            _check('说明所选方案为何在存在s到t路径时给出YES，并在不存在时给出NO。', 'statement:0', 'outputSpec:0'),
            _check('分别说明自环、重复边、图不连通与s等于t时怎样符合题面定义。', 'statement:1'),
        ])
        implementation = _node('实现图数据读取和路径判断结果输出', [
            _check('按规定读入n、m、s、t及m条无向边，存在路径输出YES，否则输出NO。', 'inputSpec:0', 'inputSpec:1', 'outputSpec:0'),
        ])
        boundaries = _check('检查无边图、重复边、自环、起终点相同和图不连通的代表输入，逐例按路径定义解释结果。', 'statement:1', 'constraints:0', 'constraints:1')
    elif name == 'merge-intervals':
        understanding = _node('说明闭区间端点和输入形式', [
            _check('输入n个闭区间，第一行n，随后n行各含l和r。', 'statement:0', 'inputSpec:0', 'inputSpec:1'),
            _check('两个区间端点相接也视为有交集；输入顺序任意，可含重复和包含关系。', 'statement:1'),
        ])
        strategy = _node('提出符合区间规模的方案并估算开销', [
            _check('说明1 <= n <= 100000，端点满足-1000000000 <= l <= r <= 1000000000。', 'constraints:0', 'constraints:1'),
            _check('估算所选区间处理方案的时间与空间开销，并结合规模判断可行性。', 'constraints:0'),
        ])
        correctness = _node('说明结果区间必须满足的条件', [
            _check('输出区间应覆盖每个原区间。', 'statement:0'),
            _check('输出区间之间应互不相交，端点相接也属于有交集。', 'statement:0', 'statement:1'),
            _check('输出与原区间应具有完全相同的覆盖范围，不得扩大范围或填补原本不存在的间隔。', 'statement:0'),
        ])
        implementation = _node('实现区间数据读取和规定格式的输出', [
            _check('第一行输出结果区间数k，随后k行按左端点从小到大输出每个区间的左右端点。', 'outputSpec:0', 'outputSpec:1'),
            _check('按规定读入n及n行端点，并按合同中的结果条件处理输入。', 'inputSpec:0', 'inputSpec:1'),
        ])
        boundaries = _check('设计单个区间、相同端点、端点相接、重复区间、包含关系和输入逆序的检查，按已列结果条件核对。', 'statement:1', 'constraints:0', 'constraints:1')
    elif name == 'array-deduplication':
        understanding = _node('说明数组去重的输入及结果对象', [
            _check('输入第一行n，第二行n个整数；结果对象是数值而非下标。', 'inputSpec:0', 'inputSpec:1', 'statement:0'),
        ])
        strategy = _node('提出符合数组长度的方案并估算开销', [
            _check('说明输入规模为1 <= n <= 200000。', 'constraints:0'),
            _check('数组元素在-1000000000至1000000000之间。', 'constraints:1'),
            _check('估算所选方案的时间与空间开销，并结合n判断可行性。', 'constraints:0'),
        ])
        correctness = _node('说明结果保留的数值和排列条件', [
            _check('原数组中的每个不同数值在结果中出现且只出现一次，不得增加原数组不存在的数值。', 'statement:0'),
            _check('结果按每个数值在原数组中第一次出现的顺序排列。', 'statement:1'),
        ])
        implementation = _node('实现数组读取和去重结果输出', [
            _check('读取n及n个整数；第一行输出结果数值数量k，第二行用空格分隔输出k个整数。', 'inputSpec:0', 'inputSpec:1', 'outputSpec:0', 'outputSpec:1'),
        ])
        boundaries = _check('检查单个元素、全相同、全不同、负数和0、取值边界与最大长度的输入，按结果条件逐项核对。', 'statement:0', 'constraints:0', 'constraints:1')
    elif name == 'bracket-matching':
        understanding = _node('说明括号字符串输入和整体合法性的含义', [
            _check('字符串非空，只含()[]{}六种字符；第一行n，第二行长度为n的字符串。', 'statement:0', 'inputSpec:0', 'inputSpec:1', 'constraints:1'),
            _check('说明整体合法性的条件：每个左括号由同类型右括号闭合，右括号对应一个尚未闭合的左括号，括号对正确嵌套且不能交叉。', 'statement:1'),
        ])
        strategy = _node('提出符合字符串长度的方案并估算开销', [
            _check('说明1 <= n <= 100000，并估算所选方案的时间和空间开销。', 'constraints:0'),
        ])
        correctness = _node('说明各括号和括号对必须满足的条件', [
            _check('每个左括号必须由同类型右括号闭合，右括号必须对应一个尚未闭合的左括号。', 'statement:1'),
            _check('括号对必须正确嵌套，不能交叉。', 'statement:1'),
        ])
        implementation = _node('实现括号输入读取及合法性输出', [
            _check('读取n和完整字符串；整个字符串合法时输出YES，否则输出NO。', 'inputSpec:0', 'inputSpec:1', 'outputSpec:0'),
        ])
        boundaries = _check('检查单个括号、前置右括号、未闭合左括号、不同括号类型与多层嵌套，逐例对照已列合法性条件。', 'statement:1', 'constraints:0')
    else:
        understanding = _node('说明网格、坐标和可通行格子的含义', [
            _check('网格为R行C列，坐标从0编号；0可通行，1为障碍。', 'statement:0', 'statement:1'),
            _check('第一行R、C，随后R行各C个0或1且无空格，最后sr、sc、tr、tc分别是起终点行列。', 'inputSpec:0', 'inputSpec:1', 'inputSpec:2'),
        ])
        strategy = _node('提出符合网格规模的方案并估算开销', [
            _check('说明1 <= R,C <= 1000，起终点行坐标在[0,R)内、列坐标在[0,C)内，并估算时间与空间开销。', 'constraints:0', 'constraints:1', 'constraints:2'),
        ])
        correctness = _node('说明所选方案怎样符合路径定义', [
            _check('存在路径意味着可从起点到终点；每步仅向上、下、左、右移动到网格内可通行的相邻格子，不能斜走或穿越障碍。', 'statement:0', 'statement:1'),
            _check('起点或终点为障碍时无路径；起终点是同一可通行格子时有路径。', 'statement:2'),
        ])
        implementation = _node('实现网格输入读取及路径判断输出', [
            _check('按规定读取网格和起终点坐标，存在路径输出YES，否则输出NO。', 'inputSpec:0', 'inputSpec:1', 'inputSpec:2', 'outputSpec:0'),
        ])
        boundaries = _check('检查单格、单行、单列、起终点障碍、只有对角位置可通行和无法到达终点的输入，逐例按路径定义解释结果。', 'statement:1', 'statement:2', 'constraints:0')
    example_refs = tuple(item['evidenceId'] for item in build_evidence_catalog(problem)
                         if item['sourceField'] == 'examples')
    testing = _node('运行公开样例并检查代表边界输入', [
        _check('运行全部公开样例，要求输出逐行与公开预期一致；该结果仅支持这些样例。', *example_refs),
        boundaries,
    ])
    return [understanding, strategy, correctness, implementation, testing]


def _build_inputs(name):
    folder = ROOT / 'examples/goal-compilation' if name in DEVELOPMENT else ROOT / 'examples/goal-contract-review/problems'
    intake = json.loads((folder / f'{name}.json').read_text(encoding='utf-8'))
    problem = create_problem_package(intake, clock=_clock, id_factory=lambda: f'problem-t042-fixture-{name}')
    catalog = build_evidence_catalog(problem)
    sources = {source['evidenceId']: source for source in catalog}
    features = []
    for key, category, field in [('scale.primary', 'dataScale', 'constraints'),
                                  ('input.primary', 'inputStructure', 'inputSpec'),
                                  ('output.primary', 'outputStructure', 'outputSpec'),
                                  ('domain.primary', 'domainTag', 'statement')]:
        features.append({'featureKey': key, 'category': category, 'status': 'confirmed',
                         'value': ({'two-sum': 'array', 'array-deduplication': 'array',
                                    'graph-connectivity': 'graph', 'merge-intervals': 'interval',
                                    'bracket-matching': 'string', 'grid-path': 'grid'}[name]
                                   if category == 'domainTag' else intake[field].splitlines()[0]), 'confidence': 1,
                         'reason': '合成评测fixture：直接摘录题面；humanCreated仅模拟接口前提，不是真实人工审核记录。',
                         'evidenceRefs': [f'{field}:0']})
    features.append({'featureKey': 'resource.primary', 'category': 'resourceConstraint',
                     'status': 'unknown', 'reason': '题面未提供独立的时间和内存数值限额。', 'evidenceRefs': []})
    feature_set = build_human_feature_set(problem, {'expectedDraftRevision': 1, 'features': features},
                                          clock=_clock, id_factory=lambda: f'features-t042-fixture-{name}')
    digest = problem_content_hash(problem)
    problem_record = {'problemPackage': problem, 'lifecycle': {
        'status': 'frozen', 'draftRevision': 1, 'contentHash': digest,
        'updatedAt': CREATED_AT, 'frozenAt': CREATED_AT, 'confirmedFeatureSetId': feature_set['featureSetId'],
    }}
    goal = {'goalId': f'goal-t042-fixture-{name}', 'schemaVersion': '1.0.0', 'createdAt': CREATED_AT,
            'problemRef': deepcopy(feature_set['problemRef']), 'status': 'active'}
    contract = build_generic_algorithm_goal_contract(goal, created_at=CREATED_AT)
    public, rubrics = [], []
    for index, node in enumerate(_nodes(name, problem)):
        subgoal = contract['subgoals'][index]
        subgoal['subgoalId'] = f"{goal['goalId']}:subgoal-{index + 1:02d}"
        subgoal['objective'] = node['title']
        public.append({'subgoalId': subgoal['subgoalId'], 'stage': subgoal['stage'], 'text': node['title']})
        refs = list(dict.fromkeys(ref for check in node['checks'] for ref in check['sourceRefs']))
        rubrics.append({'goalId': goal['goalId'], 'subgoalId': subgoal['subgoalId'], 'sourceRefs': refs,
                        'sources': [deepcopy(sources[ref]) for ref in refs], 'dependencies': [],
                        'checks': [{**check, 'sources': [deepcopy(sources[ref]) for ref in check['sourceRefs']]}
                                   for check in node['checks']]})
    compilation = {
        'contract': contract, 'publicGoals': public, 'rubrics': rubrics,
        'strategyCandidates': [], 'sourceCatalog': catalog,
        'generation': {'promptVersion': 'goal-compilation-v6', 'model': 'synthetic-fixture-no-model',
                       'providerResponseId': 'synthetic-fixture-no-request', 'problemContentHash': digest,
                       'featureSetId': feature_set['featureSetId'], 'inputHash': _hash([goal, problem_record, feature_set]),
                       'unknowns': ['用户尚未选择具体算法；策略候选不构成合同验收要求。'],
                       'inputUncertainties': [deepcopy(feature) for feature in feature_set['features'] if feature['status'] != 'confirmed'],
                       'semanticStatus': 'unverified', 'goalId': goal['goalId'], 'problemRef': deepcopy(goal['problemRef'])},
    }
    return {'goal_record': goal, 'problem_record': problem_record, 'feature_set': feature_set, 'compilation': compilation}


def _mutation(inputs, name, kind):
    compilation = inputs['compilation']
    rubrics = compilation['rubrics']
    if kind == 'numericMismatch':
        check = rubrics[1]['checks'][0]
        check['check'] = check['check'].replace('100000', '1000000') if name == 'two-sum' else check['check'].replace('200000', '2000000')
    elif kind == 'conditionMismatch':
        if name == 'two-sum':
            rubrics[2]['checks'][1]['check'] = '两个返回位置必须不同，不能重复使用同一位置；两个位置上的数值也必须不同。'
        else:
            rubrics[2]['checks'][1]['check'] = '括号对允许交叉，只要每个括号均能找到同类型的配对括号。'
    elif kind == 'missingRequirement':
        rubrics[2]['checks'].pop(2 if name == 'merge-intervals' else 1)
    elif kind == 'prescribedStrategy':
        rubrics[1]['checks'][-1]['check'] += (' 必须采用广度优先搜索BFS，其他判定方法不算通过。' if name == 'graph-connectivity'
                                             else ' 必须使用栈完成括号匹配，其他判定方法不算通过。')
    elif kind == 'unverifiableObjective':
        title = '充分掌握图的连通性' if name == 'graph-connectivity' else '充分掌握网格路径问题'
        compilation['contract']['subgoals'][2]['objective'] = title
        compilation['publicGoals'][2]['text'] = title
        for check in rubrics[2]['checks']:
            check['check'] = title
    elif kind == 'insufficientEvidence':
        catalog = {source['evidenceId']: source for source in compilation['sourceCatalog']}
        refs = ['examples:0:input', 'examples:0:output']
        rubrics[4]['checks'].append({'check': '第一组公开样例通过，就足以证明程序在所有合法输入上均正确。',
                                     'sourceRefs': refs, 'sources': [deepcopy(catalog[ref]) for ref in refs]})


MUTATIONS = {
    'two-sum': [('numeric-mismatch', 'numericMismatch', 'n上限应为100000，合同误写1000000。'),
                ('condition-mismatch', 'conditionMismatch', '题面允许相同数值，合同错误要求两个数值不同。')],
    'graph-connectivity': [('prescribed-strategy', 'prescribedStrategy', '题目只要求判定连通性，不要求必须使用BFS。'),
                           ('unverifiable-objective', 'unverifiableObjective', '目标标题和全部相关细则都只说充分掌握，没有可观察要求。')],
    'merge-intervals': [('missing-requirement', 'missingRequirement', '只要求覆盖原区间及结果互不相交，漏掉不得扩大原有覆盖范围。'),
                        ('insufficient-evidence', 'insufficientEvidence', '第一组样例通过无法证明所有合法输入正确。')],
    'array-deduplication': [('numeric-mismatch', 'numericMismatch', 'n上限应为200000，合同误写2000000。'),
                            ('missing-requirement', 'missingRequirement', '合同遗漏按第一次出现的顺序输出的关键要求。')],
    'bracket-matching': [('condition-mismatch', 'conditionMismatch', '题面禁止括号对交叉，合同允许交叉。'),
                         ('prescribed-strategy', 'prescribedStrategy', '题面没有规定必须使用栈。')],
    'grid-path': [('unverifiable-objective', 'unverifiableObjective', '目标和对应全部细则只有充分掌握，不能据此验收。'),
                   ('insufficient-evidence', 'insufficientEvidence', '第一组样例通过无法证明所有合法输入正确。')],
}


def build_cases() -> list[dict]:
    """Return fresh complete cases; expected/provenance stay outside review inputs."""
    cases = []
    for name in (*DEVELOPMENT, *HOLDOUT):
        split = 'development' if name in DEVELOPMENT else 'holdout'
        baseline = _build_inputs(name)
        variants = [('correct', None, '题面关键条件完整，目标有可观察的内部细则，证据适用。'),
                    *MUTATIONS[name], ('nonessential-unknown', None, '实际机器耗时未知，不影响题面要求及复杂度估算。')]
        for suffix, issue_type, rationale in variants:
            inputs = deepcopy(baseline)
            if issue_type:
                _mutation(inputs, name, issue_type)
            if suffix == 'nonessential-unknown':
                inputs['compilation']['generation']['unknowns'].append('所选方案在具体评测机器上的实际运行耗时未知；仍按题面规模估算时间和空间开销，不把某个秒数作为验收要求。')
            expected = {'status': 'needsReview' if issue_type else 'passed', 'issueType': issue_type,
                        'blocking': bool(issue_type), 'rationale': rationale}
            alternatives = {
                'conditionMismatch': ['conditionMismatch', 'objectMismatch'] if name == 'two-sum' else ['conditionMismatch'],
                'missingRequirement': ['missingRequirement', 'conditionMismatch'] if name == 'merge-intervals' else ['missingRequirement'],
                'insufficientEvidence': ['insufficientEvidence', 'inappropriateEvidence'],
            }
            expected['acceptableIssueTypes'] = alternatives.get(issue_type, [issue_type] if issue_type else [])
            if issue_type:
                target_index = {'numericMismatch': 1, 'prescribedStrategy': 1, 'conditionMismatch': 2,
                                'missingRequirement': 2, 'unverifiableObjective': 2, 'insufficientEvidence': 4}[issue_type]
                source_refs = {
                    'numericMismatch': ['constraints:0'],
                    'prescribedStrategy': ['statement:0', 'constraints:0'],
                    'conditionMismatch': ['statement:0', 'statement:1'],
                    'missingRequirement': ['statement:0'] if name == 'merge-intervals' else ['statement:1', 'outputSpec:1'],
                    'unverifiableObjective': [],
                    'insufficientEvidence': ['examples:0:input', 'examples:0:output'],
                }[issue_type]
                expected['acceptableItemIds'] = ['node:' + inputs['compilation']['contract']['subgoals'][target_index]['subgoalId']]
                expected['acceptableItemIds'] += ['source:' + ref for ref in source_refs]
            else:
                expected['acceptableItemIds'] = []
            if name == 'merge-intervals' and issue_type == 'missingRequirement':
                expected['counterexample'] = {'input': '2\n1 2\n5 6\n', 'badOutput': '1\n1 6\n', 'correctOutput': '2\n1 2\n5 6\n'}
            case = {'id': f'{name}-{suffix}', 'split': split, 'inputs': inputs, 'expected': expected,
                    'provenance': {'humanReview': 'representativeApproved' if split == 'development' and issue_type else 'aiPreparedUnreviewed',
                                   'humanFeatureReport': 'syntheticFixture', 'fixtureAuthor': 'aiPrepared',
                                   'reviewScope': '六个开发组单点错误的判定依据获用户认可；完整案例及保留组尚未逐份人工审核。',
                                   'construction': 'handBuiltSchemaValidCompilation; T041 compiler and business database not used'}}
            for key, schema in [('goal_record', 'GoalRecord'), ('problem_record', 'ProblemRecord'),
                                ('feature_set', 'ProblemFeatures'), ('compilation', 'GoalCompilation')]:
                validate(schema, inputs[key])
            cases.append(case)
    return cases


if __name__ == '__main__':
    destination = ROOT / 'examples/goal-contract-review/cases.json'
    if destination.exists():
        raise SystemExit('cases.json already exists; update it explicitly after reviewing fixture changes.')
    destination.write_text(json.dumps(build_cases(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
