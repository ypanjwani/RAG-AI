// Runs the server's pytest suite with its venv (server/.venv): npm run test:server [-- pytest args].
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";

const python = resolve(process.platform === "win32" ? "server/.venv/Scripts/python.exe" : "server/.venv/bin/python");
if (!existsSync(python)) {
  console.error("No Python venv at server/.venv (see CLAUDE.md, \"Commands\").");
  process.exit(1);
}
const r = spawnSync(python, ["-m", "pytest", "-q", ...process.argv.slice(2)], { stdio: "inherit", cwd: resolve("server") });
process.exit(r.status ?? 1);
