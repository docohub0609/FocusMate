import { stopTestProcess } from '../stop-test-process.mjs';
import { chromium, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, delimiter } from 'node:path';
import { createWebServer } from '../../apps/web/server.mjs';

const temp = await mkdtemp(join(tmpdir(), 'focusmate-coach-ui-'));
const child = spawn(process.env.FOCUSMATE_PYTHON || 'python', ['tools/experiments/t082_browser_fixture.py'], {
  env: { ...process.env, PYTHONPATH: [process.cwd(), join(process.cwd(), 'src')].join(delimiter), FOCUSMATE_DB_PATH: join(temp, 'test.db'), PYTHONUTF8: '1' }, stdio: ['ignore', 'pipe', 'pipe'],
});
let server, browser;
const errors = [];
try {
  const port = await new Promise((resolve, reject) => {
    let output = ''; const timer = setTimeout(() => reject(Error('fixture timeout')), 25000);
    child.stdout.on('data', c => { output += c; try { const data = JSON.parse(output.trim()); clearTimeout(timer); resolve(data.port); } catch {} });
    child.stderr.on('data', c => process.stderr.write(c)); child.on('exit', code => reject(Error('fixture exit ' + code)));
  });
  process.env.FOCUSMATE_API_URL = 'http://127.0.0.1:' + port;
  server = await createWebServer({ test: true }); await new Promise(r => server.listen(0, '127.0.0.1', r));
  const base = 'http://127.0.0.1:' + server.address().port;
  browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  const context = await browser.newContext({ viewport: { width: 1560, height: 1000 } });
  const page = await context.newPage(); page.on('pageerror', error => errors.push(String(error)));
  page.on('dialog', dialog => dialog.accept());
  await page.goto(base + '/problems/problem-submit/versions/1/code');
  const code = page.getByRole('textbox', { name: 'Python代码', exact: true });
  await expect(code).toBeVisible();
  await expect(page.getByRole('region', { name: '整题核验' })).toContainText('尚未通过');
  await page.getByRole('button', { name: '向教练求助' }).click();
  const panel = page.getByRole('complementary', { name: '刷题教练' });
  const question = panel.getByRole('textbox', { name: '想请教的问题' });
  async function ask(text) {
    await question.fill(text); await panel.getByRole('button', { name: '发送给教练' }).click();
    await expect(panel.locator('.message').filter({ hasText: text })).toBeVisible({ timeout: 15000 });
  }
  async function settled() { await expect(panel.getByRole('status')).toHaveText('等待你继续', { timeout: 30000 }); }
  await ask('普通问答：输入是什么？'); await settled();
  await expect(panel.getByLabel('工作目标进展')).toHaveCount(0);
  await expect(code).toHaveText('');
  await code.fill('print(1)');
  await expect(page.getByTestId('save-status')).toHaveText('已保存', { timeout: 8000 });
  const editorElement = await code.elementHandle();
  await ask('建立目标，看看我的输出'); await settled();
  await expect(panel.getByRole('button', { name: '当前处理：检查输出语句' })).toBeVisible();
  await panel.getByRole('button', { name: '查看关联代码' }).last().click();
  await expect(page.locator('.code-pane .cm-coach-line')).toHaveCount(1);
  await code.press('End'); await code.pressSequentially(' # changed');
  await expect(page.locator('.code-pane .cm-coach-line')).toHaveCount(0);
  await code.press('Control+z'); await expect(code).toHaveText('print(1)');
  expect(await editorElement.evaluate(el => el.isConnected)).toBe(true);
  await panel.getByRole('button', { name: '当前处理：检查输出语句' }).click();
  await panel.getByRole('button', { name: '检查这个问题' }).click();
  await expect(panel.locator('.message').filter({ hasText: '请检查所选问题' })).toBeVisible({ timeout: 15000 }); await settled();
  await expect(panel.getByRole('button', { name: '已核验：检查输出语句' })).toBeVisible();
  await expect(panel.getByRole('region', { name: '工作目标详情' })).toContainText('本次检查通过');
  await code.fill('print(2)'); await expect(page.getByTestId('save-status')).toHaveText('已保存', { timeout: 8000 });
  await panel.getByRole('button', { name: '定位检查的代码' }).last().click();
  await expect(panel.getByRole('textbox', { name: '教练引用的历史代码' })).toHaveText('print(1)');
  await expect(code).toHaveText('print(2)'); await expect(page.locator('.code-pane .cm-coach-line')).toHaveCount(0);
  await panel.locator('.coach-previous > summary').click();
  await panel.getByRole('button', { name: '回复这条建议' }).last().click();
  await expect(panel).toContainText('正在回复选中的建议');
  await ask('谢谢，请解释这条建议'); await settled();

  const requests = []; let dropped = false;
  await page.route('**/commands', async route => {
    requests.push(route.request().postDataJSON());
    if (!dropped) { dropped = true; await route.fetch(); await route.abort(); } else await route.continue();
  });
  await ask('断网测试，保留原来的问题');
  await expect(panel.getByRole('button', { name: '找回发送结果' })).toBeVisible();
  await context.setOffline(true); await expect(question).toHaveValue('断网测试，保留原来的问题');
  await context.setOffline(false); await page.reload();
  await expect(panel.getByRole('button', { name: '找回发送结果' })).toBeVisible();
  await panel.getByRole('button', { name: '找回发送结果' }).click(); await settled();
  expect(requests[0]).toEqual(requests[1]);
  await expect(panel.locator('.message').filter({ hasText: '断网测试，保留原来的问题' })).toHaveCount(1);
  await page.unroute('**/commands');

  await ask('等待我修改'); await expect(panel.getByRole('status')).toHaveText('已登记，等待教练');
  await panel.getByRole('button', { name: '暂停辅导' }).click(); await expect(panel.getByRole('status')).toHaveText('辅导已暂停');
  await panel.getByRole('button', { name: '恢复辅导' }).click();
  await panel.getByRole('button', { name: '取消本轮' }).click(); await expect(panel.getByRole('status')).toHaveText('本轮已取消');
  await ask('申请执行许可'); await expect(panel.getByRole('button', { name: '仅允许这一步' })).toBeVisible({ timeout: 12000 });
  await code.press('End'); await code.pressSequentially(' # edit');
  await expect(panel.getByRole('button', { name: '仅允许这一步' })).toBeDisabled();
  await expect(page.getByTestId('save-status')).toHaveText('已保存', { timeout: 8000 });
  await ask('再申请执行许可'); await expect(panel.getByRole('button', { name: '仅允许这一步' })).toBeEnabled({ timeout: 12000 });
  await panel.getByRole('button', { name: '仅允许这一步' }).click();
  await expect(panel).toContainText('本轮未能完成', { timeout: 15000 });
  // Unknown external execution is never rerun by a recovery click.
  await expect(panel.getByRole('button', { name: '继续未完成的步骤' })).toHaveCount(0);
  await ask('再申请执行许可');
  await expect(panel.getByRole('button', { name: '不允许执行' })).toBeVisible({ timeout: 15000 });
  await panel.getByRole('button', { name: '不允许执行' }).click();
  await expect(panel.getByRole('status')).toHaveText('本轮已取消');

  await ask('模拟建议失败');
  await expect(panel.getByRole('button', { name: '继续未完成的步骤' })).toBeVisible({ timeout: 15000 });
  await panel.getByRole('button', { name: '继续未完成的步骤' }).click(); await settled();

  await ask('建立目标，运行后跟进'); await settled();
  const reportCount = await panel.locator('.coach-message.report').count();
  await page.getByRole('button', { name: '运行', exact: true }).click();
  await expect(panel).toContainText('等待运行结果及后台跟进', { timeout: 10000 });
  await expect(panel.locator('.coach-message.report')).toHaveCount(reportCount + 1, { timeout: 35000 });
  await settled();

  // The report is useful immediately, even if producing its following hint fails.
  await code.fill('print(1) # check-fail');
  await expect(page.getByTestId('save-status')).toHaveText('已保存', { timeout: 8000 });
  await panel.getByRole('region', { name: '当前问题', exact: true }).getByRole('button', { name: '检查这一步' }).click();
  await expect(panel.locator('.coach-message.report').last()).toContainText('本次检查未通过', { timeout: 15000 });
  await expect(panel.getByRole('button', { name: '继续未完成的步骤' })).toBeVisible({ timeout: 15000 });
  await expect(panel).not.toContainText('UNREVIEWED');
  const savedReport = await panel.locator('.coach-message.report').last().textContent();
  await panel.getByRole('button', { name: '继续未完成的步骤' }).click(); await settled();
  await expect(panel.locator('.coach-message.report').last()).toHaveText(savedReport);
  await ask('普通问答：先解释输入含义'); await settled();
  await expect(panel.getByRole('region', { name: '当前问题', exact: true })).toHaveCount(0);
  await panel.locator('.coach-history > summary').click();
  await panel.locator('.coach-history .history-item').last().click();
  await expect(panel.getByRole('region', { name: '工作目标详情' })).toContainText('当前关注的问题没有改变');
  await expect(panel.getByRole('region', { name: '当前问题', exact: true })).toHaveCount(0);

  // Presentation-only completion fixture; authoritative final verification is tested in Python.
  await page.route('**/completion', route => route.fulfill({ json: { status: 'completed', codeVersion: 1, resultId: 'display-only', report: null } }));
  await panel.getByRole('button', { name: '暂停辅导' }).click();
  await panel.getByRole('button', { name: '收起教练' }).click();
  await page.reload();
  await expect(page.getByRole('region', { name: '整题核验' })).toContainText('代码版本 1 已通过整题核验');
  await expect(page.getByRole('region', { name: '整题核验' })).toContainText('当前草稿有变化，尚未验证');
  await page.getByRole('button', { name: '向教练求助' }).click();
  await expect(panel.getByRole('status')).toHaveText('辅导已暂停');
  await page.unroute('**/completion');
  // Reload may happen after the server paused but before the browser received its reply.
  const recover = panel.getByRole('button', { name: '找回发送结果' });
  if (await recover.isVisible()) await recover.click();
  await panel.getByRole('button', { name: '恢复辅导' }).click();

  const other = await context.newPage();
  await other.goto(base + '/problems/problem-submit/versions/1/code');
  await other.getByRole('textbox', { name: 'Python代码', exact: true }).fill('print(99)');
  await expect(other.getByTestId('save-status')).toHaveText('已保存', { timeout: 8000 });
  await code.fill('print(88)');
  await expect(page.getByTestId('save-status')).toHaveText('保存冲突', { timeout: 8000 });
  await question.fill('冲突时保留的问题');
  await expect(panel.getByRole('button', { name: '发送给教练' })).toBeDisabled();
  await page.getByRole('button', { name: '采用后台内容' }).click();
  await expect(code).toHaveText('print(99)'); await expect(question).toHaveValue('冲突时保留的问题');
  await expect(panel.getByRole('button', { name: '发送给教练' })).toBeEnabled();
  await other.close();

  await question.fill('中文问题暂存');
  const currentEditor = await code.elementHandle();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(code).not.toBeVisible();
  await panel.getByRole('button', { name: '收起教练' }).click(); await expect(code).toBeVisible();
  expect(await currentEditor.evaluate(el => el.isConnected)).toBe(true);
  await page.getByRole('button', { name: '向教练求助' }).click(); await expect(question).toHaveValue('中文问题暂存');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: join(temp, 'coach-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1560, height: 1000 });
  await page.screenshot({ path: join(temp, 'coach-desktop.png'), fullPage: true });
  expect(errors).toEqual([]);
  const result = { status: 'passed', model: 'scripted responses', runner: 'FakeRunner', completionDisplay: 'mocked projection; actual final verifier separately tested', cases: ['empty-question', 'goals', 'reports', 'highlight-undo', 'old-code', 'reply', 'lost-response-reload', 'pause-cancel', 'permission-version', 'unknown-execution-no-replay', 'resume-failed-step', 'report-before-help-and-resume', 'safe-report-projection', 'history-view-keeps-focus', 'delayed-follow-up', 'paused-collapsed-completion', 'draft-conflict-keeps-question', 'mobile-preserves-editor'], artifacts: temp };
  await writeFile(join(temp, 'result.json'), JSON.stringify(result, null, 2)); console.log(JSON.stringify(result));
} catch (error) {
  const page = browser?.contexts()[0]?.pages()[0];
  if (page) await page.screenshot({ path: join(temp, 'coach-failure.png'), fullPage: true }).catch(() => {});
  await writeFile(join(temp, 'failure.json'), JSON.stringify({ status: 'failed', error: String(error), artifacts: temp }, null, 2));
  throw error;
} finally {
  if (browser) await browser.close(); if (server) await new Promise(r => server.close(r)); await stopTestProcess(child);
}
