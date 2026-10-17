"""Fixed facts for isolated review; labels never enter a model request."""
from copy import deepcopy
from action_cases import inputs
from focusmate.core.action_quality import review_schema


def reviewer_cases():
    rows=[
      ('x是3时打印什么？','解释if/else行为。','if x>0:\n    print("A")\nelse:\n    print("B")\n',
       '只打印A。','先打印A，再执行else打印B。','条件成立只走if，else不执行。'),
      ('best从0开始，这个程序什么情况下算不对？','非空整数数组求最大值。','best=0\nfor x in a:best=max(best,x)\n',
       '数组全为负数时会错误地留下0；只要有非负元素，就能得到真实最大值。',
       '只有数组所有元素非负才能算对，有负数就会算错。','[-5,2]得到2，全负才被零压住。'),
      ('空网格时grid[0]会被访问吗？','逐个访问元素。','for i in range(len(grid)):\n    for j in range(len(grid[0])):\n        print(grid[i][j])\n',
       '不会。外层循环没有执行，内层的grid[0]也不会求值。',
       '会。内层循环会先计算len(grid[0])，所以空网格必然报错。','外层零次时不执行内层语句。'),
      ('报错时一定是我输入了不能转整数的文字吗？','读取一个整数。','n=int(input())\n',
       '不一定。文本转换失败可能是ValueError；输入流结束时，input本身可能先抛EOFError。',
       '一定。这一行只可能在int解析文字失败时出错。','输入流结束的EOFError是反例。'),
      ('只把参数n改成count，字符串不变。','重复字母n。','def repeat(n):\n    return "n"*n\n',
       '修改参数名和函数体中的变量引用，字符串保持不变：\n```python\ndef repeat(count):\n    return "n"*count\n```',
       '只修改了函数签名一行：\n```python\ndef repeat(count):\n    return "n"*count\n```','正文说一行但代码改两行。'),
      ('a[mid]<t时lo能改成mid+1吗？','有序数组查找第一个不小于t的位置；区间[lo,hi)。','while lo<hi:\n    mid=(lo+hi)//2\n    if a[mid]<t:lo=mid\n    else:hi=mid\n',
       '可以。这个分支中mid位置小于t，不可能是答案，应排除它并推进lo；else分支才保留mid。',
       '不可以。mid在这个分支仍可能是答案，lo=mid+1会跳过答案。','明确区分if和else，当前分支已经排除mid。')]
    result=[]
    for i,(q,p,code,good,bad,basis) in enumerate(rows,1):
        bundle,diagnosis=inputs({'id':f'check-{i}','question':q,'problem':p,'code':code,'oldRun':False,'wrongDiagnosis':False})
        for j,(text,expected) in enumerate([(good,'passed'),(bad,'failed')],1):
            candidate={'questionKeys':['q1'],'subgoalIds':['correctness'],'actionType':'answer','hintLevel':4 if i==5 else 2,
                'answerScope':'localCode' if i==5 else 'guided','reason':'回答本次问题。',
                'evidenceRefs':['question','code','problem'],'requestQuotes':[],'content':text,'diagnosisCorrections':[],
                'followUp':{'kind':'none','reason':'本次解释结束。','criterionIds':[],'checks':[],'expectedUserAction':''}}
            result.append({'id':f'fixed-{i:02}-{j}','bundle':bundle,'diagnosis':diagnosis,'candidate':candidate,'expected':expected,'basis':basis})
    return result


def review_payload(case):
    return {'materials':deepcopy(case['bundle']['records']),'candidate':deepcopy(case['candidate']),
            'evidenceCatalog':[r['ref'] for r in case['bundle']['records']],
            'reviewSchema':review_schema([])}


def repair_cases():
    from focusmate.core.action_planning import REVIEW_KEYS,review_issues
    cases=reviewer_cases()
    rows=[(0,'if条件成立之后else仍会执行，因此漏掉了B。','首次审核错了，保留只打印A。'),
          (4,'内层range必须求值，即使外层没有迭代也会求grid[0]。','首次审核错了，保留空网格不访问grid[0]。'),
          (3,'[-5,2]是反例，存在负数不代表结果错；原文扩大了失败条件。','纠正全部非负的必要条件，保留全负会错。'),
          (9,'实际代码同时改了定义和函数体变量，不是只改签名一行。','修正文字，保留正确代码与字符串。')]
    result=[]
    for i,(index,reason,basis) in enumerate(rows,1):
        case=deepcopy(cases[index]);candidate=case['candidate']
        review={'checks':{k:{'verdict':'pass','reason':'其他项无异议。','candidateQuote':'','materialRefs':['code']} for k in REVIEW_KEYS}}
        review['checks']['fact']={'verdict':'fail','reason':reason,'candidateQuote':candidate['content'],'materialRefs':['code']}
        case.update(id=f'repair-{i:02}',firstReview=review,previousIssues=review_issues(review,candidate),basis=basis)
        result.append(case)
    return result
