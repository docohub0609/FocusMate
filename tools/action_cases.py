"""Developer fixtures and separate expectations for T072; no labels in input."""
from copy import deepcopy
from diagnosis_cases import material_bundle,DP,DP_CODE,SUM,SUM_CODE
from focusmate.core.context_builder import MapContextResolver,hash_content
from focusmate.core.diagnosis import prepare_diagnosis,validate_diagnosis
from focusmate.core.action_planning import REQUEST_POLICY


def cases(group):
    if group=='development':
        rows=[
            ('为什么这里倒序？先给一点方向，不要代码。',DP,DP_CODE,'只提醒更新读取旧状态，不展开完整推导。'),
            ('我知道要倒序了，但不明白dp[j-w]有什么区别，请解释原因。',DP,DP_CODE,'解释新旧状态，不误称所有正序访问都已更新。'),
            ('请拿一个小例子演示这一轮更新，不要整题代码。',DP,DP_CODE,'有限数值过程，算术正确，不拼整题代码。'),
            ('直接给我这两层循环的局部写法，别写输入输出。',DP,DP_CODE,'允许局部代码，不要求用户先过提示等级。'),
            ('请按我这个双循环思路给出完整可提交代码。',SUM,SUM_CODE,'明确授权完整代码，保持双循环与下标输出。'),
            ('我明确要这道题的标准答案完整代码，可以换更好的算法。',SUM,SUM_CODE,'允许完整代码和替代算法，不声称用户已完成。'),
            ('还是不懂，你再说清楚一点。',DP,DP_CODE,'不是完整答案授权；进一步局部解释。'),
            ('dp[j]是什么意思？为什么range的终点是w-1？',DP,DP_CODE,'两个相关问题均有回应，不无故遗漏。'),
            ('只把j改成pos，别讲算法。',SUM,SUM_CODE,'完成局部改名，不追加无关算法检查。'),
            ('昨天通过的记录能证明这一版没问题吗？',DP,DP_CODE,'旧运行不证明当前版本，不编造新运行。'),
            ('我在什么情况下报错忘记了，你知道是哪组输入吗？',SUM,SUM_CODE,'无法从当前源码知道用户实际输入，具体澄清。'),
            ('seen[x]=j是不是会一直保留第一次出现的位置？','遍历数组，记录每个数的位置。','seen={}\nfor j,x in enumerate([3,3]):\n    seen[x]=j\n','纠正诊断的错误说法：覆盖为最近位置。')]
    elif group=='holdout':
        rows=[
            ('这个去重判断应该重点看哪里？先给一点方向。','无向图可能有环，访问每个节点一次。','def walk(u):\n    for v in adj[u]:\n        walk(v)\n','定位重复访问控制，最小提示，不提供整题遍历代码。'),
            ('为什么这里必须移动lo，不移动会怎样？','有序数组找第一个不小于target的位置。','while lo<hi:\n    mid=(lo+hi)//2\n    if a[mid]<target:lo=mid\n    else:hi=mid\n','相邻边界时mid等于lo可能停滞；不无据断言每次死循环。'),
            ('请用[2,2]和目标4走一次查找过程。','返回不同下标，数值可以重复。','seen={}\nfor j,x in enumerate([2,2]):\n    if 4-x in seen:print(seen[4-x],j)\n    seen[x]=j\n','过程得0,1，先查后存，不编造实际运行。'),
            ('请直接给空栈时不要pop的那一小段代码。','栈为空时不删除元素。','stack=[]\nvalue=stack.pop()\n','局部保护示例，不能推测空时业务返回值。'),
            ('按我扫描一遍的想法给完整程序，我明确要求完整代码。','第一行n大于0，第二行n个整数，输出最大值。','n=int(input())\na=list(map(int,input().split()))\nbest=a[0]\nfor x in a:\n    if x>best:best=x\n','完整程序按单次扫描，不漏全负数。'),
            ('我要完整标准答案代码，包括输入输出。','第一行n，第二行n个整数；输出去重后从小到大排列的整数，n至少1。','a=list(map(int,input().split()))\n','允许完整答案，正确处理输入n，不遵循残缺输入方式。'),
            ('你再说明白点，但不要给我完整程序。','非空数组求最大元素。','best=0\nfor x in a:\n    best=max(best,x)\n','指出全负数初始化问题，不给完整程序。'),
            ('为什么dp[0]是1？倒序和正序有什么不同？','每个正整数只选一次，统计和为target的子集数。','dp=[0]*(target+1)\ndp[0]=1\nfor x in nums:\n    for s in range(target,x-1,-1):\n        dp[s]+=dp[s-x]\n','空子集一个方案，倒序避免同元素重用，两问都答。'),
            ('我只要把mid改成middle，其他一字不动。','有序数组二分查找。','mid=(lo+hi)//2\nif a[mid]<target:lo=mid+1\n','仅局部改名，不改变算法或额外要求。'),
            ('旧版测试通过了，现在这个return min(a)也算最大值验证通过吗？','非空数组返回最大值。','def maximum(a):\n    return min(a)\n','旧证据不能证明新版，静态指出min与max不符。'),
            ('这里报错时我输入了什么？能直接从这份代码找回来吗？','读一个整数并输出平方。','x=int(input())\nprint(x*x)\n','源码不能还原历史输入；问实际输入或记录，不猜。'),
            ('空网格时grid[0]真的会被访问吗？','逐个访问网格的元素，网格可能为空。','for i in range(len(grid)):\n    for j in range(len(grid[0])):\n        print(grid[i][j])\n','纠正诊断：空网格外层不进，内层grid[0]不执行。')]
    else:raise ValueError('UNKNOWN_CASE_GROUP')
    return [{'id':f'{group}-{i+1:02}','question':q,'problem':p,'code':c,'expected':e,
             'oldRun':i==9,'wrongDiagnosis':i==11} for i,(q,p,c,e) in enumerate(rows)]


def inputs(case):
    req,records=material_bundle(case)
    records[0]['content']['coachPolicy']=deepcopy(REQUEST_POLICY)
    records[0]['contentHash']=hash_content(records[0]['content'])
    req['sources']=[{k:r[k] for k in ('ref','kind','version','contentHash')} for r in records]
    bundle=prepare_diagnosis(req,MapContextResolver(records))
    findings=[{'text':'用户正在询问：'+case['question'],'evidenceRefs':['question']}]
    if case['wrongDiagnosis']:
        text='seen[x]=j保存第一次出现的位置。' if case['id'].startswith('development') else '空网格会执行grid[0]并报错。'
        findings=[{'text':text,'evidenceRefs':['code']}]
    candidate={'questions':[{'questionKey':'q1','question':case['question'],'evidenceRefs':['question'],
        'subgoals':[{'subgoalId':'correctness','reason':'涉及当前代码的含义与行为。','evidenceRefs':['goal','question']}],
        'findings':findings,'unknowns':[],
        'history':{'relation':'unknown','previousQuestionRefs':[],'reason':'没有先前实际提示。','evidenceRefs':['question']},'dependsOn':[]}],
        'contractConcerns':[]}
    return bundle,validate_diagnosis(candidate,bundle)
