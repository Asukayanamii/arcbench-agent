"""Diagnostic entrypoint that reports exactly what the platform extracted.

Costs no model tokens: it makes no LLM calls and only inspects the filesystem.
Packaged with `--diagnostic` so it keeps the real submission's directory layout.

Status is written to stdout, stderr, a file next to the entrypoint, AND the output
directory - whichever the platform chooses to surface, the answer shows up.
"""

from __future__ import annotations

import json
import os
import sys


def tree(root: str, max_depth: int = 3) -> list[str]:
    lines: list[str] = []
    base = len(root.rstrip(os.sep).split(os.sep))
    for current, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        depth = len(current.rstrip(os.sep).split(os.sep)) - base
        if depth >= max_depth:
            dirnames[:] = []
        indent = "  " * depth
        lines.append(f"{indent}{os.path.basename(current) or current}/")
        for name in filenames[:30]:
            lines.append(f"{indent}  {name}")
        if len(filenames) > 30:
            lines.append(f"{indent}  ... {len(filenames) - 30} more")
    return lines


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))

    report = {
        "python": sys.version,
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "entrypoint_dir": here,
        "argv": sys.argv[1:],
        "sys_path_first_5": sys.path[:5],
        "top_level_entries": sorted(os.listdir(here)),
        "expected_packages": {},
        "import_probe": {},
    }

    for name in ("app", "core", "agents", "app_type_handler", "arcbench_agent_runtime", "skills", "arc-template"):
        path = os.path.join(here, name)
        report["expected_packages"][name] = {
            "is_dir": os.path.isdir(path),
            "has_init": os.path.isfile(os.path.join(path, "__init__.py")),
            "entry_count": len(os.listdir(path)) if os.path.isdir(path) else 0,
        }

    for module in ("arc_main", "app_type_handler", "core.files", "agents.runtime.factory", "app.agent.graph"):
        try:
            __import__(module)
            report["import_probe"][module] = "OK"
        except Exception as exc:  # noqa: BLE001 - the failure text is the payload
            report["import_probe"][module] = f"{type(exc).__name__}: {exc}"

    report["tree_depth3"] = tree(here, max_depth=3)
    payload = json.dumps(report, ensure_ascii=False, indent=2)

    banner = "===== ARC-BENCH SUBMISSION DIAGNOSTIC ====="
    print(banner)
    print(payload)
    print("===== END DIAGNOSTIC =====")
    print(payload, file=sys.stderr)

    for target in (
        os.path.join(here, "diagnostic.json"),
        os.path.join(os.path.dirname(here), "diagnostic.json"),
    ):
        try:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(payload)
            print("wrote", target)
        except OSError as exc:
            print("could not write", target, "->", exc)

    # Also drop it where the agent is expected to produce output.
    if "--output-dir" in sys.argv:
        out = sys.argv[sys.argv.index("--output-dir") + 1]
        try:
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, "diagnostic.json"), "w", encoding="utf-8") as handle:
                handle.write(payload)
            print("wrote", os.path.join(out, "diagnostic.json"))
        except OSError as exc:
            print("could not write into output dir ->", exc)


if __name__ == "__main__":
    main()
