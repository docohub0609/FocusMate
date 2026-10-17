import { stopTestProcess } from '../stop-test-process.mjs';
import { chromium, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createWebServer } from '../../apps/web/server.mjs';

const temp = await mkdtemp(join(tmpdir(), 'focusmate-editor-recovery-'));
const child = spawn(process.env.FOCUSMATE_PYTHON || 'python', ['tools/experiments/t081_browser_fixture.py'], {
  env: { ...process.env, T081_REAL_DOCKER: '0', PYTHONPATH: process.cwd(), FOCUSMATE_DB_PATH: join(temp, 'test.db') }, stdio: ['ignore', 'pipe', 'pipe'],
});
let server, browser;
try {
  const port = await new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => reject(Error('fixture timeout')), 20000);
    child.stdout.on('data', chunk => { output += chunk; try { const data = JSON.parse(output.trim()); clearTimeout(timer); resolve(data.port); } catch {} });
    child.stderr.on('data', chunk => process.stderr.write(chunk));
    child.on('exit', code => reject(Error('fixture exit ' + code)));
  });
  process.env.FOCUSMATE_API_URL = 'http://127.0.0.1:' + port;
  server = await createWebServer({ test: true }); await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = 'http://127.0.0.1:' + server.address().port;
  const url = base + '/problems/problem-submit/versions/1/code';
  browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  const cases = {
    async longCodeScroll(page) {
      const code = page.getByRole('textbox', { name: 'Python代码' });
      await code.fill(Array.from({ length: 200 }, (_, i) => '# line ' + i).join('\n'));
      await expect(page.getByTestId('save-status')).toHaveText('已保存', { timeout: 8000 });
      const scroller = page.locator('.python-editor .cm-scroller');
      for (const width of [1280, 390]) {
        await page.setViewportSize({ width, height: 844 });
        expect(await scroller.evaluate(node => node.clientHeight)).toBeLessThan(700);
        expect(await scroller.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
        await scroller.evaluate(node => { node.scrollTop = 0; });
        await scroller.hover();
        const pageY = await page.evaluate(() => window.scrollY);
        await page.mouse.wheel(0, 400);
        await expect.poll(() => scroller.evaluate(node => node.scrollTop)).toBeGreaterThan(0);
        expect(await page.evaluate(() => window.scrollY)).toBe(pageY);
        await scroller.evaluate(node => { node.scrollTop = node.scrollHeight; });
        await expect(code).toContainText('# line 199');
        await page.mouse.wheel(0, 400);
        await page.waitForTimeout(200); // Allow native wheel scroll chaining to settle.
        expect(await page.evaluate(() => window.scrollY)).toBe(pageY);
      }
    },
    async returnToEditor(page) {
      await page.getByRole('textbox', { name: 'Python代码' }).fill('print("return intact")');
      await expect(page.getByTestId('save-status')).toHaveText('已保存', { timeout: 8000 });
      await page.getByRole('link', { name: '← 题目与测试准备' }).click();
      await page.getByRole('link', { name: '开始写代码 →' }).click();
      await expect(page.getByRole('textbox', { name: 'Python代码' })).toHaveText('print("return intact")');
      await expect(page.getByTestId('save-status')).toHaveText('已保存');
    },
    async copiedTabIsolation(page) {
      const popup = page.waitForEvent('popup');
      await page.evaluate(url => window.open(url, '_blank'), url);
      const other = await popup;
      await expect(other.getByRole('textbox', { name: 'Python代码' })).toBeVisible();
      for (const tab of [page, other]) await tab.route('**/code-draft', route => route.request().method() === 'PUT' ? route.abort() : route.continue());
      await page.getByRole('textbox', { name: 'Python代码' }).fill('print("offline first")');
      await other.getByRole('textbox', { name: 'Python代码' }).fill('print("offline second")');
      await page.reload();
      await expect(page.getByRole('textbox', { name: 'Python代码' })).toHaveText('print("offline first")');
      await other.reload();
      await expect(other.getByRole('textbox', { name: 'Python代码' })).toHaveText('print("offline second")');
    },
    async unavailableStorageRetry(page) {
      await page.getByRole('textbox', { name: 'Python代码' }).fill('print("storage failure")');
      await expect(page.getByTestId('save-status')).toHaveText('已保存', { timeout: 8000 });
      let requests = 0;
      await page.route('**/api/v1/runs', route => { requests++; return route.abort(); });
      await page.evaluate(() => { Storage.prototype.setItem = () => { throw new DOMException('full', 'QuotaExceededError'); }; });
      await page.getByRole('button', { name: '运行', exact: true }).click();
      await page.getByRole('button', { name: '找回上次运行或提交' }).click();
      await expect(page.getByRole('button', { name: '找回上次运行或提交' })).toBeVisible();
      expect(requests).toBe(0);
    },
    async continuousAutosave(page) {
      const code = page.getByRole('textbox', { name: 'Python代码' });
      await code.fill(''); await expect(page.getByTestId('save-status')).toHaveText('已保存', { timeout: 8000 });
      let saves = 0;
      page.on('request', request => { if (request.method() === 'PUT' && request.url().endsWith('/code-draft')) saves++; });
      await page.clock.install();
      await code.fill('#');
      for (let second = 0; second < 29; second++) {
        await page.clock.runFor(1000); await code.pressSequentially('x');
      }
      expect(saves).toBe(0);
      const before = await code.evaluate(node => { window.editorBeforeSave = node; return getSelection().anchorOffset; });
      await page.clock.runFor(1000);
      await expect.poll(() => saves).toBe(1);
      await expect(page.getByTestId('save-status')).toHaveText('已保存');
      expect(await code.evaluate(node => node === window.editorBeforeSave)).toBe(true);
      expect(await code.evaluate(() => getSelection().anchorOffset)).toBe(before);
    },
    async lostActionReplies(page) {
      const code = page.getByRole('textbox', { name: 'Python代码' });
      for (const [path, label] of [['runs', '运行'], ['submissions', '正式提交']]) {
        await code.fill('print("captured ' + path + '")');
        await expect(page.getByTestId('save-status')).toHaveText('已保存', { timeout: 8000 });
        const sent = [];
        await page.route('**/api/v1/' + path, async route => {
          sent.push(route.request().postDataJSON());
          if (sent.length === 1) { await route.fetch(); await route.abort(); } else await route.continue();
        });
        await page.getByRole('button', { name: label, exact: true }).click();
        await expect(page.getByRole('button', { name: '找回上次运行或提交' })).toBeVisible();
        await page.reload();
        await code.fill('print("edited after ' + path + '")');
        await page.getByRole('button', { name: '找回上次运行或提交' }).click();
        await expect(page.getByRole('button', { name: '找回上次运行或提交' })).not.toBeVisible();
        expect(sent).toHaveLength(2); expect(sent[1]).toEqual(sent[0]);
        await expect(page.getByRole('region', { name: '执行结果' })).toContainText('当前代码已有变化');
        await page.unroute('**/api/v1/' + path);
      }
    },
  };
  const failures = [];
  for (const [name, run] of Object.entries(cases)) {
    const context = await browser.newContext();
    try {
      const page = await context.newPage(); await page.goto(url);
      await expect(page.getByRole('textbox', { name: 'Python代码' })).toBeVisible();
      await run(page); console.log('PASS: ' + name);
    } catch (error) { failures.push(name); console.error('FAIL: ' + name + '\n' + error.message); }
    finally { await context.close(); }
  }
  if (failures.length) throw Error('Recovery failures: ' + failures.join(', '));
} finally {
  if (browser) await browser.close();
  if (server) await new Promise(resolve => server.close(resolve));
  await stopTestProcess(child);
}
