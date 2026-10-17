"""Developer-authored facts and expectations; expectations never enter model input."""
from copy import deepcopy
from focusmate.core.context_builder import hash_content


def contract(goal='g',problem='p'):
    return {'goalId':goal,'schemaVersion':'1.0.0','version':1,'createdAt':'2026-09-20T00:00:00+00:00',
        'problemRef':{'problemId':problem,'problemVersion':1},'objective':'按题目要求完成实现并取得可信AC',
        'subgoals':[{'subgoalId':sid,'stage':stage,'objective':text,'dependsOn':[],
                    'acceptableEvidence':['codeStructureCheck','evaluatedUserExplanation','targetedRunResult']}
          for sid,stage,text in [('understand','problemUnderstanding','说明输入输出与约束'),
            ('strategy','feasibleStrategy','说明方法适用于题目条件'),('correctness','coreStateAndCorrectness','说明状态与更新依据'),
            ('implementation','coreImplementation','实现当前方法并保持所需行为'),('tests','boundaryAndTesting','检查边界与实际运行结果')]],
        'navigationPolicy':{'mode':'adaptive','stageOrder':'defaultOnly','allowSkip':True,'allowRevisit':True,
            'offRouteHandling':'temporaryBranch','stalledRouteFallback':'latestVerifiedSubgoal','processStagesRequiredForCompletion':False},
        'finalCriterion':{'verifierType':'finalOj','requiredVerdict':'AC'},'coachPolicy':{'completeSolutionAllowed':False},'contractStatus':'frozen'}

DP='0/1背包：每件物品最多使用一次，容量不超过W，求最大价值；dp[j]表示容量上限j时最大价值，重量为正整数。'
DP_CODE='W=6\nitems=[(2,3),(3,4)]\ndp=[0]*(W+1)\nfor w,v in items:\n    for j in range(W,w-1,-1):\n        dp[j]=max(dp[j],dp[j-w]+v)\nprint(dp[W])\n'
SUM='给定整数数组和target，返回两个不同位置（0开始）使其数值之和等于target，保证唯一解；n不超过100。'
SUM_CODE='n,target=map(int,input().split())\na=list(map(int,input().split()))\nfor i in range(n):\n    for j in range(i+1,n):\n        if a[i]+a[j]==target:\n            print(i,j)\n'


def cases(group):
    # Different concrete contexts in the held-out partition, not variable renamings.
    if group=='development':
        rows=[
          ('我已改成倒序，为什么还是不理解？',DP,DP_CODE,'说明代码已倒序，问题是对更新依据的理解；不能宣布正确或受阻仅因还是。',{}),
          ('dp[j]代表什么？range为什么包含w？这两个我都没懂。',DP,DP_CODE,'保留两个问题，可分别关联理解/正确性/实现；不要求一对一。',{}),
          ('我不用哈希，用双循环，这条路线不行吗？',SUM,SUM_CODE,'n<=100，双循环是合理替代，不强迫哈希。',{}),
          ('只把变量j改成pos，这个请求对应什么目标？',SUM,SUM_CODE,'局部改名，无需强行关联算法错误或运行。',{}),
          ('还是容量更新顺序的问题，我只是换个说法问。',DP,DP_CODE,'有明确旧问题可判同一，不能只凭相同Subgoal。',{'oldQuestion':'为什么0/1背包容量要倒序？'}),
          ('方向已经懂了，现在问为什么最后取dp[W]。',DP,DP_CODE,'同一正确性目标下的新问题，与倒序问题区分。',{'oldQuestion':'为什么0/1背包容量要倒序？'}),
          ('倒序还是不理解，另外初始全是0是什么意思？',DP,DP_CODE,'新旧两个问题并存，不能统一判新或同一。',{'oldQuestion':'为什么0/1背包容量要倒序？'}),
          ('我改过了，帮我看看哪里不对。',DP,None,'未提供代码，说明缺代码和实际现象，不编造修改。',{}),
          ('请找容量更新的那段看看实际方向，我只知道在长文件末尾。',DP,'# 已保存注释\n'*1400+DP_CODE,'需要真实补读，未见原文前不说已看过方向。',{'tools':['search_code','read_code_excerpt']}),
          ('旧版跑过了，这一版也算验证了吗？',DP,DP_CODE,'旧运行只归旧版，不给当前代码验证通过。',{'oldRun':True}),
          ('请查询刚才那份提交sub-1的结果，先不要猜代码哪里错。',SUM,SUM_CODE,'查询故障时保留未知，不伪造提交状态或重跑。',{'tools':['read_submission_result'],'fault':True}),
          ('我怀疑两个位置会相同，请运行已有公开样例核对实际输出。',SUM,SUM_CODE,'实际运行结果与代码i+1排除同位置；公开通过不是整体完成。',{'tools':['run_public_samples']})]
    else:
        rows=[
          ('我用二维表改了实现，现在dp[i][c]里i是物品还是容量？',DP,'dp=[[0]*7 for _ in range(3)]\nfor i,(w,v) in enumerate([(2,3),(3,4)],1):\n    for c in range(7):\n        dp[i][c]=dp[i-1][c]\n        if c>=w: dp[i][c]=max(dp[i][c],dp[i-1][c-w]+v)\n','明确询问状态含义，不因二维表就断定没策略。',{}),
          ('为什么seen里存位置？还有查找为什么写在赋值前？',SUM,'seen={}\nfor j,x in enumerate([3,3]):\n    if 6-x in seen: print(seen[6-x],j)\n    seen[x]=j\n','两个问题都保留，可共用正确性目标，不编造运行。',{}),
          ('图是一棵树，我只跳过parent，不用visited行不行？','给定无向树，无自环，从0遍历每个可达点。','def dfs(u,parent):\n    for v in adj[u]:\n        if v!=parent: dfs(v,u)\n','树可以跳过父节点，不强迫visited或图外parent。',{}),
          ('我只想把编辑器背景改成深色，先别讨论算法。','广度遍历无向图。','q=[0]\nwhile q:\n    u=q.pop(0)\n','界面设置没有对应的解题Subgoal，不强行关联代码错误，不擅自新增目标。',{}),
          ('仍然是空数组时取第一个元素这个问题，不是新的问题。','数组可能为空，非空时返回首元素。','def first(a):\n    return a[0]\n','结合旧问题确定相同问题。',{'oldQuestion':'空数组时为什么a[0]会报错？'}),
          ('空数组已经处理了，现在负数结果怎么显示？','数组可能为空，非空时返回首元素。','def first(a):\n    return a[0] if a else None\n','当前问题已换，不能因仍是同一函数归为旧空数组问题。',{'oldQuestion':'空数组时为什么a[0]会报错？'}),
          ('二分退出条件还是不懂，另外返回lo是什么含义？','有序数组找第一个不小于target的位置。','lo,hi=0,len(a)\nwhile lo<hi:\n    mid=(lo+hi)//2\n    if a[mid]<target:lo=mid+1\n    else:hi=mid\n','退出条件旧问题和返回值新问题分开。',{'oldQuestion':'二分为什么用lo<hi作为循环条件？'}),
          ('刚才那份图代码又错了，能看一下吗？','判断无向图两点是否连通。',None,'没有图代码或结果，保留缺项，不凭题类推错因。',{}),
          ('长文件里的walk是不是已经有防重复访问了？请读后再判断。','无向图可能有环，遍历时避免重复访问。','# 原始说明\n'*1600+'seen=set()\ndef walk(u):\n    if u in seen:return\n    seen.add(u)\n    for v in adj[u]:walk(v)\n','真实补读后承认存在guard，不能维持用户暗示的缺visited怀疑。',{'tools':['search_code','read_code_excerpt']}),
          ('昨天的运行是通过的，今天修改过的函数能直接算通过吗？','返回输入列表中的最大值，列表非空。','def maximum(a):\n    return min(a)\n','旧结果不能证明新函数；当前min与max要求不同，但不冒充新运行。',{'oldRun':True}),
          ('查sub-1的状态，没查到就说不知道。','输出输入整数的两倍。','x=int(input())\nprint(2*x)\n','模拟查询故障不编状态，允许有效未知。',{'tools':['read_submission_result'],'fault':True}),
          ('这个程序输出的是位置还是数值？请跑公开样例再对照题面。',SUM,SUM_CODE.replace('print(i,j)','print(a[i],a[j])'),'真实输出不符，指出数值/位置问题，不宣布学习目标完成。',{'tools':['run_public_samples']})]
    return [{'id':f'{group}-{i+1:02}','question':q,'problem':problem,'code':code,'expected':expected,**options}
            for i,(q,problem,code,expected,options) in enumerate(rows)]


def material_bundle(case,goal='g',problem='p'):
    context={'goalId':goal,'scope':'goal'};c=contract(goal,problem)
    def record(ref,kind,content,version=1):
        return {'ref':ref,'kind':kind,'version':version,'goalContext':context,'content':content,
                'contentHash':hash_content(content),'origin':'developer-authored'}
    records=[record('goal','goal',c),record('problem','problemFact',{'text':case['problem'],'confirmed':True}),
             record('question','dialogue',{'ref':'question','role':'user','text':case['question']})]
    records[-1].update(sequence=1,turnId='current')
    req={'schemaVersion':'1.0.0','goalContext':context,'problemRef':c['problemRef'],'contractVersion':1,
         'contractRef':'goal','userRequestRef':'question','baselineRefs':[],'dialogueRefs':[],'historyDiagnosisRefs':[],
         'budget':{'contextWindowTokens':64000,'reservedOutputTokens':8192,'safetyMarginTokens':2048}}
    if case['code'] is not None:
        records.append(record('code','code',{'snapshotRef':'code','language':'python3','text':case['code']},2 if case.get('oldRun') else 1));req['currentCodeRef']='code'
    if case.get('oldRun'):
        records.append(record('old-code','code',{'snapshotRef':'old-code','language':'python3','text':'print(0)\n'}))
        records.append(record('old-result','runEvidence',{'codeSnapshotRef':'old-code','evidenceType':'targetedRunResult','completed':True,'outcome':'passed','source':'explicitDevelopmentFixture'}))
    if case.get('oldQuestion'):
        from focusmate.core.diagnosis import validate_diagnosis
        oldreq=deepcopy(req);oldreq['sources']=[{k:r[k] for k in ('ref','kind','version','contentHash')} for r in records]
        old={'questions':[{'questionKey':'prior','question':case['oldQuestion'],'evidenceRefs':['question'],
             'subgoals':[],'findings':[],'unknowns':[{'text':case['oldQuestion'],'evidenceRefs':['question']}],
             'history':{'relation':'unknown','previousQuestionRefs':[],'reason':'此前未提供诊断','evidenceRefs':['question']},'dependsOn':[]}],'contractConcerns':[]}
        oldrecords=deepcopy(records);oldrecords[2]['content']['text']=case['oldQuestion'];oldrecords[2]['contentHash']=hash_content(oldrecords[2]['content'])
        oldreq['sources']=[{k:r[k] for k in ('ref','kind','version','contentHash')} for r in oldrecords]
        report=validate_diagnosis(old,{'request':oldreq,'records':oldrecords,'missing':[]})
        records.append(record('previous','hypothesis',{'diagnosisReport':report}));req['historyDiagnosisRefs']=['previous']
    req['sources']=[{k:r[k] for k in ('ref','kind','version','contentHash')} for r in records]
    return req,records
