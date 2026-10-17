import { readFile, readdir } from "node:fs/promises";
import { extname, join, relative, resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const activeRoots = ["apps", "src", "services", "skills", "schemas", "tests", "tools"];
const sourceExtensions = new Set([".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".py"]);
const legacyPath = ["test", "legacy-mvp"].join("/");
const violations = [];

async function walk(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) files.push(...(await walk(path)));
    else if (sourceExtensions.has(extname(entry.name))) files.push(path);
  }
  return files;
}

for (const activeRoot of activeRoots) {
  const files = await walk(join(root, activeRoot));
  for (const file of files) {
    const content = await readFile(file, "utf8");
    const displayPath = relative(root, file).replaceAll("\\", "/");

    if (content.includes(legacyPath) && displayPath !== "tests/python/test_project_boundaries.py") {
      violations.push(`${displayPath}: active V1 code must not reference ${legacyPath}`);
    }

    if (displayPath.startsWith("src/focusmate/adapters/") && /(?:focusmate[./]core|\.\.core)/.test(content)) {
      violations.push(`${displayPath}: adapters must not import core orchestration`);
    }

    if (displayPath.startsWith("services/oj_worker/") && /(?:apps[./]api|focusmate[./]core)/.test(content)) {
      violations.push(`${displayPath}: OJ worker must remain independent from API/core orchestration`);
    }
  }
}

if (violations.length > 0) {
  console.error("Architecture boundary check failed:\n" + violations.map((item) => `- ${item}`).join("\n"));
  process.exitCode = 1;
} else {
  console.log("Architecture boundary check passed.");
}
