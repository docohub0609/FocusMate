// README screenshot from an isolated, scripted fixture. No user data or model calls.
import { chromium, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, delimiter } from 'node:path';
import { createWebServer } from '../apps/web/server.mjs';
import { stopTestProcess } from './stop-test-process.mjs';

const temp = await mkdtemp(join(tmpdir(), 'focusmate-preview-'));
const child = spawn(process.env.FOCUSMATE_PYTHON || 'python', ['tools/preview_fixture.py'], {
  env: { ...process.env, PYTHONPATH: [process.cwd(), join(process.cwd(), 'src')].join(delimiter),
    PYTHONUTF8: '1', FOCUSMATE_DB_PATH: join(temp, 'fixture.db') },
  stdio: ['ignore', 'pipe', 'pipe'],
});
let server, browser;
try {
  const port = await new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => reject(Error('Preview fixture did not start')), 30000);
    child.stdout.on('data', data => {
      output += data;
      try { const value = JSON.parse(output.trim()); clearTimeout(timer); resolve(value.port); } catch {}
    });
    child.stderr.on('data', data => process.stderr.write(data));
    child.once('exit', code => { clearTimeout(timer); reject(Error('Fixture exit ' + code)); });
  });
  process.env.FOCUSMATE_API_URL = 'http://127.0.0.1:' + port;
  server = await createWebServer({ test: true });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  const page = await browser.newPage({ viewport: { width: 1560, height: 1000 }, deviceScaleFactor: 1 });
  await page.goto('http://127.0.0.1:' + server.address().port + '/problems/probe-problem/versions/1/code');
  const editor = page.getByRole('textbox', { name: 'Python代码', exact: true });
  await expect(editor).toBeVisible();
  await editor.fill('numbers = list(map(int, input().split()))\ntotal = 0\nfor number in numbers:\n    total += number\nprint(total)\n');
  await expect(page.getByTestId('save-status')).toHaveText('已保存', { timeout: 10000 });
  await page.getByRole('button', { name: '向教练求助' }).click();
  await expect(page.getByRole('complementary', { name: '刷题教练' })).toBeVisible();
  await mkdir('docs/assets', { recursive: true });
  await page.screenshot({ path: 'docs/assets/editor.png', fullPage: false });
  console.log('Captured isolated preview; no model claims or personal data.');
} finally {
  await browser?.close();
  if (server) await new Promise(resolve => server.close(resolve));
  await stopTestProcess(child);
}
