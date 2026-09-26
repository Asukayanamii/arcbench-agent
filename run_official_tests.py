"""Run a platform-standard Playwright suite against a locally compiled application.

This closes the local loop the competition expects:

    download requirements + tests   ->   compile with a real model key   ->   run the
    official suite against the compiled app   ->   read the per-requirement pass rate

The official hackathon suites (hackathon--github / hackathon--sheet) are NOT downloadable
- the platform keeps those server-side - so this runner is for the public tracks
(smoke, ticket-booking, arc-bench-lite, arc-bench-web). It is the same Playwright contract
though, so a task that passes here is exercising the same surface the scorer uses.

Usage:
    python run_official_tests.py --task smoke--counter --app D:\\out\\smoke --port 3310
    python run_official_tests.py --task arc-bench-lite--bookstack --app D:\\out\\bs --port 3311
    python run_official_tests.py --task smoke--counter --app D:\\out\\smoke --no-build
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS_ROOT = HERE / "_arcbench_tasks"
HARNESS = HERE / "_arcbench_harness"

PLAYWRIGHT_CONFIG = """import {{ defineConfig }} from '@playwright/test';

// Benchmark suites run sequentially against one target URL; the platform uses the
// same shape (one worker, no retries) so state-changing scenarios stay deterministic.
export default defineConfig({{
  testDir: './tests',
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 60_000,
  expect: {{ timeout: 10_000 }},
  reporter: [['list'], ['json', {{ outputFile: 'results.json' }}]],
  use: {{
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:{port}',
    headless: true,
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  }},
}});
"""

PACKAGE_JSON = """{
  "name": "arcbench-official-harness",
  "private": true,
  "version": "1.0.0"
}
"""


def run(command: str, cwd: Path, env: dict[str, str] | None = None, timeout: float | None = None) -> tuple[int, str]:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **(env or {})},
        timeout=timeout,
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def wait_for_port(host: str, port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/", timeout=3):
                return True
        except urllib.error.HTTPError:
            return True  # any HTTP answer means something is listening
        except Exception:
            time.sleep(0.5)
    return False


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    else:
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass


def ensure_harness(port: int) -> None:
    """Create/reuse the Playwright harness project (npm install happens once)."""

    HARNESS.mkdir(parents=True, exist_ok=True)
    (HARNESS / "package.json").write_text(PACKAGE_JSON, encoding="utf-8")

    if not (HARNESS / "node_modules" / "@playwright" / "test").exists():
        print("[harness] installing @playwright/test (first run only)...")
        code, output = run("npm install --no-audit --no-fund @playwright/test", HARNESS, timeout=900)
        if code != 0:
            print(output[-3000:])
            raise SystemExit("[harness] npm install failed")
        code, output = run("npx playwright install chromium", HARNESS, timeout=900)
        if code != 0:
            print(output[-3000:])
            print("[harness] browser install reported a problem; continuing")

    # baseURL default tracks the requested port
    (HARNESS / "playwright.config.ts").write_text(
        PLAYWRIGHT_CONFIG.format(port=port), encoding="utf-8"
    )


def stage_tests(task: str) -> int:
    source = TASKS_ROOT / task / "tests"
    if not source.is_dir():
        raise SystemExit(f"no downloaded tests for {task}; run _fetch_tasks.py first")

    target = HARNESS / "tests"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return sum(1 for _ in target.rglob("*.spec.ts"))


def build_and_start(app: Path, port: int) -> subprocess.Popen | None:
    frontend = app / "frontend"
    backend = app / "backend"

    if frontend.is_dir():
        print("[app] building frontend ...")
        code, output = run("npm run build", frontend, timeout=600)
        if code != 0:
            print(output[-2500:])
            print("[app] frontend build FAILED (the tests will not see a served bundle)")
        else:
            print("[app] frontend build ok")

    if not backend.is_dir():
        raise SystemExit(f"no backend/ under {app}")

    print(f"[app] starting backend on port {port} ...")
    process = subprocess.Popen(
        "npm run start",
        cwd=str(backend),
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PORT": str(port)},
        start_new_session=True,
    )

    if not wait_for_port("127.0.0.1", port, timeout=90):
        print(f"[app] backend did not answer on {port}; last output:")
        kill_tree(process.pid)
        raise SystemExit("[app] startup failed")
    print(f"[app] backend is up on http://127.0.0.1:{port}")
    return process


def summarise(results_path: Path) -> tuple[int, int, list[str]]:
    if not results_path.is_file():
        return 0, 0, []
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    passed = failed = 0
    failures: list[str] = []
    for suite in payload.get("suites") or []:
        for spec in _walk_specs(suite):
            for test in spec.get("tests") or []:
                title = spec.get("title") or ""
                outcome = test.get("status")
                if outcome == "expected":
                    passed += 1
                elif outcome in {"unexpected", "flaky"}:
                    failed += 1
                    failures.append(title)
    return passed, failed, failures


def _walk_specs(suite: dict):
    for spec in suite.get("specs") or []:
        yield spec
    for child in suite.get("suites") or []:
        yield from _walk_specs(child)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--app", required=True, help="compiled output directory")
    parser.add_argument("--port", type=int, default=3310)
    parser.add_argument("--no-build", action="store_true", help="skip the frontend rebuild")
    parser.add_argument("--keep-running", action="store_true", help="leave the app up after the run")
    parser.add_argument("--grep", default="", help="only run matching tests")
    args = parser.parse_args()

    app = Path(args.app).resolve()
    if not app.is_dir():
        raise SystemExit(f"compiled app not found: {app}")

    ensure_harness(args.port)
    spec_count = stage_tests(args.task)
    print(f"[tests] staged {spec_count} spec file(s) from {args.task}")

    process = None
    if not args.no_build:
        process = build_and_start(app, args.port)

    env = {"PLAYWRIGHT_BASE_URL": f"http://127.0.0.1:{args.port}"}
    command = "npx playwright test"
    if args.grep:
        command += f' --grep "{args.grep}"'

    try:
        code, output = run(command, HARNESS, env=env, timeout=3600)
        print(output)
    finally:
        if process is not None and not args.keep_running:
            print("[app] stopping backend")
            kill_tree(process.pid)

    passed, failed, failures = summarise(HARNESS / "results.json")
    total = passed + failed
    print("=" * 72)
    print(f"task        : {args.task}")
    print(f"app         : {app}")
    print(f"passed      : {passed}")
    print(f"failed      : {failed}")
    if total:
        print(f"pass rate   : {passed / total:.1%}")
    if failures:
        print("failed tests:")
        for title in failures[:40]:
            print(f"  - {title}")
    print(f"playwright exit code: {code}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
