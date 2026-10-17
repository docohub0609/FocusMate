import { stopTestProcess } from '../stop-test-process.mjs';
import { chromium, expect } from "@playwright/test";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { mkdtemp, rm, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, delimiter } from "node:path";
import assert from "node:assert/strict";
import { createWebServer } from "../../apps/web/server.mjs";
if (!process.env.REGRESSION_CASE) {
  for (const name of [
    "baseline",
    "rejected-save",
    "unknown-restore",
    "fast-reparse",
    "numeric-edit",
    "build-states",
    "delayed-confirm",
    "library",
    "io-proposal",
    "failure-details",
  ]) {
    console.log("Browser case:", name);
    const run = spawnSync(process.execPath, [fileURLToPath(import.meta.url)], {
      env: { ...process.env, REGRESSION_CASE: name },
      stdio: "inherit",
    });
    if (run.status !== 0) process.exit(run.status || 1);
  }
  process.exit(0);
}
const temp = await mkdtemp(join(tmpdir(), "focusmate-web-"));
const port = 18381;
const child = spawn(
  process.env.FOCUSMATE_PYTHON || "python",
  ["tools/experiments/t080_browser_fixture.py"],
  {
    env: {
      ...process.env,
      PYTHONPATH: ["src", "."].join(delimiter),
      TEST_API_PORT: String(port),
      FOCUSMATE_DB_PATH: join(temp, "test.db"),
    },
    stdio: "pipe",
  },
);
process.env.FOCUSMATE_API_URL = `http://127.0.0.1:${port}`;
let server, browser;
try {
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch(process.env.FOCUSMATE_API_URL + "/health")).ok) break;
    } catch {}
    if (i === 99) throw Error("fixture not ready");
    await new Promise((r) => setTimeout(r, 100));
  }
  server = await createWebServer({ test: true });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({
    channel: process.env.PLAYWRIGHT_CHANNEL || "msedge",
    headless: true,
  });
  const page = await browser.newPage({
    viewport: { width: 1280, height: 900 },
  });
  const errors = [];
  let buildMode='running';
  let buildFailureCode=process.env.BUILD_FAILURE_CODE || 'LLM_TEMPORARILY_UNAVAILABLE';
  if(process.env.REGRESSION_CASE==='build-states')await page.route('**/verifier-asset-build',route=>buildMode==='offline'?route.abort():route.fulfill({json:{status:buildMode==='limited'?'completed':buildMode==='repairing'?'running':buildMode,phase:buildMode==='repairing'?'repairingCases':null,...(buildMode==='limited'?{assurance:'limited',warningCodes:['MUTATION_CHALLENGE_UNAVAILABLE']}:{}),issueCode:buildMode==='failed'?buildFailureCode:null,retryable:buildMode==='failed',allowedAction:buildMode==='failed'?'retry':'wait',message:'private test detail must not be shown',updatedAt:'2026-09-25T12:00:00Z'}}));
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(base + "/problems/new");
  assert.equal(await page.getByRole("button", { name: "2　核对题目条件", exact: true }).isDisabled(), true);
  assert.equal(await page.getByRole("heading", { name: "核对题目条件", exact: true }).count(), 0);
  const fields = {
    题目描述: "给定数组，输出区间的两个边界。",
    输入说明: "第一行 n，第二行 n 个整数。",
    输出说明: "输出两个整数，分别为左右边界。",
    约束条件: "1 <= n <= 100000",
    样例输入: "3\n1 2 3",
    样例输出: "1 2",
  };
  if (process.env.REGRESSION_CASE === "io-proposal") fields["输入说明"] = "输入一个数组。";
  for (const [label, value] of Object.entries(fields))
    await page.getByLabel(label, { exact: true }).fill(value);
  if (process.env.REGRESSION_CASE === "library")
    await page.getByLabel("题目名称（选填）", { exact: true }).fill("区间边界练习");
  await page.reload();
  assert.equal(
    await page.getByLabel("题目描述", { exact: true }).inputValue(),
    fields["题目描述"],
  );
  if (process.env.REGRESSION_CASE === "rejected-save") {
    await page.getByLabel("题目描述", { exact: true }).fill("   ");
    await page.getByRole("button", { name: "保存并继续", exact: true }).click();
    await page.getByRole("status").waitFor();
    await page.getByLabel("题目描述", { exact: true }).fill(fields["题目描述"]);
  }
  // Let the server save, then lose the response. Retry must recover one problem.
  await page.route("**/api/v1/problems", async (route) => {
    if (route.request().method() === "POST") {
      await route.fetch();
      await route.abort();
    } else await route.continue();
  });
  await page.getByRole("button", { name: "保存并继续", exact: true }).click();
  await page.getByRole("status").waitFor();
  await page.unroute("**/api/v1/problems");
  await page.getByRole("button", { name: "保存并继续", exact: true }).click();
  await page.waitForURL("**/versions/1/review");
  // The URL changes before React finishes rendering the destination page.
  await expect(page.getByRole("heading", { name: "核对题目条件", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "填写题目", exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "准备测试数据", exact: true })).toHaveCount(0);
  assert.equal(await page.getByRole("button", { name: "3　准备测试数据", exact: true }).isDisabled(), true);
  const problemUrl = page.url().replace(/\/review$/, "");
  await page.goto(problemUrl + "/tests");
  await page.getByText("请先核对并确认题目条件，后台才能准备对应的测试数据。", { exact: true }).waitFor();
  await page.getByRole("button", { name: "返回核对题目条件", exact: true }).click();
  assert.equal(
    (await (await fetch(base + "/api/v1/problems")).json()).items.length,
    1,
  );
  if (process.env.REGRESSION_CASE === "library") {
    await page.getByRole("link", { name: "← 所有题目", exact: true }).click();
    await page.getByRole("link").filter({ hasText: "区间边界练习" }).waitFor();
    await page.getByRole("button", { name: "删除题目：区间边界练习", exact: true }).click();
    await page.getByRole("button", { name: "取消", exact: true }).click();
    assert.equal(await page.getByRole("link").filter({ hasText: "区间边界练习" }).count(), 1);
    await page.getByRole("button", { name: "删除题目：区间边界练习", exact: true }).click();
    await page.getByRole("button", { name: "确认删除", exact: true }).click();
    await page.getByText("还没有保存的题目。从录入第一道题开始。", { exact: true }).waitFor();
    await page.reload();
    await page.getByRole("button", { name: "查看已删除题目", exact: true }).click();
    await page.getByRole("button", { name: "恢复题目：区间边界练习", exact: true }).click();
    await page.getByText("没有已删除的题目。", { exact: true }).waitFor();
    await page.getByRole("button", { name: "返回已保存题目", exact: true }).click();
    await page.getByRole("link").filter({ hasText: "区间边界练习" }).click();
    await page.getByRole("heading", { name: "区间边界练习", exact: true }).waitFor();
    assert.equal(await page.getByLabel("题目名称（选填）", { exact: true }).inputValue(), "区间边界练习");
    await page.getByRole("button", { name: "下一步：核对题目条件", exact: true }).click();
    console.log("PASS: optional title, cancel deletion, delete, refresh, restore and reopen");
  }
  await page.getByRole("button", { name: "让 AI 整理条件" }).click();
  await page.reload();
  await page
    .getByRole("button", { name: "确认以上全部内容" })
    .waitFor({ timeout: 15000 });
  if (process.env.REGRESSION_CASE === "failure-details") {
    await page.route("**/feature-extraction", async route => {
      const response = await route.fetch();
      const job = await response.json();
      await route.fulfill({json: {...job, status:"failed", failureCode:"LLM_OUTPUT_INVALID",
        failureDetails:[{field:"features.0.presentation", message:"这项条件的中文显示名称不完整。"}]}});
    });
    await page.reload();
    await page.getByText("查看解析失败原因", {exact:true}).click();
    await page.getByText("这项条件的中文显示名称不完整。（位置：features.0.presentation）", {exact:true}).waitFor();
    await page.unroute("**/feature-extraction");
    await page.reload();
    await page.getByRole("button", {name:"确认以上全部内容", exact:true}).waitFor();
    console.log("PASS: failure details expand and successful report can be reopened");
  }
  if (process.env.REGRESSION_CASE === "io-proposal") {
    const proposalRegion = page.getByRole("region", { name: "输入输出格式建议" });
    await proposalRegion.getByText("下面是 AI 提出的 ACM 格式转换建议，尚未作为题目要求保存。", { exact: true }).waitFor();
    await proposalRegion.getByText("本平台使用标准输入输出：按行和空格组织数据，整数数组不用方括号或逗号。原题中的函数参数或数组表示也需要转换为可直接读写的文本。", { exact: true }).waitFor();
    await proposalRegion.getByText("转换只改变输入输出的表示方式，原题的算法含义、约束和样例数据应保持不变。请对照原文核对后，再决定是否采用。", { exact: true }).waitFor();
    assert.equal(await proposalRegion.getByRole("heading", { name: "原来填写的格式", exact: true }).count(), 1);
    assert.equal(await proposalRegion.getByRole("heading", { name: "建议采用的 ACM 格式", exact: true }).count(), 1);
    assert.equal(await page.getByRole("button", { name: "确认以上全部内容" }).isDisabled(), true);
    await page.getByRole("checkbox", { name: "我已核对，保留原来填写的格式，不采用这份建议" }).check();
    assert.equal(await page.getByRole("button", { name: "确认以上全部内容" }).isEnabled(), true);
    const original = await (await fetch(problemUrl.replace(base, base + "/api/v1"))).json();
    await page.getByRole("button", { name: "将建议填入草稿，继续核对" }).click();
    await page.waitForURL("**/edit");
    const unchanged = await (await fetch(problemUrl.replace(base, base + "/api/v1"))).json();
    assert.equal(unchanged.lifecycle.contentHash, original.lifecycle.contentHash);
    fields["输入说明"] = "第一行 n，第二行 n 个整数，以空格分隔。";
    fields["输出说明"] = "输出两个整数，以单个空格分隔，不含文字前缀。";
    await page.reload();
    assert.equal(await page.getByLabel("输入说明", { exact: true }).inputValue(), fields["输入说明"]);
    await page.getByRole("button", { name: "保存并继续", exact: true }).click();
    await page.waitForURL("**/review");
    const saved = await (await fetch(problemUrl.replace(base, base + "/api/v1"))).json();
    assert.notEqual(saved.lifecycle.contentHash, original.lifecycle.contentHash);
    assert.equal(saved.problemPackage.examples[0].output, "1 2\n");
    await page.getByText("题目格式或内容已修改，请按当前内容重新整理条件。", { exact: true }).waitFor();
    assert.equal(await page.getByText("条件报告暂时无法读取，请刷新重试。原题面仍然保留。", { exact: true }).count(), 0);
    await page.getByRole("button", { name: "让 AI 整理条件" }).click();
    await page.getByRole("button", { name: "确认以上全部内容" }).waitFor({ timeout: 15000 });
    assert.equal(await page.getByRole("region", { name: "输入输出格式建议" }).count(), 0);
    console.log("PASS: ACM conversion policy is visible; IO proposal remains editable, not saved automatically, and reparsed after adoption");
  }
  // Edits survive leaving this step and returning; the original text is separate.
  const minimum = () => page.locator(".feature:visible").first().getByLabel("最小值", { exact: true });
  await minimum().fill("2");
  await page.getByRole("button", { name: "1　填写题目", exact: true }).click();
  await page.waitForURL("**/edit");
  assert.equal(await page.getByRole("heading", { name: "核对题目条件", exact: true }).count(), 0);
  await page.getByLabel("题目描述", { exact: true }).fill(fields["题目描述"] + "本地修改");
  assert.equal(await page.getByRole("button", { name: "2　核对题目条件", exact: true }).isDisabled(), true);
  await page.reload();
  assert.equal(await page.getByLabel("题目描述", { exact: true }).inputValue(), fields["题目描述"] + "本地修改");
  await page.getByLabel("题目描述", { exact: true }).fill(fields["题目描述"]);
  await page.getByRole("button", { name: "2　核对题目条件", exact: true }).click();
  assert.equal(await minimum().inputValue(), "2");
  await minimum().fill("1");
  if (process.env.REGRESSION_CASE === "unknown-restore") {
    const feature = page.locator(".feature").first();
    await feature.getByLabel("这项条件的确定程度").selectOption("unknown");
    await page.reload();
    await feature
      .getByRole("button", { name: "恢复原解析内容" })
      .click({ timeout: 2000 });
    assert.equal(
      await feature.getByLabel("最大值", { exact: true }).inputValue(),
      "100000",
    );
  }
  if (process.env.REGRESSION_CASE === "numeric-edit") {
    const input = page
      .locator(".feature")
      .first()
      .getByLabel("最小值", { exact: true });
    await input.fill("");
    await input.pressSequentially("-20");
    assert.equal(await input.inputValue(), "-20");
    await input.fill("1");
  }
  if (process.env.REGRESSION_CASE === "fast-reparse") {
    let reads = 0;
    page.on("request", (r) => {
      if (r.url().endsWith("/features/display")) reads++;
    });
    await page.route("**/api/v1/feature-extraction-jobs", async (route) => {
      const response = await route.fetch();
      const job = await response.json();
      for (let i = 0; i < 80; i++) {
        const current = await (
          await fetch(base + "/api/v1/feature-extraction-jobs/" + job.jobId)
        ).json();
        if (current.status === "succeeded") break;
        await new Promise((r) => setTimeout(r, 50));
      }
      await route.fulfill({ response });
    });
    // Await the new report itself: parsing and the 2-second status poll can
    // both outlast a fixed sleep, even though the page reloads it correctly.
    await Promise.all([
      page.waitForResponse(
        (response) => response.url().endsWith("/features/display") && response.ok(),
        { timeout: 15000 },
      ),
      page.getByRole("button", { name: "让 AI 整理条件" }).click(),
    ]);
    assert.ok(reads >= 1, "A newly completed task must reload its new report");
  }
  if (process.env.REGRESSION_CASE === "delayed-confirm") {
    let release;
    const hold = new Promise((resolve) => { release = resolve; });
    let saved;
    const serverSaved = new Promise((resolve) => { saved = resolve; });
    await page.route("**/confirm-and-freeze", async (route) => {
      const response = await route.fetch();
      saved();
      await hold;
      await route.fulfill({ response });
    });
    await page.getByRole("button", { name: "确认以上全部内容" }).click();
    await serverSaved;
    assert.equal(await page.getByRole("button", { name: "1　填写题目", exact: true }).isDisabled(), true);
    await page.getByRole("link", { name: "← 所有题目", exact: true }).click();
    await page.getByRole("heading", { name: "先把题目准备好", exact: true }).waitFor();
    release();
    await page.waitForTimeout(1200);
    assert.equal(new URL(page.url()).pathname, "/");
    assert.equal(await page.getByRole("heading", { name: "先把题目准备好", exact: true }).count(), 1);
    console.log("PASS: confirmation locks editing and a late reply does not navigate away from home");
    await page.goto(problemUrl + "/tests");
  } else {
    await page.getByRole("button", { name: "确认以上全部内容" }).click();
  }
  await page.waitForURL("**/tests");
  await page
    .getByRole("heading", { name: "准备测试数据", exact: true })
    .waitFor();
  assert.equal(await page.getByRole("heading", { name: "填写题目", exact: true }).count(), 0);
  assert.equal(await page.getByRole("heading", { name: "核对题目条件", exact: true }).count(), 0);
  await page.reload();
  await page.getByRole("heading", { name: "准备测试数据", exact: true }).waitFor();
  await page.getByRole("button", { name: "1　填写题目", exact: true }).click();
  assert.equal(await page.getByLabel("题目描述", { exact: true }).isDisabled(), true);
  await page.getByRole("button", { name: "2　核对题目条件", exact: true }).click();
  await page.getByRole("heading", { name: "核对题目条件", exact: true }).waitFor();
  assert.equal(await minimum().isDisabled(), true);
  await page.getByRole("button", { name: "3　准备测试数据", exact: true }).click();
  if(process.env.REGRESSION_CASE==='build-states'){
    await page.getByRole('heading',{name:'正在准备与检查',exact:true}).waitFor();
    buildMode='repairing';await page.getByRole('heading',{name:'正在自动修复',exact:true}).waitFor({timeout:6000});
    await page.getByText('已有参考程序已保留，正在修正测试输入；修正后会重新检查。',{exact:true}).waitFor();
    buildMode='offline';await page.getByRole('button',{name:'重新查询',exact:true}).waitFor({timeout:6000});
    assert.equal(await page.getByRole('heading',{name:'正在自动修复',exact:true}).count(),1);
    buildMode='completed';await page.getByRole('button',{name:'重新查询',exact:true}).click();
    await page.getByRole('heading',{name:'测试数据已就绪',exact:true}).waitFor();
    buildMode='limited';await page.reload();
    await page.getByText('测试数据可用于练习，但一次修正后仍未找到合适的错误程序，尚未验证这些测试能否识别这类错误。',{exact:false}).waitFor();
    buildMode='failed';await page.reload();await page.getByRole('button',{name:'重新准备',exact:true}).waitFor();
    if(process.env.BUILD_FAILURE_CODE === 'BUILD_BUDGET_EXCEEDED') await page.getByText('测试准备超过了本次时间限制，尚未完成全部检查。已保存的题目和条件不受影响。',{exact:true}).waitFor();
    for(const [code, message] of [
      ['GENERATOR_CASES_INVALID','生成的测试输入结构或大小不符合要求，暂不能用于判题。可以重新准备。'],
      ['GENERATOR_COVERAGE_INSUFFICIENT','调整测试数据后仍缺少必要的测试类型或数量，尚不能用于判题。可以重新准备。'],
      ['SMALL_REFERENCE_TIMEOUT','调整测试数据后，小用例的参考程序仍执行超时，尚未得到可比较的答案。可以重新准备。'],
      ['GENERATOR_REPAIR_INVALID','AI 未能返回可用的测试数据修正，前面生成的程序已保留。可以重新准备。'],
      ['GENERATOR_INPUT_LEGALITY_UNCONFIRMED','调整后的测试输入尚未确认符合题目条件，暂不能用于判题。可以重新准备。'],
      ['REFERENCE_EXECUTION_FAILED','生成的参考程序未能在执行限制内正常完成，尚未得到可比较的答案。请重新准备。'],
      ['REFERENCE_DISAGREEMENT','生成的两个参考程序给出了不同答案，暂不能用于判题。请重新准备。'],
    ]) {
      buildFailureCode=code;
      await page.reload();
      await page.getByText(message,{exact:true}).waitFor();
      assert.equal(await page.getByRole('heading',{name:'正在自动修复',exact:true}).count(),0);
      assert.equal(await page.getByText('private test detail must not be shown',{exact:true}).count(),0);
    }
    console.log('PASS: explicitly simulated build running/repairing/offline/ready/retry states and specific failures');
  }
  assert.equal(
    await page.getByRole("button", { name: "确认以上全部内容" }).count(),
    0,
  );
  const artifactRoot = process.env.FOCUSMATE_BROWSER_ARTIFACT_ROOT || ".tmp/t080-browser";
  await mkdir(artifactRoot, { recursive: true });
  await page.screenshot({
    path: join(artifactRoot, "confirmed.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: join(artifactRoot, "mobile.png"),
    fullPage: true,
  });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  assert.deepEqual(errors, []);
  console.log(
    "PASS: browser draft, lost-save retry, parse refresh, full confirmation, frozen view, narrow layout",
  );
} finally {
  await browser?.close();
  if (server) await new Promise((r) => server.close(r));
  await stopTestProcess(child);
  await rm(temp, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
}
