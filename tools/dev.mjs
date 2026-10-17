import { spawn, spawnSync } from "node:child_process";
import { delimiter, join } from "node:path";
import { fileURLToPath } from "node:url";

// Every service must use this checkout and the same local configuration, even
// when launched from another directory or with a different editable install.
const root = fileURLToPath(new URL("../", import.meta.url));
process.chdir(root);
try {
  process.loadEnvFile(join(root, ".env"));
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}
process.env.PYTHONPATH = [join(root, "src"), process.env.PYTHONPATH].filter(Boolean).join(delimiter);
if (process.env.FOCUSMATE_TEMP_DIR) {
  for (const key of ["TEMP", "TMP", "TMPDIR"]) process.env[key] = process.env.FOCUSMATE_TEMP_DIR;
}
const python = process.env.FOCUSMATE_PYTHON || "python";

const initialized = spawnSync(python, ["-m", "tools.dev_database"], { stdio: "inherit", shell: false });
if (initialized.error || initialized.status !== 0) {
  console.error("Local database initialization failed; services were not started.");
  process.exit(1);
}

const commands = [
  { name: "web", command: process.execPath, args: ["apps/web/server.mjs"] },
  { name: "api", command: python, args: ["-m", "apps.api"] },
  { name: "builder", command: python, args: ["-m", "services.verifier_builder", "--poll", "--build-seconds", "600"] },
  { name: "runs", command: python, args: ["-m", "services.oj_worker", "--task-kind", "runs"] },
  { name: "oj", command: python, args: ["-m", "services.oj_worker"] },
  { name: "coach", command: python, args: ["-m", "services.coach_worker"] },
];

const children = [];
const completions = [];
let shuttingDown = false;
for (const { name, command, args } of commands) {
  const child = spawn(command, args, { stdio: "inherit", shell: false });
  children.push(child);
  completions.push(new Promise(resolve => {
    function stopped(detail) {
      resolve();
      if (shuttingDown) return;
      console.error(`${name} stopped unexpectedly (${detail}); stopping this FocusMate launch.`);
      process.exitCode = 1;
      shutdown();
    }
    child.once("exit", (code, signal) => stopped(signal || `code ${code}`));
    child.once("error", error => stopped(error.code || "spawn error"));
  }));
}

function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const child of children) {
    if (!child.killed) child.kill();
  }
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
process.on("exit", shutdown);

await Promise.all(completions);
