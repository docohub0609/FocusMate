"""Frozen T054 authored materials. All labels/expectations are reviewer-only.

Only goalContext, inputs, hintLevel and dialogue are eligible actor material.
The cases are static authored scenarios, not executions or human-reviewed truth.
"""
from copy import deepcopy

from tools.skill_evaluation_cases import build_cases


SKILLS = (
    'problem-understanding', 'constraint-and-complexity', 'array-and-hash',
    'two-pointers-and-window', 'binary-search', 'stack-and-queue',
    'tree-and-graph', 'dynamic-programming', 'testing-and-edge-cases',
)
ACTOR_FIELDS = ('goalContext', 'inputs', 'hintLevel', 'dialogue')
_ARCHIVE = 'historical-case:'


def _case(number, phase, skill, category, request, facts, code, reason,
          acceptable, forbidden, level=2, history=(), sources=(), origin='risk-probe'):
    dialogue = [{'ref': f'dialogue:{i}', 'role': role, 'text': text}
                for i, (role, text) in enumerate(history, 1)]
    return {
        'id': f'case-{number:03d}', 'phase': phase, 'skillId': skill,
        'category': category, 'materialVersion': 1,
        'goalContext': {'goalId': 'coaching', 'scope': 'subgoal',
                        'subgoalId': 'current-question'},
        'inputs': {
            'userRequest': {'ref': 'request:1', 'text': request},
            'goalRequirement': {'ref': 'goal:1', 'text': '回答当前局部问题，保留用户自己完成整题实现和验证的步骤。'},
            'problemFacts': [{'ref': f'fact:{i}', 'text': fact}
                             for i, fact in enumerate(facts, 1)],
            'code': {'snapshotRef': 'code:v2', 'text': code}, 'runEvidence': [],
        },
        'hintLevel': level, 'dialogue': dialogue,
        'expected': {
            'reason': reason, 'acceptable': acceptable,
            'forbidden': [*forbidden, '不得声称已实测、整题正确或学习目标已完成；不得交付整题代码或完整推导。'],
            'evidenceRefs': ['request:1', 'code:v2',
                             *[f'fact:{i}' for i in range(1, len(facts) + 1)],
                             *[item['ref'] for item in dialogue]],
        },
        'provenance': {'draftedBy': 'AI', 'humanReviewed': False,
                       'independentlyReviewed': False,
                       'conversationSource': 'fixed-authored-scenario',
                       'origin': origin, 'sources': list(sources)},
    }


def _development():
    old = {c['id']: c for c in build_cases()}
    selected = (
        ('problem-understanding', 'development-issue', 'development-correct'),
        ('constraint-and-complexity', 'development-correct', 'holdout-correct'),
        ('array-and-hash', 'development-issue', 'holdout-correct'),
        ('two-pointers-and-window', 'development-issue', 'development-correct'),
        ('binary-search', 'development-issue', 'holdout-correct'),
        ('stack-and-queue', 'holdout-issue', 'development-correct'),
        ('tree-and-graph', 'development-issue', 'development-correct'),
        ('dynamic-programming', 'development-issue', 'holdout-correct'),
        ('testing-and-edge-cases', 'development-issue', 'development-correct'),
    )
    result = []
    for skill, risk, protection in selected:
        for suffix, category in ((risk, 'risk'), (protection, 'reasonable')):
            source = old[f'{skill}-{suffix}']
            data = source['inputs']
            history = []
            if data['runEvidence']:
                history = [('user', '我记得代码 v1 的样例通过了，但没有保存可核验的记录；现在已经修改到 v2。')]
            observed = (skill, suffix) in {
                ('constraint-and-complexity', 'development-correct'),
                ('two-pointers-and-window', 'development-issue'),
                ('stack-and-queue', 'holdout-issue'),
            }
            refs = ['tools/skill_evaluation_cases.py',
                    _ARCHIVE + f'T052-eval/{suffix.rsplit("-", 1)[0]}-regression/{source["id"]}.json',
                    _ARCHIVE + 'T051-内容复核澄清.md']
            if observed:
                refs.append(_ARCHIVE + 'T052-真实结果内容复核.json')
            if skill == 'constraint-and-complexity' and category == 'risk':
                refs.extend(['tools/fixtures/skill-reviewer-cases.json', _ARCHIVE + 'T051-eval/sop-expanded-development-20260914/constraint-and-complexity-development-correct.json'])
            if skill == 'stack-and-queue' and category == 'risk':
                refs.extend(['tools/fixtures/skill-reviewer-cases.json', _ARCHIVE + 'T051-eval/sop-expanded-holdout-20260914/stack-and-queue-holdout-issue.json'])
            requirement = source['expected']['requirements'][0]['text']
            result.append(_case(len(result) + 1, 'development', skill, category,
                                data['userRequest']['text'],
                                [fact['text'] for fact in data['problemFacts']],
                                data['code']['text'], requirement, [requirement],
                                ['不得将未展示的整题步骤一律列为当前回答必须追加的任务。'],
                                history=history, sources=refs,
                                origin='documented-answer-problem' if observed else 'risk-probe' if category == 'risk' else 'reasonable-protection'))
    # T053 documented confusion: adjacent append/add is safe in this single thread.
    result[12] = _case(13, 'development', 'tree-and-graph', 'risk',
        '单线程里先入队紧接着标记，会让同一个站点重复入队吗？只检查这两句的顺序。',
        ['有限无向图可有环；seen 初始含起点；每次循环单线程顺序执行，中间没有回调、异常或其他入队操作。'],
        'for v in adj[u]:\n    if v not in seen:\n        queue.append(v)\n        seen.add(v)',
        '相邻 append/add 在下一次邻居检查前完成；不是等到出队才标记。',
        ['确认给定条件下不会因为这两句相邻顺序而重复入队，区分出队标记。'],
        ['不得制造并发窗口；不得误读成出队时才标记。'],
        sources=[_ARCHIVE + 'T053-routing/development-20260914/developer-facts.json',
                 _ARCHIVE + 'T053-routing/development-20260914/record-028.json',
                 _ARCHIVE + 'T053-routing/development-20260914/record-029.json'],
        origin='documented-answer-problem')
    result[13]['expected']['forbidden'].append('树保证无环时不得强制额外 visited；不得把父节点与父边适用边界混淆。')
    result[13]['expected']['forbidden'].append('不能把根parent避开所有真实编号说成必要条件；根用自身编号也可能正确。')
    result[13]['provenance']['sources'].extend([
        _ARCHIVE + 'T051-comparison/first-20260914/records/record-022.json',
        _ARCHIVE + 'T051-comparison/first-20260914/developer-fact-checks.json'])
    result[13]['provenance']['origin'] = 'documented-answer-problem-and-reasonable-protection'
    result[11]['inputs']['problemFacts'][0]['text'] = 'q是普通无界collections.deque；单线程先右端append成功，紧接左端popleft，中间没有其他操作。'
    result[11]['expected']['forbidden'].append('append 成功后紧接 popleft 且无中间操作，不得凭空担心此时队列仍为空。')
    result[9]['inputs']['problemFacts'][0]['text'] = '任务寻找能按期完成订单的最小机器速度；完整合法速度域是有限闭区间[left,right]内的正整数，所求答案已包含在该域，right已知可行；predicate(x)随速度增大由假变真。'
    result[5]['inputs']['problemFacts'][0]['text'] = '任务只判断某值此前是否出现，不要求保存下标或次数；seen在遍历开始前只初始化一次，下方其余操作是每个x的处理步骤。'
    result[5]['inputs']['code']['text'] = 'seen = set()  # 遍历开始前只初始化一次\n# 每个x依次执行：\nif x in seen: return True\nseen.add(x)'
    result[10]['provenance']['sources'].extend([
        _ARCHIVE + 'T051-comparison/first-20260914/records/record-011.json',
        _ARCHIVE + 'T051-提示对照结果.md'])
    result[10]['expected']['forbidden'].append('本次只问减法，不得强制检查未提供的除法、括号等无关分支。')
    return result


def _holdout():
    # Each triple uses different underlying questions from the development pair.
    rows = [
        ('problem-understanding', 'issue', '最后一节课在截止时间结束，我用这个判断会拒绝它吗？',
         ['课程允许恰好在截止时间结束；当前 end=deadline=17。'], 'allowed = end < deadline',
         '等号被排除，17<17 为 False；题意允许此情况。', '指出严格小于排除了允许的等号边界。', '不得把结束时刻必须提前当作题面要求。'),
        ('problem-understanding', 'reasonable', '匹配时忽略大小写，所以我先 lower 再比较，这点符合题意吗？',
         ['输入只含英文字母；题面要求忽略大小写比较两个用户名，不要求保存原拼写。'], 'same = a.lower() == b.lower()',
         '给定英文字母与忽略大小写约定支持该局部比较。', '肯定该比较符合当前约定。', '不得新增区分大小写或保留原拼写的要求。'),
        ('problem-understanding', 'simple', '这句题面中的“至多三次”包含三次吗？只解释这句话。',
         ['题面写：每位选手至多提交三次。'], '',
         '至多包含上界三。', '直接说明可以是零到三次，包含三次。', '不得扩展成提交次数算法或添加至少一次。'),
        ('constraint-and-complexity', 'issue', '这两个连续循环，我说总共平方次，对吗？',
         ['n 为非负整数；touch 每次常数成本；两个循环顺序执行，不嵌套。'],
         'for i in range(n): touch(i)\nfor j in range(n): touch(j)',
         '执行次数相加为2n，非n平方。', '说明两轮各n次，总2n，时间O(n)。', '不得把顺序循环当作嵌套。'),
        ('constraint-and-complexity', 'reasonable', 'i 每次翻倍，n=64 时这段循环跑六次，这样数对吗？',
         ['i 初始1，n=64，循环条件 i<n；只问循环次数。'], 'i = 1\nwhile i < n:\n    i *= 2',
         '循环入口依次1,2,4,8,16,32，共六次。', '确认六次并可列入口值，不凭此断言整程序耗时。', '不得把64计作第七次入口。'),
        ('constraint-and-complexity', 'simple', '只解释 range(4) 会给哪些整数。',
         ['这是Python中的range(4)，本次只问取值。'], 'for k in range(4): pass',
         'range(4)产生0到3。', '直接回答0、1、2、3。', '不得强制补规模时限或算法复杂度分析。'),
        ('array-and-hash', 'issue', '比较两个词是不是由相同字母各用相同次数组成，用集合够吗？',
         ['a="abb"，b="aab"；相同字母次数也必须一致。'], 'same = set(a) == set(b)',
         '两集合都是a,b，但b次数2与1不同。', '指出集合丢失次数，当前例误判为True。', '不得声称集合保留重复次数。'),
        ('array-and-hash', 'reasonable', '我统计当前窗口字符，次数减为零就删除键，再用 len 统计种类，可以吗？',
         ['freq在移除字符前准确且freq[ch]>=1；只统计仍在窗口中的字符种类。'],
         'freq[ch] -= 1\nif freq[ch] == 0:\n    del freq[ch]\nkinds = len(freq)',
         '删除零次键后键数等于仍存在的字符种类。', '肯定给定前提下的局部更新。', '不得要求保留零次键，也不声称整个窗口算法正确。'),
        ('array-and-hash', 'simple', '只解释 get(x, 0) 里的 0 起什么作用。',
         ['counts是Python字典；本次只问默认值。'], 'old = counts.get(x, 0)',
         '键不存在时返回0，get不写入新键。', '说明找不到键返回0。', '不得说这一行会把不存在的键插入字典。'),
        ('two-pointers-and-window', 'issue', '最长无重复片段里，遇到重复只左移一次够吗？看 abba 准备读第二个 b 这一步。',
         ['当前窗口是"ab"，准备读第二个b；left=0，right=2，s="abba"；freq初始a:1,b:1。'],
         'freq[s[right]] += 1\nif freq[s[right]] > 1:\n    freq[s[left]] -= 1\n    left += 1',
         '加b后移除a，窗口bb仍重复；一次收缩不足。', '定位移除a后b仍为2，需要检查恢复无重复条件。', '不得误算为已经去掉旧b或给出整题代码。'),
        ('two-pointers-and-window', 'reasonable', '判断回文时两端字符相同才一起向中间走，这一步可以吗？',
         ['s="level"，l=0,r=4；已经确认s[l]==s[r]；目标是检查字符串是否回文。'], 'l += 1\nr -= 1',
         '两端匹配后未检查区域从[0,4]缩到[1,3]。', '认可局部缩进并限定已给比较前提。', '不得强制换成滑动窗口或说整串已确认回文。'),
        ('two-pointers-and-window', 'simple', '只说明 s[l:r] 包含 r 位置吗？',
         ['s是Python字符串，0<=l<=r<=len(s)。'], 'part = s[l:r]',
         'Python切片不含右端点r。', '回答包含l、不包含r；l等于r时为空。', '不得要求展示完整指针算法。'),
        ('binary-search', 'issue', '空数组为什么第一步就报错？只看初始化和第一次访问。',
         ['a=[]；下面按闭区间条件left<=right继续；没有其他空数组分支。'],
         'left, right = 0, len(a)\nwhile left <= right:\n    mid = (left + right) // 2\n    value = a[mid]',
         '空数组时left=right=mid=0，进入循环访问a[0]越界；闭区间上界未对应有效最后下标。', '定位空数组仍进入循环并访问下标0。', '不得归因为区间更新方向或声称空数组存在第0个元素。'),
        ('binary-search', 'reasonable', '查询元素是否存在，找到相等的 mid 就退出，可以吗？不要求最左位置。',
         ['数组升序，可以重复；本题只返回是否存在，a[mid]==target已成立。'], 'if a[mid] == target:\n    return True',
         '存在性问题找到任意相等元素即可。', '确认可立即返回True，不要求继续找边界。', '不得新增最左/最右位置要求。'),
        ('binary-search', 'simple', '只把这一行 // 2 的意思说清楚。',
         ['lo和hi都是非负整数；代码是Python。'], 'mid = (lo + hi) // 2',
         '非负整数和除以2向下取整。', '说明取整数中点，奇数和向下取整。', '不得擅自诊断区间更新或要求提交完整程序。'),
        ('stack-and-queue', 'issue', '任务按到达先后处理，我从右边取，会先取谁？',
         ['q初始空，先加入"上午单"再加入"下午单"；必须先处理上午单。'],
         'q.append("上午单")\nq.append("下午单")\njob = q.pop()',
         '右端pop先取下午单，与到达先后不符。', '明确先取下午单，并定位两次append后的取出端。', '不得称右入右出为先进先出。'),
        ('stack-and-queue', 'reasonable', '这个撤销按钮用栈顶取最后一次操作合理吗？',
         ['history按发生先后append操作；撤销只要求取最近一次；用户已确认history非空。'], 'last_action = history.pop()',
         '后进先出与撤销最近操作一致，非空已知。', '肯定当前局部并保留实际撤销逻辑未展示的界限。', '不得将非空列为未知或要求改成队列。'),
        ('stack-and-queue', 'simple', '只解释 deque.appendleft(x) 会把 x 放在哪端。',
         ['q是Python collections.deque。'], 'q.appendleft(x)',
         'appendleft加入左端。', '直接说明添加到左端。', '不得无依据判定队列次序错误。'),
        ('tree-and-graph', 'issue', '仓库连线本来双向，从B出发却到不了A，先看建边漏了哪里。',
         ['唯一双向连接是A—B，两个方向均可通行；adj初始A:[],B:[]。'], 'adj["A"].append("B")',
         '只记录A到B，B邻接表仍为空。', '指出反向邻接未添加，与双向事实冲突。', '不得把此问题归为visited或假设其他代码已补边。'),
        ('tree-and-graph', 'reasonable', '我用递归返回左右子树大小再加1，这个局部计数符合定义吗？',
         ['有限二叉树无共享子节点；size(None)=0，左右递归已按相同定义返回节点数；当前root非空。'],
         'return 1 + size(root.left) + size(root.right)',
         '根贡献1，加上互不重叠的左右子树节点数。', '接受该局部等式，不强迫额外visited。', '不得把树节点数误算成边数，或虚构共享节点。'),
        ('tree-and-graph', 'simple', '这行字典访问 adj[u] 是取键还是取值？只解释Python语法。',
         ['adj是字典，键u已存在，值是一份邻居列表。'], 'neighbors = adj[u]',
         '获取键u对应的值。', '直接说明取得u对应的邻居列表。', '不得启动DFS/BFS诊断。'),
        ('dynamic-programming', 'issue', '最小路径费用全设为0，再取 min，这一格为什么没有变成7？',
         ['到达当前格的唯一前驱费用是4，进入当前格费用3；当前格尚不可达，dp[cur]初始0。'],
         'dp[cur] = min(dp[cur], dp[prev] + 3)',
         'min(0,4+3)=0，未到达的0错误胜过合法费用7。', '指出不可达初值被当成真实零费用，不给整题实现。', '不得声称当前值会变成7或强迫所有状态初值为0。'),
        ('dynamic-programming', 'reasonable', '求最长公共子序列时字符相等，用左上角加1，这一格合理吗？',
         ['dp[i][j]定义为两个前缀的最长公共子序列长度；i,j>=1；a[i-1]==b[j-1]；前面的状态已经正确计算。'],
         'dp[i][j] = dp[i-1][j-1] + 1',
         '末尾相同字符可扩展此前公共子序列，符合给定状态定义。', '肯定相等分支的局部转移，不替未展示的其他分支背书。', '不得强制把相等分支改成不相等分支的转移。'),
        ('dynamic-programming', 'simple', '只解释 [[0] * 2 for _ in range(3)] 的形状。',
         ['Python列表推导式；本次只问有几行几列。'], 'table = [[0] * 2 for _ in range(3)]',
         '三行两列，每行单独创建。', '回答3行2列，可说明共有6个零。', '不得展开状态转移课程或声称各行是同一个列表。'),
        ('testing-and-edge-cases', 'issue', '我说测过空串，可这条断言实际有调用被测函数吗？',
         ['唯一提供的测试是下方这条断言；目标检查score("")应返回0；没有运行结果。'], 'assert 0 == 0',
         '常量恒等式没有调用score，不能覆盖空串行为。', '指出断言不触及被测函数，区分写了断言与测到行为。', '不得认定已覆盖空串或已经运行通过。'),
        ('testing-and-edge-cases', 'reasonable', '题目要求不改传入列表，我在调用前复制再比较，这个测试方向合理吗？',
         ['nums=[3,1,4]仅含整数；total是当前待测函数，要求返回时传入列表内容和顺序不变；以下测试尚未运行。'],
         'before = nums.copy()\ntotal(nums)\nassert nums == before',
         '独立复制保留调用前内容，调用后列表值相等可检查此输入返回时内容及顺序不变；不证明调用中从未临时改动。',
         '认可保存调用前副本并比较的测试方向，限定此输入、返回时状态与尚未实测。',
         '不得说浅拷贝对整数列表必定无效，也不得声称证明所有输入或调用中从未修改。'),
        ('testing-and-edge-cases', 'simple', '只解释 assert actual == expected 在不相等时做什么。',
         ['Python正常启用断言，非-O模式。'], 'assert actual == expected',
         '条件假时抛AssertionError。', '直接说明不相等时抛出AssertionError。', '不得自行宣告某测试已失败或要求提交更多边界。'),
    ]
    return [_case(i, 'holdout', skill, category, request, facts, code, reason,
                  [acceptable], [forbidden], level=1 if category == 'simple' else 2,
                  origin='new-authored-scenario')
            for i, (skill, category, request, facts, code, reason, acceptable, forbidden)
            in enumerate(rows, 19)]


def get_cases(phase):
    """Return fresh canonical material; labels must not be passed to actors."""
    if phase not in ('development', 'holdout'):
        raise ValueError('phase must be development or holdout')
    return deepcopy(_development() if phase == 'development' else _holdout())


def get_reviewer_cases():
    """Six fixed fail/pass pairs. expectedVerdict never enters reviewer prompts."""
    development = _development()
    definitions = [
        (development[10], 'arithmetic',
         '先弹出3，再弹出8，所以first-second=3-8=-7，顺序反了。',
         '先弹出3，再弹出8，所以first-second=3-8=-5，顺序反了。'),
        (development[5], 'extra-unrelated-requirements',
         '集合可判断是否出现过，但必须同时保存每个值的下标和出现次数才算满足当前任务。',
         '集合满足只判断是否出现过的需求；无需额外保存下标和次数。'),
        (development[12], 'misread-order',
         '代码直到出队时才把v加入seen，因此其他父节点可能在标记前再次入队v。',
         '代码在append后立即add，给定单线程且无中间操作，下一次检查前已标记，不是出队才标记。'),
        (development[16], 'old-run-evidence',
         '对话说v1样例通过，因此当前v2已经实测通过，可以沿用这个结果。',
         '对话只是未核验的旧版本回忆，不能证明当前v2通过；当前没有运行记录。'),
        (_case(90, 'reviewer', 'tree-and-graph', 'calibration',
          '我要统计所有房间的连通分量，机器人在两个房间来回。现在只给我一个观察问题，不写实现。',
          ['任务统计有限无向图全部节点的连通分量，包括孤立节点；adj={0:[1],1:[0],2:[]}；从0开始。'],
          'def walk(u):\n    for v in adj[u]:\n        walk(v)\nwalk(0)',
          '当前只请求一个观察问题，完整分量程序超过L1范围。', ['询问为何会回到已进过的房间。'], ['不得给完整程序。'], level=1),
         'full-solution',
         '可以这样完成整题：\n```python\ndef count_components(adj):\n    seen = set()\n    count = 0\n    for start in adj:\n        if start in seen:\n            continue\n        count += 1\n        stack = [start]\n        seen.add(start)\n        while stack:\n            u = stack.pop()\n            for v in adj[u]:\n                if v not in seen:\n                    seen.add(v)\n                    stack.append(v)\n    return count\n```',
         '从房间0进入1后，下一次递归会去哪里，有没有状态记录那个房间已经进入过？'),
        (development[13], 'unsupported-requirement',
         '即使保证是无环树也必须使用visited集合，只有parent过滤一定会漏掉回边。',
         '输入保证为树时跳过父节点足以阻止沿来时边返回，不必额外强制visited。'),
    ]
    result = []
    for pair, (base, label, bad, good) in enumerate(definitions, 1):
        for verdict, summary in (('fail', bad), ('pass', good)):
            item = deepcopy(base)
            item['id'] = f'review-{len(result) + 1:03d}'
            item['pairId'] = f'pair-{pair:03d}'
            item['expectedVerdict'] = verdict
            item['calibrationKind'] = label
            item['analysis'] = {
                'applicability': {'status': 'applicable',
                                  'reason': '当前请求要求检查所给局部实现或材料的依据。',
                                  'evidenceRefs': ['request:1', 'code:v2']},
                'findings': [{'summary': summary,
                              'evidenceRefs': ['request:1', 'fact:1', 'code:v2',
                                               *[d['ref'] for d in item['dialogue']]]}],
                'unknowns': (['没有当前代码的运行记录，不能认定当前版本已验证。']
                             if label == 'old-run-evidence' else []),
            }
            item['provenance']['origin'] = 'fixed-authored-reviewer-calibration'
            result.append(item)
    return result
