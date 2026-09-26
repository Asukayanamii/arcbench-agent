"""Regression test for the mid-stream stall that aborted a whole platform run.

Production failure (run of 2026-09-25 12:04, node REQ-1.1 IMPLEMENT):

    langchain_openai...StreamChunkTimeoutError: No streaming chunk received for
    120.0s (model=deepseek-v4-flash, chunks_received=5164)
    -> retry.py: "模型调用 失败但已经吐出内容，不再重试"
    -> propagated out of _run_task -> out of the compile loop -> exit status 1

Three separate defects, each with its own check group below:

  1. retry.py refused to retry a *stalled* stream because tokens had been emitted.
     The no-retry-after-output rule exists so a complete answer isn't fetched twice;
     an interrupted stream is a truncated answer, and no AIMessage was ever built,
     so no tool ran and retrying repeats nothing.
  2. the SDK's 120s stall watchdog was left at its default -- too tight for this gateway.
  3. an exception raised inside a phase escaped `compile_requirement_tree`'s loop and
     killed every remaining node, which is the expensive part: the score is a pass rate.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(r"D:\project\hks\my_agent")
WORKSPACE = Path(r"D:\project\hks\_stall_test")

sys.path.insert(0, str(REPO))

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + label + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


class _StallError(Exception):
    """The production wording, but under a neutral class name.

    This is the case that matters: the message contains no word `RETRYABLE_PATTERN`
    knows, so classifying it correctly must come from the interrupted-stream rule
    rather than from the class name happening to contain "timeout".
    """

    def __init__(self) -> None:
        super().__init__(
            "No streaming chunk received for 120.0s (model=deepseek-v4-flash, "
            "chunks_received=5164). The connection may be alive at the TCP layer but "
            "is not producing content."
        )


class StreamChunkTimeoutError(Exception):
    """The real langchain-openai class name, which contains "timeout"."""

    def __init__(self) -> None:
        super().__init__("No streaming chunk received for 120.0s")


class _GatewayError(Exception):
    """A non-stream failure that arrives after output: must still NOT be retried."""

    def __init__(self) -> None:
        super().__init__("400 - invalid request: messages[3].role is not allowed")


def _fake_model(raise_with, *, succeed_on: int = 2, emit_tokens: bool = True):
    class _Model:
        def __init__(self) -> None:
            self.calls = 0

        async def ainvoke(self, messages, config=None, **kwargs):  # noqa: ANN001
            self.calls += 1
            if self.calls < succeed_on:
                if emit_tokens:
                    for handler in (config or {}).get("callbacks") or []:
                        handler.on_llm_new_token("partial")
                raise raise_with()
            return f"ok-after-{self.calls}-calls"

    return _Model()


def check_retry_layer() -> None:
    from app.agent import retry as retry_module

    print("=== 1. classification ===")
    check("stream chunk timeout is recognised as an interrupted stream",
          retry_module.stream_was_interrupted(_StallError()))
    check("  ... via the exception class name too",
          retry_module.stream_was_interrupted(StreamChunkTimeoutError()))
    check("connection reset mid-stream is an interrupted stream",
          retry_module.stream_was_interrupted(Exception("Connection reset by peer")))
    check("a plain API error is NOT an interrupted stream",
          not retry_module.stream_was_interrupted(_GatewayError()))

    print()
    print("--- retryability (the load-bearing half) ---")
    check("a stalled stream is retryable from the message alone"
          " (no retryable word in the text, neutral class name)",
          retry_module.is_retryable(_StallError()))
    check("  ... and from the real class name", retry_module.is_retryable(StreamChunkTimeoutError()))
    check("a plain API error is still not retryable",
          not retry_module.is_retryable(_GatewayError()))

    print()
    print("=== 2. retry after a stalled stream (the production failure) ===")
    original_sleep = retry_module._sleep
    slept: list[float] = []

    async def _no_wait(seconds: float) -> None:
        slept.append(seconds)

    retry_module._sleep = _no_wait
    try:
        model = _fake_model(_StallError, succeed_on=2)
        result = asyncio.run(
            retry_module.ainvoke_with_retry(model, [], label="模型调用", thread_id="t")
        )
        check("the stalled call was retried and succeeded", result == "ok-after-2-calls",
              f"result={result!r} calls={model.calls}")
        check("exactly one retry was spent", model.calls == 2, f"calls={model.calls}")
        check("backoff was applied between attempts", slept == [2.0], f"slept={slept}")

        print()
        print("=== 3. the old rule still holds for non-stream failures ===")
        slept.clear()
        model = _fake_model(_GatewayError, succeed_on=5)
        raised = False
        try:
            asyncio.run(
                retry_module.ainvoke_with_retry(model, [], label="模型调用", thread_id="t")
            )
        except _GatewayError:
            raised = True
        check("a non-stream error after output is still not retried", raised)
        check("  ... and consumed only one call", model.calls == 1, f"calls={model.calls}")

        print()
        print("=== 4. retries are still bounded for a stream that never recovers ===")
        slept.clear()
        model = _fake_model(_StallError, succeed_on=99)
        raised = False
        try:
            asyncio.run(
                retry_module.ainvoke_with_retry(model, [], label="模型调用", thread_id="t")
            )
        except _StallError:
            raised = True
        check("a permanently stalled stream eventually gives up", raised)
        check("  ... after the full retry budget", model.calls == retry_module.MAX_RETRIES + 1,
              f"calls={model.calls}")
    finally:
        retry_module._sleep = original_sleep


def check_timeout_config() -> None:
    print()
    print("=== 5. the stall watchdog is no longer 120s ===")

    import importlib
    import os

    # Must be set *before* the reload: config reads the environment at import time.
    os.environ["LLM_API_KEY"] = "k"
    os.environ["LLM_BASE_URL"] = "http://127.0.0.1:1/v1"

    def load(value: str | None):
        from app import config as config_module
        from app.agent import models as models_module

        if value is None:
            os.environ.pop("LLM_STREAM_CHUNK_TIMEOUT", None)
        else:
            os.environ["LLM_STREAM_CHUNK_TIMEOUT"] = value
        importlib.reload(config_module)
        importlib.reload(models_module)
        return config_module, models_module

    try:
        config_module, models_module = load(None)
        check("default is wider than the SDK's 120s",
              config_module.LLM_STREAM_CHUNK_TIMEOUT == 300,
              f"{config_module.LLM_STREAM_CHUNK_TIMEOUT}")

        model = models_module.build_model()
        check("the value reaches the model client",
              getattr(model, "stream_chunk_timeout", None) == 300.0,
              f"{type(model).__name__}.stream_chunk_timeout="
              f"{getattr(model, 'stream_chunk_timeout', None)}")

        config_module, models_module = load("0")
        check("0 disables the watchdog", config_module.LLM_STREAM_CHUNK_TIMEOUT is None,
              str(config_module.LLM_STREAM_CHUNK_TIMEOUT))
        check("  ... and reaches the client as None",
              getattr(models_module.build_model(), "stream_chunk_timeout", "missing") is None)

        config_module, _ = load("nonsense")
        check("a bad value falls back to the default",
              config_module.LLM_STREAM_CHUNK_TIMEOUT == 300,
              str(config_module.LLM_STREAM_CHUNK_TIMEOUT))
    finally:
        os.environ.pop("LLM_STREAM_CHUNK_TIMEOUT", None)
        os.environ.pop("LLM_API_KEY", None)
        os.environ.pop("LLM_BASE_URL", None)
        load(None)


def _log_cb(agent: str, message: str, status=None, node_id=None):  # noqa: ANN001
    logs.append(f"{agent}|{status or ''}|{message}")
    return None


logs: list[str] = []


def check_workflow_guard() -> None:
    print()
    print("=== 6. a phase that raises no longer aborts the whole compile ===")

    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE, ignore_errors=True)
    (WORKSPACE / "frontend" / "src").mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "package.json").write_text('{"name":"ws"}\n', encoding="utf-8")

    # The real compile path initialises git and commits a checkpoint after every phase,
    # so the failure path has to run against a real repo or it dies on `git commit`.
    subprocess.run(["git", "init", "-q"], cwd=str(WORKSPACE), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(WORKSPACE),
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(WORKSPACE),
                   check=True, capture_output=True)

    from core.service import configure_runtime
    from core.workflow import ARCWorkflowManager

    manager = ARCWorkflowManager(
        workspace_path=str(WORKSPACE), app_type="web", web_port=3307, log_cb=_log_cb
    )
    runtime = configure_runtime(
        project_dir=str(WORKSPACE),
        traceability_dir=str(WORKSPACE / ".arc" / "traceability"),
        app_type="web",
        web_port=3307,
    )
    runtime.traceability.init_store(reset=True)
    manager.runtime = runtime

    attempted: list[tuple[str, str]] = []

    async def exploding_run_task(task: dict) -> bool:
        attempted.append((task["node_id"], task["phase"]))
        if len(attempted) == 1:
            raise _StallError()
        return False

    manager._run_task = exploding_run_task

    tree = {
        "id": "REQ-1",
        "name": "shell",
        "description": "Root shell.",
        "children": [
            {"id": "REQ-1.1", "name": "child", "description": "Child feature.", "children": []}
        ],
    }

    try:
        result = asyncio.run(manager.compile_requirement_tree(tree))
        escaped = None
    except Exception as exc:  # noqa: BLE001
        result, escaped = None, exc

    check("the exception did not escape compile_requirement_tree", escaped is None,
          f"{type(escaped).__name__}: {escaped}" if escaped else "")
    check("a compile result was still produced", isinstance(result, dict))

    raised_line = [line for line in logs if "raised for node" in line]
    print("  log line:", raised_line[0][:160] if raised_line else "(none)")
    check("the raised phase was recorded as a node failure", bool(raised_line))
    check("the loop kept going instead of stopping at the first failure",
          len(attempted) > 1, f"{len(attempted)} task(s): {attempted}")


def main() -> int:
    check_retry_layer()
    check_timeout_config()
    check_workflow_guard()

    print()
    print("result: " + ("ALL OK" if not failures else f"{len(failures)} FAILED -> {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
