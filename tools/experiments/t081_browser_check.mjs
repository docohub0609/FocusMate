import { stopTestProcess } from '../stop-test-process.mjs';
import { chromium, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createWebServer } from '../../apps/web/server.mjs';

const temp=await mkdtemp(join(tmpdir(),'focusmate-editor-'));
const child=spawn(process.env.FOCUSMATE_PYTHON || 'python',['tools/experiments/t081_browser_fixture.py'],{
  env:{...process.env,PYTHONPATH:process.cwd(),FOCUSMATE_DB_PATH:join(temp,'test.db')},stdio:['ignore','pipe','pipe']});
let server,browser;
try {
  const port=await new Promise((resolve,reject)=>{
    let output='';const timer=setTimeout(()=>reject(Error('fixture timeout')),20000);
    child.stdout.on('data',chunk=>{output+=chunk;try {const data=JSON.parse(output.trim());clearTimeout(timer);resolve(data.port);}catch{}});
    child.stderr.on('data',chunk=>process.stderr.write(chunk));child.on('exit',code=>reject(Error('fixture exit '+code)));
  });
  process.env.FOCUSMATE_API_URL='http://127.0.0.1:'+port;
  server=await createWebServer({test:true});await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base='http://127.0.0.1:'+server.address().port;
  browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
  const context=await browser.newContext();const page=await context.newPage();
  await page.goto(base+'/problems/problem-submit/versions/1/code');
  await expect(page.getByRole('textbox',{name:'Python代码'})).toBeVisible({timeout:8000});
  const code=page.getByRole('textbox',{name:'Python代码'});
  const real=process.env.T081_REAL_DOCKER==='1';
  const verdictSource={AC:'print("1 2")',WA:'print("wrong")',CE:'for x in',RE:'raise ValueError("demo")',TLE:'while True: pass'};
  async function write(text) { await code.fill(text); await expect(page.getByTestId('save-status')).toHaveText('已保存',{timeout:8000}); }
  await code.pressSequentially('pr');
  await expect(page.getByRole('option').filter({hasText:/^print$/})).toBeVisible();
  // CodeMirror intentionally ignores completion keys for 75 ms after opening.
  await page.waitForTimeout(100);
  await code.press('ArrowDown'); await code.press('ArrowUp'); await code.press('Enter');
  await expect(code).toHaveText('print');
  await code.fill('pr');await code.press('Control+Space');
  await code.press('Escape');
  await expect(page.getByRole('listbox')).not.toBeVisible();
  await code.fill('count = 1\nco');await code.press('Control+Space');
  await expect(page.getByRole('option').filter({hasText:/^count$/})).toBeVisible();await code.press('Escape');
  await code.fill('if True:');await code.press('End');await code.press('Enter');await code.pressSequentially('pass');
  await expect(code).toContainText('    pass');
  await code.press('Tab');await expect(code).toContainText('pass    ');
  await code.fill('');await code.pressSequentially('(');await expect(code).toContainText('()');
  await code.press('Control+z');await expect(code).not.toContainText('()');
  await code.press('Control+y');await expect(code).toContainText('()');
  await write('');await expect(page.getByRole('button',{name:'运行',exact:true})).toBeDisabled();
  await page.reload();await expect(code).toHaveText('');
  await write('# 中文注释\nprint("1 2")');
  await page.reload();await expect(code).toContainText('中文注释');
  await page.getByRole('button',{name:'运行',exact:true}).click();
  await expect(page.getByRole('region',{name:'执行结果'})).toContainText('样例通过',{timeout:45000});
  const metrics=page.getByRole('region',{name:'执行结果'});
  await expect(metrics).toContainText(real ? /执行用时 \d+ ms/ : '执行用时 未记录');
  await expect(metrics).toContainText('峰值内存');
  await expect(metrics).not.toContainText('内存上限');
  await page.getByRole('combobox').selectOption('customInput');
  if(real) await write('print(input())');
  await page.getByRole('textbox',{name:'标准输入'}).fill('你好\n');
  await page.getByRole('button',{name:'运行',exact:true}).click();
  await expect(page.getByRole('region',{name:'执行结果'})).toContainText('执行完成',{timeout:45000});
  await expect(page.getByRole('region',{name:'执行结果'})).toContainText('你好');
  if(real) await expect(page.getByRole('region',{name:'执行结果'}).locator('.case-columns > div').last()).toContainText('你好');
  for(const verdict of ['AC','WA','CE','RE','TLE']) {
    await write(real ? verdictSource[verdict] : 'print("1 2")\n# '+verdict);
    await page.getByRole('button',{name:'正式提交',exact:true}).click();
    await expect(page.getByRole('region',{name:'执行结果'}).getByRole('heading',{name:new RegExp('^'+verdict+' ·')})).toBeVisible({timeout:45000});
    if(verdict==='AC') {
      await expect(metrics).toContainText(real ? /执行用时（合计） \d+ ms/ : '执行用时（合计） 未记录');
      await expect(metrics).toContainText('峰值内存');
    }
  }
  if (!real) {
    await write('print("1 2")\n# SYSTEM');
    for (const action of ['正式提交', '运行']) {
      await page.getByRole('button',{name:action,exact:true}).click();
      await expect(metrics).toContainText('连接执行服务时发生系统故障',{timeout:15000});
      await metrics.getByText('查看故障记录',{exact:true}).click();
      await expect(metrics).toContainText('Timeout');
      await expect(metrics).toContainText('安全查询重试：1 次');
    }
  }
  await write('print("old")');
  await page.getByRole('button',{name:'运行',exact:true}).click();
  await code.fill('print("new")');
  await expect(page.getByRole('region',{name:'执行结果'})).toContainText('当前代码已有变化');
  await expect(page.getByTestId('save-status')).toHaveText('已保存',{timeout:8000});
  const other=await page.context().newPage();await other.goto(base+'/problems/problem-submit/versions/1/code');
  const otherCode=other.getByRole('textbox',{name:'Python代码'});await expect(otherCode).toBeVisible();
  await write('print("first page")');await otherCode.fill('print("second page")');
  await expect(other.getByTestId('save-status')).toHaveText('保存冲突',{timeout:8000});
  await expect(otherCode).toContainText('second page');
  await other.getByRole('button',{name:'采用后台内容'}).click();await expect(otherCode).toContainText('first page');await other.close();
  let dropped=false;
  await page.route('**/code-draft',async route=>{
    if(route.request().method()==='PUT' && !dropped){dropped=true;await route.fetch();await route.abort();}else await route.continue();
  });
  await code.fill('print("lost response")');await expect(page.getByTestId('save-status')).toHaveText('保存未确认',{timeout:8000});
  await page.getByRole('button',{name:'重试保存'}).click();await expect(page.getByTestId('save-status')).toHaveText('已保存');
  await page.unroute('**/code-draft');
  await expect(page.getByRole('region',{name:'执行结果'})).toContainText('执行结束',{timeout:45000});
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:join(temp,'editor-mobile.png'),fullPage:true});
  await page.setViewportSize({width:1400,height:1000});await page.screenshot({path:join(temp,'editor-desktop.png'),fullPage:true});
  console.log(JSON.stringify({status:'passed',runner:real?'Docker':'FakeRunner',materials:'explicit developer test cases; not model generated assets',cases:['editor-features','save-refresh','public-run','custom-run','five-verdicts','old-version','two-pages','lost-response','mobile'],artifacts:temp}));
} finally {
  if(browser)await browser.close();if(server)await new Promise(resolve=>server.close(resolve));await stopTestProcess(child);
}
