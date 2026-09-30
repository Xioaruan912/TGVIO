import { spawnSync } from "node:child_process";
import { rmSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

const output = fileURLToPath(new URL("../.test-dist/", import.meta.url));
const compiler = fileURLToPath(new URL("../node_modules/typescript/bin/tsc", import.meta.url));
const root = fileURLToPath(new URL("../", import.meta.url));
let status = 1;
try {
  rmSync(output, { recursive: true, force: true });
  const compile = spawnSync(process.execPath, [compiler, "-p", "tsconfig.test.json"], { cwd: root, stdio: "inherit" });
  if (compile.error) throw compile.error;
  status = compile.status ?? 1;
  if (status === 0) {
    const tests = spawnSync(process.execPath, ["--test", ...readdirSync(root + "tests").filter((name) => name.endsWith(".test.mjs")).sort().map((name) => "tests/" + name)], { cwd: root, stdio: "inherit" });
    if (tests.error) throw tests.error;
    status = tests.status ?? 1;
  }
} finally {
  rmSync(output, { recursive: true, force: true });
}
process.exitCode = status;
