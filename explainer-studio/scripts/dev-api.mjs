// Runs the Flask API on :8787 for development (Vite proxies /api to it).
// It reads explainer-studio/.env (ANTHROPIC_API_KEY=..., JEV_API_KEY=...) itself,
// and Flask's debug reloader restarts it when a server file changes.
// First time: create the venv (see CLAUDE.md, "Commands").
import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";

const python = resolve(process.platform === "win32" ? "server/.venv/Scripts/python.exe" : "server/.venv/bin/python");
if (!existsSync(python)) {
  console.error("No Python venv at server/.venv. Create it: py -3.12 -m venv server/.venv, then server/.venv/Scripts/python -m pip install -e \"server[dev]\".");
  process.exit(1);
}
if (!existsSync(resolve(".env"))) console.warn("No .env found; create one with ANTHROPIC_API_KEY=... to enable generation.");

// Bring the database up to date first (development: server/instance/dev.db unless DATABASE_URL is set).
const migrate = spawnSync(python, ["-m", "alembic", "upgrade", "head"], { stdio: "inherit", cwd: resolve("server") });
if (migrate.status !== 0) { console.error("Database migration failed (see above)."); process.exit(1); }

const args = ["-m", "flask", "--app", "explainer.app:create_app", "run", "--port", "8787", "--debug"];
const child = spawn(python, args, { stdio: "inherit", cwd: resolve("server") });
child.on("exit", (code) => process.exit(code ?? 0));
