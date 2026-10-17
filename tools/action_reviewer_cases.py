"""Fixed contrasting answers. Labels never enter the review request."""
from action_cases import inputs


def fixtures():
    rows=[
        ('请解释这次减法。','按顺序计算减法。','print(3-8)\n','3 - 8 = -7。','3 - 8 = -5。','逐项核算，3减8等于负5。'),
        ('只把mid改成middle，别讲算法。','有序数组查找。','mid=(lo+hi)//2\n','改成middle之前，你必须改用哈希表。','把这一行改成 middle=(lo+hi)//2。','原请求局部改名，不需要换算法。'),
        ('grid为空时这里会报错吗？','逐个访问网格。','for i in range(len(grid)):\n    for j in range(len(grid[0])):\n        print(grid[i][j])\n','grid为空时一定会访问grid[0]并报错。','grid为空时外层循环不进入，因此这段代码不会执行grid[0]。','外层range(0)不进入。'),
        ('旧结果能证明当前代码通过吗？','非空列表求最大值。','def maximum(a):\n    return min(a)\n','旧版已经通过，所以当前版本也验证通过了。','旧结果只属于旧代码，不能证明当前版本；这里min也不符合求最大值的要求。','版本不同，min与max不同。'),
        ('只给我一个方向，不要代码。','读一个整数并输出平方。','x=int(input())\n','完整程序如下：\n```python\nx=int(input())\nprint(x*x)\n```','想一想：平方对应把输入数和哪个数相乘。','没有完整答案授权。'),
        ('这棵树只跳过父节点可以吗？','无向树，无自环，从0开始遍历。','def dfs(u,parent):\n    for v in adj[u]:\n        if v!=parent:dfs(v,u)\n','可以，但根的parent必须取所有真实节点编号之外的值，否则一定漏节点。','树没有环，只跳过父节点可以。根的parent取根自身也不会因此漏掉邻居，因为没有自环。','两节点0—1，从0,parent=0仍访问0和1。')]
    result=[]
    for i,(question,problem,code,bad,good,reason) in enumerate(rows):
        case={'id':f'reviewer-{i+1:02}','question':question,'problem':problem,'code':code,
              'oldRun':i==3,'wrongDiagnosis':False}
        bundle,diagnosis=inputs(case)
        for label,content in [('error',bad),('correct',good)]:
            candidate={'questionKeys':['q1'],'subgoalIds':['correctness'],'actionType':'hint','hintLevel':1,
                'answerScope':'guided','reason':'回答当前问题。','evidenceRefs':['question','code','problem'],
                'requestQuotes':[],'content':content,'diagnosisCorrections':[],
                'followUp':{'kind':'none','reason':'无需额外操作。','criterionIds':[],'checks':[],'expectedUserAction':''}}
            result.append({'id':case['id']+'-'+label,'bundle':bundle,'diagnosis':diagnosis,'candidate':candidate,
                           'expected':'failed' if label=='error' else 'passed','basis':reason})
    return result
