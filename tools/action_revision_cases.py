"""Fresh source-checked fixtures; expected judgments are evaluation-only."""

def cases():
    rows=[
        ('邻居已经看过就跳过，这样可以吗？只解释这份写法。','无向图有环，每点访问一次；起点已加入seen。',
         'def walk(u):\n    for v in adj[u]:\n        if v not in seen:\n            seen.add(v)\n            walk(v)\n','邻居循环中先标记再递归有效，不强制移到函数入口。'),
        ('我已经写了seen，为什么还可能反复调用？先给方向。','无向图可能含环，seen初始为空。',
         'def visit(u):\n    if u in seen:return\n    for v in adj[u]:visit(v)\n    seen.add(u)\n','访问标记在递归后，环上可能来不及标记；不要求换BFS。'),
        ('[-4,7,-2]也有负数，这段是不是就一定错？','非空整数列表求最大值。',
         'a=[-4,7,-2]\nbest=0\nfor x in a:best=max(best,x)\nprint(best)\n','该输入输出7正确；有负数不必然错，可简短限定全负数问题。'),
        ('[-9,-3]应该得到哪个值，为什么现在不对？不要完整程序。','非空整数列表返回最大元素。',
         'a=[-9,-3]\nbest=0\nfor x in a:\n    if x>best:best=x\nprint(best)\n','应-3实际0，初值阻止负数更新；无完整程序。'),
        ('这一分支遇到相邻位置，还会原地不动吗？','非空有序列表找第一个不小于t的位置，初始lo=0,hi=len(a)。',
         'while lo<hi:\n    m=(lo+hi)//2\n    if a[m]<t:lo=m+1\n    else:hi=m\n','相邻时m=lo，两分支均严格缩小；不能看成lo=m。'),
        ('a=[2,6]，t=5时为什么返回0？只看边界。','返回第一个不小于t的位置，不存在时返回len(a)。',
         'a=[2,6];t=5\nlo=0;hi=len(a)\nwhile lo<hi:\n    m=(lo+hi)//2\n    if a[m]<t:lo=m+1\n    else:hi=m-1\nprint(lo)\n','m=1时hi变0漏过位置1；不误称死循环。'),
        ('只把循环变量item改成value，别改其他内容。','输出列表每个元素的平方。',
         'for item in nums:\n    print(item*item)\n','三处item对应改名，局部两行，不添加算法要求。'),
        ('只把函数里的p改成position，外面的p不要动。','返回传入位置加一，外部变量独立保留。',
         'p=99\ndef advance(p):\n    return p+1\nprint(p)\n','只改函数参数和函数体引用；外部p保持99，不断言必然NameError。'),
        ('空列表调用这个函数，会在a[0]报错吗？','空列表返回None，其他返回第一个元素。',
         'def first(a):\n    if not a:return None\n    return a[0]\n','先return，空时不访问a[0]，不扩展无关输入。'),
        ('a为空时，这个检查来得及吗？','空列表应返回None。',
         'def first(a):\n    x=a[0]\n    if not a:return None\n    return x\n','取a[0]在检查之前，空时IndexError，指出先后顺序。'),
        ('昨天究竟输入了哪个单词，这个函数能告诉我吗？','把读到的文本转换为大写，没有保存历史输入。',
         'def convert():\n    text=input()\n    return text.upper()\n','源码不能恢复过去输入，需日志或用户补充，不猜输入。'),
        ('我按了一个空行，这里一定抛EOFError吗？','input读取一行，再把文本转换成整数。',
         'n=int(input())\nprint(n)\n','空行input返回空串，int引发ValueError；输入流结束才是input的EOFError。'),
    ]
    return [{'id':f'new-{i:02}','question':q,'problem':p,'code':c,'expected':e,
             'oldRun':False,'wrongDiagnosis':False} for i,(q,p,c,e) in enumerate(rows,1)]


def final_cases():
    rows=[
      ('标记放在入队前可以吗，不要帮我换写法。','有环有向图，只处理从起点可达的点；seen初始有起点，q初始包含起点。',
       'while q:\n    u=q.popleft()\n    for v in adj[u]:\n        if v not in seen:\n            seen.add(v)\n            q.append(v)\n','先标记再入队防重复，支持有向图，不要求DFS入口标记。'),
      ('[4,9]的最小值为什么算出0？请简短解释。','非空整数列表求最小值。',
       'a=[4,9]\nbest=0\nfor x in a:best=min(best,x)\nprint(best)\n','0比真实最小值4小，无法更新；不扩大为有正数都错误。'),
      ('这里t比所有元素都大，返回len(a)算不算越界错误？','下界查找，不存在不小于t的元素时返回len(a)，不读取该下标。',
       'a=[3,8];t=10\nlo=0;hi=len(a)\nwhile lo<hi:\n    m=(lo+hi)//2\n    if a[m]<t:lo=m+1\n    else:hi=m\nprint(lo)\n','返回2是允许的不存在标记，没有a[2]读取，不误报越界。'),
      ('只把函数参数n改成count，保留字符串里的n。','返回重复指定次数的字母n。',
       'def repeat(n):\n    return "n"*n\n','只改参数与乘法变量，字符串n保持，不加无关检查。'),
      ('没有任何订单时，这里会调用charge吗？','orders是列表，charge负责收款；只解释是否调用。',
       'for order in orders:\n    charge(order)\nprint("done")\n','空列表不进入循环，charge不调用，仍执行打印。'),
      ('输入三个空格再回车，为什么不是EOFError？','程序读取一行并转成整数。',
       'text=input()\nnumber=int(text)\n','input返回空格文本；int不能解析成整数，ValueError而非EOFError。'),
    ]
    return [{'id':f'final-{i:02}','question':q,'problem':p,'code':c,'expected':e,'oldRun':False,'wrongDiagnosis':False}
            for i,(q,p,c,e) in enumerate(rows,1)]
