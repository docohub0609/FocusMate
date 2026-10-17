import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

function runLauncher(initializationStatus = 0, crash = false, pythonOverride = null) {
  // Keep the launcher's ordering and shutdown real, replacing only process creation
  // so this test cannot open the user's database, ports, Docker or model service.
  const program = `
    import childProcess from "node:child_process";
    import { syncBuiltinESMExports } from "node:module";
    import { EventEmitter } from "node:events";
    const events = [];
    process.on("exit", () => process.stdout.write(JSON.stringify(events)));
    process.loadEnvFile = (path) => {
      events.push({ operation: "environment", path });
      if (${JSON.stringify(pythonOverride)} !== null) process.env.FOCUSMATE_PYTHON = ${JSON.stringify(pythonOverride)};
      process.env.FOCUSMATE_DB_PATH = "isolated-test.db";
      process.env.FOCUSMATE_TEMP_DIR = "chosen-temp-directory";
    };
    childProcess.spawnSync = (command, args) => {
      events.push({ operation: "initialize", command, args, cwd: process.cwd(), pythonPath: process.env.PYTHONPATH, database: process.env.FOCUSMATE_DB_PATH, temporary: [process.env.TEMP, process.env.TMP, process.env.TMPDIR] });
      return { status: ${initializationStatus} };
    };
    childProcess.spawn = (command, args) => {
      events.push({ operation: "start", command, args, cwd: process.cwd(), pythonPath: process.env.PYTHONPATH, database: process.env.FOCUSMATE_DB_PATH, temporary: [process.env.TEMP, process.env.TMP, process.env.TMPDIR] });
      const child = new EventEmitter();
      child.killed = false;
      child.kill = () => {
        child.killed = true;
        events.push({ operation: "stop", command, args });
        child.emit("exit", 0);
      };
      if (events.filter(event => event.operation === "start").length === 1) {
        queueMicrotask(() => ${crash ? 'child.emit("exit", 1)' : 'process.emit("SIGTERM")'});
      }
      return child;
    };
    syncBuiltinESMExports();
    await import(${JSON.stringify(new URL("../../tools/dev.mjs", import.meta.url).href)});
  `;
  const result = spawnSync(process.execPath, ["--input-type=module", "--eval", program], {
    encoding: "utf8", timeout: 10000, cwd: tmpdir(),
  });
  assert.ifError(result.error);
  return { ...result, events: JSON.parse(result.stdout) };
}

test("development launcher starts and stops the test builder with the other services", () => {
  const result = runLauncher();
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(result.events.find(event => event.operation === "initialize").args, ["-m", "tools.dev_database"]);
  const started = result.events.filter(event => event.operation === "start").map(event => event.args);
  assert.deepEqual(started, [
    ["apps/web/server.mjs"],
    ["-m", "apps.api"],
    ["-m", "services.verifier_builder", "--poll", "--build-seconds", "600"],
    ["-m", "services.oj_worker", "--task-kind", "runs"],
    ["-m", "services.oj_worker"],
    ["-m", "services.coach_worker"],
  ]);
  const stopped = result.events.filter(event => event.operation === "stop").map(event => event.args);
  assert.deepEqual(stopped, started);
});

test("development launcher starts no service when database initialization fails", () => {
  const result = runLauncher(1);
  assert.equal(result.status, 1);
  assert.equal(result.events.filter(event => event.operation === "initialize").length, 1);
  assert.equal(result.events.filter(event => event.operation === "start").length, 0);
});


test("worker failure stops only the services owned by this launcher", () => {
  const result = runLauncher(0, true);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /web.*unexpectedly/);
  assert.equal(result.events.filter(event => event.operation === "stop").length, 6);
});

test("configuration is loaded before initialization and shared by every backend", () => {
  const result = runLauncher(0, false, "chosen-python");
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.events[0].operation, "environment");
  const initialize = result.events.find(event => event.operation === "initialize");
  assert.equal(resolve(initialize.cwd), resolve(fileURLToPath(new URL("../../", import.meta.url))));
  const started = result.events.filter(event => event.operation === "start");
  for (const event of [initialize, ...started]) {
    assert.equal(event.cwd, initialize.cwd);
    assert.equal(event.database, "isolated-test.db");
    assert.deepEqual(event.temporary, Array(3).fill("chosen-temp-directory"));
    assert.match(event.pythonPath, /[\\/]src(?:;|:|$)/);
    if (event.args[0] === "-m") assert.equal(event.command, "chosen-python");
  }
});
