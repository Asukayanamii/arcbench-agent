from __future__ import annotations

import json
import traceback as traceback_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def write_failure_report(
    *,
    output_dir: str,
    requirement_path: str,
    elapsed_seconds: float,
    result: dict[str, Any] | None = None,
    exception: BaseException | None = None,
    exception_traceback: str | None = None,
) -> dict[str, str]:
    """Write failure details in the workspace root where runners collect artifacts."""
    result = result or {}
    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    details = list(result.get("failure_details") or [])
    if exception is not None:
        details.append(
            {
                "kind": "unhandled_exception",
                "error_type": type(exception).__name__,
                "error": str(exception) or repr(exception),
                "traceback": exception_traceback or traceback_module.format_exc(),
            }
        )
    if not details and not result.get("ok", True):
        details.append(
            {
                "kind": "compilation_failure",
                "error": "Compilation returned ok=false without a node-level failure detail.",
            }
        )

    failed_nodes = [str(item) for item in result.get("failed_nodes") or []]
    failed_operations = [str(item) for item in result.get("failed_operations") or []]
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "failed",
        "elapsed_seconds": round(float(elapsed_seconds), 3),
        "requirement_path": str(Path(requirement_path).expanduser().resolve()),
        "output_dir": str(output_path),
        "failed_nodes": failed_nodes,
        "failed_operations": failed_operations,
        "states": result.get("states") or {},
        "failure_details": details,
        "artifacts": {
            "processing_queue": str(output_path / ".arc" / "processing_queue.json"),
            "node_sessions_dir": str(output_path / ".arc" / "node_sessions"),
            "debug_log": str(output_path / ".arc" / "debug.log"),
        },
    }
    json_path = output_path / "submission_failure_report.json"
    txt_path = output_path / "submission_failure_report.txt"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    lines = [
        "ARC SUBMISSION FAILURE REPORT",
        f"generated_at: {report['generated_at']}",
        f"requirement_path: {report['requirement_path']}",
        f"output_dir: {report['output_dir']}",
        f"elapsed_seconds: {report['elapsed_seconds']}",
        f"failed_nodes: {', '.join(failed_nodes) or 'none recorded'}",
        f"failed_operations: {', '.join(failed_operations) or 'none recorded'}",
        "",
        "FAILURE DETAILS:",
    ]
    for index, detail in enumerate(details, start=1):
        lines.append(f"[{index}] {detail.get('kind', 'failure')}")
        for key in ("node_id", "phase", "error_type", "error", "summary", "traceback"):
            value = detail.get(key)
            if value:
                lines.append(f"{key}: {value}")
        lines.append("")
    lines.extend(
        [
            "ARTIFACTS:",
            f"processing_queue: {report['artifacts']['processing_queue']}",
            f"node_sessions_dir: {report['artifacts']['node_sessions_dir']}",
            f"debug_log: {report['artifacts']['debug_log']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "text": str(txt_path)}
