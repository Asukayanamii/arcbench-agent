"""Download the platform's standard task material: requirement YAML, markdown and Playwright suites.

The arc-bench API serves everything the local loop needs:
  GET /api/requirements/{id}?catalog={catalog}        -> requirements_yaml + requirements_markdown
  GET /api/requirements/{id}/tests?catalog={catalog}  -> files: [{path, content}]

Public tasks need no auth; the official competition tasks need the session cookie
(pass it with --cookie). Nothing here calls a model, so it costs no budget.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://arc-bench.com"

TASKS = {
    # cheap smoke targets first: 1 test each, good for a fast local loop
    "smoke--counter": "competition",
    "smoke--dice": "competition",
    "smoke-evolution--counter": "competition",
    # the official hackathon track (needs auth)
    "hackathon--github": "competition",
    "hackathon--sheet": "competition",
    # a mid-size public benchmark, useful as a bigger regression target
    "arc-bench-lite--bookstack": "competition",
}


def fetch(url: str, cookie: str | None) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if cookie:
        request.add_header("Cookie", cookie)
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=r"D:\project\hks\_arcbench_tasks")
    parser.add_argument("--cookie", default="", help="raw Cookie header value from the browser session")
    parser.add_argument("--only", default="", help="comma-separated task ids")
    args = parser.parse_args()

    out_root = Path(args.out)
    wanted = [t.strip() for t in args.only.split(",") if t.strip()] or list(TASKS)

    for task_id in wanted:
        catalog = TASKS.get(task_id, "competition")
        target = out_root / task_id
        target.mkdir(parents=True, exist_ok=True)

        try:
            meta = json.loads(fetch(f"{BASE}/api/requirements/{task_id}?catalog={catalog}", args.cookie))
        except urllib.error.HTTPError as exc:
            print(f"SKIP  {task_id}: HTTP {exc.code} (needs a fresh session cookie?)")
            continue

        yaml_text = meta.get("requirements_yaml") or ""
        if isinstance(yaml_text, (dict, list)):
            import yaml as _yaml

            yaml_text = _yaml.safe_dump(yaml_text, allow_unicode=True, sort_keys=False)

        (target / "requirements.yaml").write_text(yaml_text, encoding="utf-8")
        (target / "requirements.md").write_text(meta.get("requirements_markdown") or "", encoding="utf-8")
        prerequisites = meta.get("prerequisites_markdown") or ""
        if prerequisites:
            (target / "prerequisites.md").write_text(prerequisites, encoding="utf-8")

        try:
            tests = json.loads(fetch(f"{BASE}/api/requirements/{task_id}/tests?catalog={catalog}", args.cookie))
        except urllib.error.HTTPError as exc:
            print(f"WARN  {task_id}: tests HTTP {exc.code}")
            tests = {"files": []}

        files = tests.get("files") or []
        tests_dir = target / "tests"
        for entry in files:
            relative = str(entry.get("path") or "").strip()
            if not relative:
                continue
            destination = (tests_dir / relative).resolve()
            if tests_dir.resolve() not in destination.parents and destination != tests_dir.resolve():
                print(f"WARN  {task_id}: refused suspicious test path {relative}")
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(str(entry.get("content") or ""), encoding="utf-8")

        summary = {
            "id": meta.get("id"),
            "display_id": meta.get("display_id"),
            "title": meta.get("title"),
            "category": meta.get("category"),
            "test_runner": meta.get("test_runner"),
            "total_tests": meta.get("total_tests"),
            "module_count": meta.get("module_count"),
            "assets_base_url": meta.get("assets_base_url"),
            "references_base_url": meta.get("references_base_url"),
            "downloaded_test_files": len(files),
            "yaml_bytes": len(yaml_text.encode("utf-8")),
        }
        (target / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        print(
            f"OK    {task_id}: yaml={summary['yaml_bytes']}B  tests={summary['total_tests']}"
            f"  files={len(files)}  -> {target}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
