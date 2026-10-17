"""Static, development-only evaluation cases for the nine basic Skills."""


PROVENANCE = {
    'draftedBy': 'AI',
    'independentlyReviewed': False,
    'humanReviewed': False,
}


def _case(skill, split, category, family, request, goal, facts, code='', runs=(), requirement=None):
    prefix = f'{skill}-{split}-{category}'
    inputs = {
        'userRequest': {'ref': 'request', 'text': request},
        'goalRequirement': {'ref': 'goal', 'text': goal},
        'problemFacts': [
            {'ref': f'fact:{index}', 'text': text}
            for index, text in enumerate(facts, 1)
        ],
        'code': {'snapshotRef': 'code:v2', 'text': code},
        'runEvidence': [
            {'ref': f'run:{index}', 'text': text, 'codeSnapshotRef': snapshot}
            for index, (text, snapshot) in enumerate(runs, 1)
        ],
    }
    if category == 'notApplicable':
        evidence = [inputs['userRequest']['ref']]
        requirement = requirement or '说明当前请求不是该 Skill 的分析任务，不强行给出相关判断'
    else:
        evidence = [inputs['code']['snapshotRef'], *[item['ref'] for item in inputs['problemFacts']]]
        if runs:
            evidence.extend(item['ref'] for item in inputs['runEvidence'])
    return {
        'id': prefix,
        'split': split,
        'problemFamily': family,
        'goalContext': {
            'goalId': f'eval-{skill}', 'scope': 'subgoal',
            'subgoalId': family,
        },
        'skillId': skill,
        'materialVersion': 1,
        'inputs': inputs,
        'expected': {
            'category': category,
            'requirements': [{
                'id': 'expected-response', 'text': requirement,
                'evidenceRefs': evidence,
            }],
        },
        'provenance': {
            **PROVENANCE,
            'representativeScaleApproved': split == 'development' and category == 'issue',
        },
    }


def build_cases():
    """Return 54 independent cases: 9 Skills × 2 splits × 3 categories."""
    rows = [
        # problem-understanding — development issue is approved representative card 1.
        ('problem-understanding','development','issue','return-indices','我返回这两个数就行吗？','核对返回对象与限制',['题面要求返回两个不同位置的下标；两个位置的数值可以相同。'],'',(), '指出应返回下标且位置必须不同；不要把数值相同误判为不合法，也不要给完整算法'),
        ('problem-understanding','development','correct','empty-result-contract','我理解为找不到时返回空列表，对吗？','核对无解时的返回约定',['题面明确规定不存在答案时返回空列表；代码片段仅展示已经确定无解的分支。'],'return []',(), '确认此无解分支的返回与题面约定一致，不推断其他分支正确，并引用题面'),
        ('problem-understanding','development','notApplicable','rename-function','请把函数名改成 solve_pair。','只处理当前请求',['题面讨论返回两个下标。'],'def answer(nums):\n    pass'),
        ('problem-understanding','holdout','issue','schedule-output-order','活动日程是不是可以随便排列输出？','核对输出顺序',['题面要求所有活动按开始时间从早到晚输出；开始时间互不相同。'],'',(), '指出活动输出顺序有明确约束，不能说任意顺序都可以'),
        ('problem-understanding','holdout','correct','inclusive-range','我用了闭区间 [1,n]，和题意一致吗？','核对取值范围',['题面规定有效编号为 1 到 n，包含两个端点。'],'for x in range(1, n + 1):\n    use(x)',(), '确认循环覆盖 1 到 n 两端，与题面一致'),
        ('problem-understanding','holdout','notApplicable','save-file','请只告诉我这个文件应该保存成什么名字。','只处理文件命名请求',['提交平台要求文件名为 main.py。'],'print(1)'),

        # constraint-and-complexity — approved card 2; issue is unsupported certainty.
        ('constraint-and-complexity','development','issue','quadratic-without-limits','这段双层遍历能直接认定超时吗？','判断复杂度结论是否有依据',['代码对每一对元素检查一次；材料没有数据规模和时间限制。'],'for i in range(n):\n    for j in range(i + 1, n): check(i, j)',(), '说明检查次数是平方级；因缺少规模与时限，不能直接断言超时'),
        ('constraint-and-complexity','development','correct','bounded-enumeration','n 最多 30，这个枚举次数可以这样估算吗？','核对枚举上界',['n 不超过 30；循环检查所有 i<j 的组合。'],'for i in range(n):\n    for j in range(i + 1, n): check(i, j)',(), '确认最多检查 435 对，结论由给定上界支持'),
        ('constraint-and-complexity','development','notApplicable','format-output','请把输出改成每个数字一行。','只处理输出格式请求',['输出格式要求每个答案独占一行。'],'print(*answer)'),
        ('constraint-and-complexity','holdout','issue','recursion-depth-missing-limit','这个递归一定会爆栈吗？','依据约束判断递归深度',['k 是非负整数；递归每次将 k 减一；材料未给 k 的最大值或运行环境栈限制。'],'def f(k):\n    return 0 if k == 0 else f(k - 1)',(), '说明深度随 k 线性增长，但缺少上界和环境限制，不能断言一定爆栈'),
        ('constraint-and-complexity','holdout','correct','linear-memory-bound','这个 visited 保存 n 个元素，辅助空间是 O(n)，对吧？','核对辅助空间',['n 是非负整数；visited 为长度 n 的 Python 列表，不保存其他随 n 增长的结构。'],'visited = [False] * n',(), '确认元素数量和该结构的辅助空间随 n 线性增长；不声称 Python 列表只占 n 个比特'),
        ('constraint-and-complexity','holdout','notApplicable','syntax-colon','帮我看这里是不是少了冒号。','只回答语法请求',['当前请求只问 Python 语法。'],'for x in items\n    use(x)'),

        # array-and-hash — approved card 3.
        ('array-and-hash','development','issue','self-pair-after-insert','当前写法会重复使用同一位置吗？','检查哈希匹配的位置约束',['允许两个数值相等，但不能重复使用同一位置；target 可能等于 2*x。'],'seen[x] = i\nif target - x in seen:\n    return [seen[target - x], i]',(), '指出先记录当前位置会在 target=2*x 时让当前位置与自身配对，提示检查查询与记录顺序'),
        ('array-and-hash','development','correct','count-duplicates','这个计数表能保留重复次数吗？','核对重复元素计数',['需要记录每个值出现的次数。'],'counts[x] = counts.get(x, 0) + 1',(), '确认当前更新会累计每个值的出现次数'),
        ('array-and-hash','development','notApplicable','rename-map','请把变量 seen 改名为 positions。','只处理变量命名请求',['当前请求仅是重命名。'],'seen = {}'),
        ('array-and-hash','holdout','issue','overwrite-first-index','题目要最早位置，我这样存行吗？','检查下标保留策略',['同一数值出现多次时必须保留第一次出现的下标。'],'for i, x in enumerate(nums):\n    pos[x] = i',(), '指出无条件赋值会覆盖第一次下标，与题面要求冲突'),
        ('array-and-hash','holdout','correct','set-membership-alternative','我用集合判断是否出现过，不用字典也可以吗？','判断替代数据结构是否满足需求',['任务只要求判断某值是否出现过，不需要保存下标或次数。'],'seen = set()\nif x in seen: return True\nseen.add(x)',(), '确认集合是满足当前只做成员判断的有效替代方案，不强迫改用字典'),
        ('array-and-hash','holdout','notApplicable','print-spacing','请只把打印结果之间改成逗号。','只处理打印格式请求',['当前请求不要求分析数组或哈希逻辑。'],'print(*values)'),

        # two-pointers-and-window — approved card 4.
        ('two-pointers-and-window','development','issue','negative-window-monotonicity','和大于目标就移动左端可靠吗？','检查窗口收缩依据',['数组允许负数；移除负数会使窗口和增大。'],'while total > target:\n    total -= nums[left]\n    left += 1',(), '指出负数破坏该收缩规则依赖的单调性，不能直接认定可靠'),
        ('two-pointers-and-window','development','correct','sorted-two-sum','有序数组里两端指针这样移动可以吗？','核对指针移动依据',['数组升序；目标是找和等于 target 的一对。'],'s = a[l] + a[r]\nif s < target: l += 1\nelif s > target: r -= 1',(), '确认升序条件支持按和的大小移动对应端点'),
        ('two-pointers-and-window','development','notApplicable','function-name-window','请把函数名改成 longest_segment。','只处理函数命名请求',['当前请求不要求判断窗口逻辑。'],'def window(a): pass'),
        ('two-pointers-and-window','holdout','issue','duplicate-skip-out-of-bounds','跳过重复值这段安全吗？','检查指针边界',['right 初始可等于数组最后下标；重复元素可能一直延伸到数组末尾。'],'while nums[right] == nums[right + 1]:\n    right += 1',(), '指出读取 right+1 前没有边界保护，末尾重复值可导致越界'),
        ('two-pointers-and-window','holdout','correct','fixed-window-update','固定长度窗口这样更新和可以吗？','核对窗口更新',['窗口长度固定为 k；每步加入 nums[r] 并移除 nums[r-k]。'],'total += nums[r]\nif r >= k: total -= nums[r-k]',(), '确认在形成后续窗口时一进一出保持长度 k，当前更新逻辑成立'),
        ('two-pointers-and-window','holdout','notApplicable','add-comment-window','帮我只给这行加一句注释。','只处理注释请求',['当前请求没有要求检查算法。'],'left += 1'),

        # binary-search — approved card 5.
        ('binary-search','development','issue','non-shrinking-left-bound','这个循环为什么可能不结束？','检查区间是否严格缩小',['闭区间满足 right=left+1 时，向下取整的 mid 等于 left。'],'while left < right:\n    mid = (left + right) // 2\n    if ok(mid): left = mid',(), '指出 right=left+1 时该分支令 left 保持不变，区间没有缩小；不要声称已运行复现'),
        ('binary-search','development','correct','half-open-lower-bound','这个找第一个不小于 target 的写法对吗？','核对半开区间不变量',['搜索区间是 [left,right)，数组升序。'],'while left < right:\n    mid=(left+right)//2\n    if a[mid] < target: left=mid+1\n    else: right=mid',(), '确认两个分支均缩小半开区间并保留第一个不小于 target 的候选'),
        ('binary-search','development','notApplicable','rename-mid','请只把 mid 改名为 middle。','只处理变量命名请求',['当前请求只要求重命名。'],'mid = (left + right) // 2'),
        ('binary-search','holdout','issue','cable-cutting-threshold','切绳长度不可行时这样更新会漏答案吗？','检查答案空间边界方向',['寻找可切出至少 k 段的最大正整数长度；can_cut(x) 随长度增大由真变假；当前闭区间含有可行长度。'],'if not can_cut(mid):\n    left = mid + 1',(), '指出长度 mid 已不可行时更大的长度也不可行，这个分支舍弃左侧可能的可行长度，方向错误'),
        ('binary-search','holdout','correct','answer-space-monotone','答案二分不用在数组里找也行吗？','判断替代二分对象',['任务为寻找能按期完成订单的最小机器速度；正整数速度在有限闭区间 [left,right] 内，right 已知可行；predicate(x) 随速度增大由假变真。'],'while left < right:\n    mid = (left + right) // 2\n    if predicate(mid): right=mid\n    else: left=mid+1',(), '确认给定边界和单调可行性支持答案空间二分，不要求答案来自数组；不宣称未提供的 predicate 实现正确'),
        ('binary-search','holdout','notApplicable','missing-parenthesis','帮我只检查这一行的括号。','只回答语法请求',['当前请求不涉及搜索正确性。'],'mid = (left + right // 2'),

        # stack-and-queue — approved card 6.
        ('stack-and-queue','development','issue','pop-empty-stack','遇到右括号直接弹栈安全吗？','检查空栈边界',['输入不保证合法，首字符可能是右括号。'],'if ch in closing:\n    top = stack.pop()',(), '指出首字符为右括号时可能从空栈弹出，应先检查弹出条件'),
        ('stack-and-queue','development','correct','fifo-deque','这样能保持先进先出吗？','核对队列次序',['元素从右端加入，从左端取出。'],'q.append(x)\nnext_item = q.popleft()',(), '确认右端入队、左端出队保持先进先出'),
        ('stack-and-queue','development','notApplicable','rename-stack','请把 stack 改名成 pending。','只处理变量命名请求',['当前请求不要求检查栈行为。'],'stack = []'),
        ('stack-and-queue','holdout','issue','postfix-subtraction-order','后缀表达式的减法顺序这样对吗？','检查栈中操作数顺序',['后缀表达式 8 3 - 的结果应为 5；处理减号前栈从底到顶为 [8,3]，弹出从栈顶取。'],'first = stack.pop()\nsecond = stack.pop()\nstack.append(first - second)',(), '指出先弹出的是右操作数，当前 first-second 把减法顺序颠倒；只分析此局部，不给完整求值程序'),
        ('stack-and-queue','holdout','correct','monotonic-stack-equal','求严格更大元素时弹出小于等于当前值合理吗？','核对单调栈等值处理',['目标是左侧最近的严格更大元素。'],'while stack and stack[-1] <= x:\n    stack.pop()',(), '确认弹出小于等于当前值后，留下的栈顶才可能是严格更大值'),
        ('stack-and-queue','holdout','notApplicable','change-quotes','请只把字符串的单引号换成双引号。','只处理格式请求',['当前请求没有询问栈或队列行为。'],"stack.append('(')"),

        # tree-and-graph — approved card 7.
        ('tree-and-graph','development','issue','cycle-without-visited','递归遍历为什么可能停不下来？','检查图遍历终止条件',['无向图允许环，一条边的两个端点会互为邻居。'],'def visit(u):\n    for v in adj[u]:\n        visit(v)',(), '指出缺少重复访问控制时相邻节点可相互递归，提示检查终止条件'),
        ('tree-and-graph','development','correct','tree-parent-guard','树上递归只跳过父节点够吗？','核对树遍历防回边',['输入保证为连通无环树；递归参数 parent 是当前节点的父节点。'],'def dfs(u, parent):\n    for v in adj[u]:\n        if v != parent: dfs(v, u)',(), '确认在无环树的保证下跳过父节点足以避免沿边立即返回'),
        ('tree-and-graph','development','notApplicable','rename-node','请把参数 u 改名为 node。','只处理参数命名请求',['当前请求不要求检查遍历。'],'def dfs(u): pass'),
        ('tree-and-graph','holdout','issue','weighted-road-cost','道路总费用直接统计经过几条边可以吗？','核对图中边的含义',['道路边权是实际通行费用且可以不同；要求给定路线的总费用，不是经过的道路数。'],'cost = len(path) - 1',(), '指出边数不能表示不同边权的总费用，需检查是否使用路线对应的费用信息；不强制改用某种最短路算法'),
        ('tree-and-graph','holdout','correct','topological-kahn','不用递归，按入度做拓扑排序可以吗？','判断有效替代算法',['输入为有向无环图；需要输出任一拓扑序；indeg 初始正确，每取出零入度节点即输出、降低后继入度，并把新变为零入度的节点入队直到队列空。'],'q=deque(v for v in nodes if indeg[v]==0)\n# 后续按题目事实中描述的完整处理步骤执行',(), '确认所描述的零入度处理方法满足拓扑序需求；代码未完整展示，不能宣布完整实现已验证'),
        ('tree-and-graph','holdout','notApplicable','print-tree-label','请只把输出标签改成 Tree。','只处理显示文本请求',['当前请求不要求分析图结构。'],"print('Graph')"),

        # dynamic-programming — approved card 8.
        ('dynamic-programming','development','issue','forward-01-update','这样从小容量更新会重复用物品吗？','检查 0/1 状态转移',['0/1 背包中每个物品最多使用一次；一维状态原地更新。'],'for cap in range(w, C + 1):\n    dp[cap] = max(dp[cap], dp[cap-w] + value)',(), '指出大容量可能读取本轮刚更新的小容量，使同一物品在本轮重复参与'),
        ('dynamic-programming','development','correct','rolling-fibonacci','只保留前两个状态可以吗？','核对状态依赖',['每个新状态只依赖紧邻的前两个状态。'],'prev2, prev1 = 0, 1\nfor _ in range(2, n+1):\n    prev2, prev1 = prev1, prev1 + prev2',(), '确认依赖范围只有前两个状态，因此滚动保存满足当前转移'),
        ('dynamic-programming','development','notApplicable','rename-dp','请把 dp 改名为 best。','只处理变量命名请求',['当前请求不要求检查状态转移。'],'dp = [0] * n'),
        ('dynamic-programming','holdout','issue','missing-base-case','为什么 n=0 得不到题面答案？','检查初始状态',['题面规定空序列的方案数为 1。'],'dp = [0] * (n + 1)\nfor i in range(1, n + 1):\n    dp[i] = sum(dp[:i])\nreturn dp[n]',(), '指出 dp[0] 保持为 0，与题面给定的空序列基础状态冲突'),
        ('dynamic-programming','holdout','correct','memoized-alternative','我用记忆化搜索而不是表格可以吗？','判断有效替代实现',['任务为从位置 0 到 n 的最小分段费用；子问题仅由位置 i 确定；0<=i<=n，i<n 时 nexts(i) 非空且每个 j 满足 i<j<=n，cost 固定；到达 n 后剩余费用为 0。'],'@cache\ndef solve(i):\n    if i == n: return 0\n    return min(cost(i,j)+solve(j) for j in nexts(i))',(), '确认在严格前进的状态和给定费用转移下，记忆化搜索是有效替代实现；不必强迫改成表格'),
        ('dynamic-programming','holdout','notApplicable','add-type-hint','帮我只给返回值加 int 类型标注。','只处理类型标注请求',['当前请求没有询问动态规划逻辑。'],'def solve(n):\n    return 0'),

        # testing-and-edge-cases — approved card 9. The old run is explicitly user-reported.
        ('testing-and-edge-cases','development','issue','stale-version-evidence','这份新代码是不是已经验证好了？','核对证据与代码版本',['当前代码是 v2；没有提供 v2 的运行结果。'],'def solve_v2(data):\n    return revised(data)',(('用户报告：之前对代码 v1 跑过一组样例并显示通过。','code:v1'),), '明确用户报告的 v1 旧记录不能证明 v2 已验证，建议检查当前版本；不把旧记录写成真实执行事实'),
        ('testing-and-edge-cases','development','correct','empty-input-covered','测试代码已经包含空输入边界了吗？','核对边界证据',['要求空输入返回 0；提供的断言直接调用当前函数；没有运行记录。'],'def count(items):\n    if not items: return 0\n    return len(items)\nassert count([]) == 0',(), '确认测试代码包含空输入及预期值的断言；仅说明已写入测试，不声称实际执行通过'),
        ('testing-and-edge-cases','development','notApplicable','save-test-name','请只告诉我测试文件应该叫什么名字。','只处理文件命名请求',['项目约定测试文件名以 test_ 开头。'],'def solve(): pass'),
        ('testing-and-edge-cases','holdout','issue','missing-limit-boundary','这些样例足够覆盖边界了吗？','检查题面限制对应的边界',['题面允许 n=1 和 n=1000；现有样例只有 n=2、3、10。'],'def solve(values):\n    return process(values)',(), '指出现有材料未覆盖最小值 n=1 与最大值 n=1000，不能据此认定边界已覆盖'),
        ('testing-and-edge-cases','holdout','correct','property-test-alternative','不用枚举所有排列，检查这个性质可以吗？','判断有效替代测试策略',['函数输出应与输入排列无关；测试对同一多重集合生成多种排列并比较结果。'],'expected = solve(base)\nfor p in permutations_sample(base):\n    assert solve(p) == expected',(), '确认该性质直接对应排列不变要求，是有效替代测试策略；不声称测试已执行'),
        ('testing-and-edge-cases','holdout','notApplicable','fix-test-indent','帮我只修正这段测试的缩进。','只处理语法格式请求',['当前请求不要求判断测试覆盖或运行结果。'],'def test_x():\nassert solve([]) == 0'),
    ]
    return [_case(*row) for row in rows]
