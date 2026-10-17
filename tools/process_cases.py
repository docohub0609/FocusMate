"""Developer-authored facts; expectations stay outside model materials."""
from copy import deepcopy
from focusmate.core.context_builder import hash_content
from focusmate.core.action_planning import action_from_candidate


def development_cases():
    return [
      {'name':'状态解释','requirements':['说明dp[j]表示容量不超过j的最大价值'], 'explanation':'dp[j]表示容量不超过j时能得到的最大价值。','expected':['passed']},
      {'name':'解释错误','requirements':['说明dp[j]表示容量不超过j的最大价值'], 'explanation':'dp[j]是恰好装满j的方案数量。','expected':['failed']},
      {'name':'合法二维写法','requirements':['判断当前实现是否避免同一物品重复使用'], 'code':'def solve(items,W):\n    dp=[[0]*(W+1) for _ in range(len(items)+1)]\n    for i,(w,v) in enumerate(items,1):\n        for j in range(W+1):\n            dp[i][j]=dp[i-1][j]\n            if j>=w: dp[i][j]=max(dp[i][j],dp[i-1][j-w]+v)\n    return dp[-1][W]\n','explanation':'每行只读上一行，因此容量正序也可以。','expected':['passed']},
      {'name':'一维重复使用','requirements':['判断当前实现是否避免同一物品重复使用'], 'code':'def solve(items,W):\n    dp=[0]*(W+1)\n    for w,v in items:\n        for j in range(w,W+1):\n            dp[j]=max(dp[j],dp[j-w]+v)\n    return dp[W]\n','explanation':'我的写法每件物品最多使用一次。','expected':['failed']},
      {'name':'缺解释','requirements':['说明dp[j]表示容量不超过j的最大价值'],'explanation':'我改好了。','expected':['notReady']},
      {'name':'部分材料','requirements':['说明dp[j]的含义','说明初始化为何符合不要求装满的题意'],'explanation':'dp[j]表示容量不超过j的最大价值。','expected':['passed','notReady']},
      {'name':'缺外层条件','requirements':['判断完整更新过程是否避免同一物品重复使用'],'code':'dp[j] = max(dp[j], dp[j-w]+v)\n','explanation':'只提供这一句，外层循环没有提交。','expected':['notReady','inconclusive'],'allowedAlternatives':True},
      {'name':'读写区分','requirements':['准确说明给定循环对dp[0]是读取还是写入；验证说明是否准确，不要求必须写入dp[0]'],'code':'dp=[0]*4\nw=3\nv=5\nfor j in range(3,w-1,-1):\n    dp[j]=max(dp[j],dp[j-w]+v)\n','explanation':'当前只读取dp[0]，不会改写dp[0]。','expected':['passed']},
      {'name':'短路解释','requirements':['解释空列表输入为何不会访问第一个元素'],'code':'values=[]\nif values and values[0]>0:\n    print(1)\n','explanation':'values为空，and右边不执行。','expected':['passed']},
      {'name':'公开样例','objective':'读入一个整数，输出它加一的值。','requirements':['当前代码通过已登记的公开样例'], 'code':'x=int(input())\nprint(x+1)\n','explanation':'请检查已登记样例。','tools':['run_public_samples'],'samples':[{'input':'2\n','expectedOutput':'3\n'}],'expected':['passed']},
      {'name':'局部观察','requirements':['在本次指定初值x=2下运行这句，观察x是否变为3，仅判断此实验'],'code':'x += 1\n','explanation':'请以x=2运行这一句并观察x。','tools':['run_local_code'],'expected':['passed']},
      {'name':'公开反例','objective':'读入一个整数，输出它加一的值。','requirements':['当前代码通过已登记的公开样例'],'code':'x=int(input())\nprint(x+2)\n','explanation':'请检查已登记样例。','tools':['run_public_samples'],'samples':[{'input':'2\n','expectedOutput':'3\n'}],'expected':['failed']},
    ]


def fresh_cases():
    return [
      {'name':'新状态解释','objective':'求序列前缀和。','requirements':['说明prefix[k]表示前k个元素之和'],'explanation':'prefix[k]保存原序列索引0到k-1的元素总和。','expected':['passed']},
      {'name':'新解释矛盾','objective':'求序列前缀和。','requirements':['说明prefix[k]表示前k个元素之和'],'explanation':'prefix[k]表示前k个元素中的最大值。','expected':['failed']},
      {'name':'合法递归替代','objective':'在0/1背包中每件物品最多选择一次。','requirements':['当前递归是否避免重复选择同一物品'],'code':'def best(items,i,c):\n    if i==len(items): return 0\n    w,v=items[i]\n    skip=best(items,i+1,c)\n    return max(skip,v+best(items,i+1,c-w)) if w<=c else skip\n','explanation':'选或不选都递归到i+1，不会再处理当前物品。','expected':['passed']},
      {'name':'递归反例','objective':'在0/1背包中每件物品最多选择一次，重量为正。','requirements':['当前递归是否避免重复选择同一物品'],'code':'def best(items,i,c):\n    if i==len(items): return 0\n    w,v=items[i]\n    skip=best(items,i+1,c)\n    return max(skip,v+best(items,i,c-w)) if w<=c else skip\n','explanation':'选择分支里没有增加i。','expected':['failed']},
      {'name':'新缺材料','objective':'解释一个二分查找的边界约定。','requirements':['说明当前代码的搜索区间是闭区间还是半开区间'],'explanation':'已经写好了，可以验收了。','expected':['notReady']},
      {'name':'新部分材料','objective':'说明二维状态表。','requirements':['说明行索引表示已考虑的物品数','说明列索引代表什么'],'explanation':'行i表示已经考虑前i件物品。','expected':['passed','notReady']},
      {'name':'新不完整范围','objective':'检查递归遍历是否终止。','requirements':['说明当前完整遍历在无向图上是否能够终止'],'code':'visit(next_node)\n','explanation':'这里只给递归调用，没有给visit函数和访问记录。','expected':['notReady','inconclusive'],'allowedAlternatives':True},
      {'name':'空循环','objective':'检查空输入行为。','requirements':['说明rows为空列表时这段代码是否访问rows[0]'],'code':'rows=[]\nfor row in rows:\n    for j in range(len(rows[0])):\n        print(row[j])\n','explanation':'外层循环不进入，因此不会执行rows[0]。','expected':['passed']},
      {'name':'覆盖不是首次','objective':'说明字典保存的信息。','requirements':['准确说明重复键赋值后pos保存哪个位置'],'code':'pos={}\nfor i,x in enumerate([4,9,4]):\n    pos[x]=i\n','explanation':'每次赋值都会覆盖，pos保存每个值最后一次出现的位置。','expected':['passed']},
      {'name':'新公开样例','objective':'读入整数，输出其平方。','requirements':['当前代码通过已登记公开样例'],'code':'n=int(input())\nprint(n*n)\n','explanation':'请运行公开样例。','tools':['run_public_samples'],'samples':[{'input':'4\n','expectedOutput':'16\n'}],'expected':['passed']},
      {'name':'新局部实验','objective':'检查列表局部修改。','requirements':['在本次values=[2,4]的初值下观察append后是否为[2,4,6]，仅限此实验'],'code':'values.append(6)\n','explanation':'请以values=[2,4]运行这一句并观察values。','tools':['run_local_code'],'expected':['passed']},
      {'name':'新公开反例','objective':'读入整数，输出其平方。','requirements':['当前代码通过已登记公开样例'],'code':'n=int(input())\nprint(n+n)\n','explanation':'请运行公开样例。','tools':['run_public_samples'],'samples':[{'input':'4\n','expectedOutput':'16\n'}],'expected':['failed']},
    ]


def materials(case):
    context={'goalId':'g1','scope':'goal'};problem={'problemId':'p1','problemVersion':1}
    contract={'schemaVersion':'1.0.0','goalId':'g1','version':1,'createdAt':'2026-09-24T00:00:00Z',
        'problemRef':problem,'objective':case.get('objective','0/1背包：物品重量为正，每件最多一次，容量非负，不要求装满，求最大价值。'),
        'subgoals':[{'subgoalId':'s1','stage':'coreStateAndCorrectness','objective':'检查下列具体要求',
                    'dependsOn':[],'acceptableEvidence':['evaluatedUserExplanation','codeStructureCheck','publicSampleRunResult','targetedRunResult']}],
        'navigationPolicy':{'mode':'adaptive','stageOrder':'defaultOnly','allowSkip':True,'allowRevisit':True,
            'offRouteHandling':'temporaryBranch','stalledRouteFallback':'latestVerifiedSubgoal','processStagesRequiredForCompletion':False},
        'finalCriterion':{'verifierType':'finalOj','requiredVerdict':'AC'},'coachPolicy':{'completeSolutionAllowed':False},'contractStatus':'frozen'}
    compilation={'contract':contract,'rubrics':[{'subgoalId':'s1','checks':[{'check':s} for s in case['requirements']]}]}
    def record(ref,kind,content):return {'ref':ref,'kind':kind,'version':1,'goalContext':deepcopy(context),
        'problemRef':problem,'content':content,'contentHash':hash_content(content),'origin':'developerFixture'}
    records=[record('contract','goal',compilation),record('message','dialogue',{'ref':'message','role':'user','text':case['explanation']})]
    if case.get('code') is not None:records.append(record('code','code',{'snapshotRef':'code','language':'python3','text':case['code']}))
    code='code' if case.get('code') is not None else None
    req={'schemaVersion':'1.0.0','goalContext':{'goalId':'g1','scope':'subgoal','subgoalId':'s1'},'problemRef':problem,
        'contractVersion':1,'contractRef':'contract','actionId':'a1','currentCodeRef':code,
        'materialRefs':[r['ref'] for r in records if r['kind']!='goal'],
        'sources':[{k:r[k] for k in ('ref','kind','version','contentHash')} for r in records],
        'budget':{'contextWindowTokens':24000,'reservedOutputTokens':4096,'safetyMarginTokens':1000}}
    candidate={'questionKeys':['q1'],'subgoalIds':['s1'],'actionType':'reflectionQuestion','hintLevel':2,'answerScope':'guided',
        'reason':'检查用户提交的材料','evidenceRefs':['contract'],'requestQuotes':[],'content':'请对照目标要求提交解释或代码。',
        'diagnosisCorrections':[],'followUp':{'kind':'awaitEvidence','reason':'等待材料后验证','criterionIds':['s1'],
             'checks':['userExplanation','codeStructure','runEvidence'],'expectedUserAction':'提交相关材料'}}
    action=action_from_candidate(candidate,{'request':{'goalContext':context,'contractVersion':1,'userRequestRef':'message','currentCodeRef':code}},
        {'diagnosisId':'diagnosis-fixture'},action_id='a1',created_at='2026-09-24T00:00:00Z')
    return req,records,action
