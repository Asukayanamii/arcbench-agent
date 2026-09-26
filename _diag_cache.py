"""Scan a platform run log for per-call prompt-cache usage, flag collapses.

Issue #4's acceptance asks for a before/after comparison on one real run's
`usage_metadata`. The run logs (`new log.txt` / `new ticket log.txt`) are
local-only and not in this repo, so this script travels instead: point it at
any run log (or `GET /api/runs/{id}/logs` output) and it extracts every
usage dict, computes the cache hit rate, and flags the collapse signature
observed in production:

    input_tokens large (>= 20K) while cache_read stays at the ~4096 head

Before the fix, the first call after a successful compaction shows exactly
that signature (and its input shrinks versus the previous call, because the
summary replaced a big middle section). After the fix
(`summary_message`: the summary rides as a standalone user message instead of
being appended to the system prompt), the post-compaction call keeps hitting
cache through "tool definitions + system + first user message", so no entry
should sit at the 4096 floor anymore.

Usage:
    python _diag_cache.py "new log.txt"
    python _diag_cache.py "new ticket log.txt" --min-input 10000 --max-hit 0.5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

INPUT_RE = re.compile(r'"input_tokens"')
FIND_INT = re.compile(r'"input_tokens"\s*:\s*(\d+)')
FIND_CACHE = re.compile(r'"cache_read(?:_input_tokens)?"\s*:\s*(\d+)')

MIN_INPUT = 20_000
MAX_HIT = 0.15


def _decode_object_at(text: str, open_brace: int) -> dict | None:
    """Decode the JSON object starting at `open_brace`; None if it isn't one."""
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(text, open_brace)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def extract_usage(text: str) -> list[dict]:
    """Pull every dict that carries an input_tokens count, with its line number."""
    found: list[dict] = []
    seen: set[int] = set()
    for match in INPUT_RE.finditer(text):
        open_brace = text.rfind("{", 0, match.start())
        while open_brace != -1 and open_brace in seen:
            open_brace = text.rfind("{", 0, open_brace)
        if open_brace == -1 or open_brace in seen:
            continue
        seen.add(open_brace)
        payload = _decode_object_at(text, open_brace)
        if payload is None or "input_tokens" not in payload:
            continue
        details = payload.get("input_token_details") or {}
        cache_read = payload.get("cache_read")
        if cache_read is None:
            cache_read = details.get("cache_read") or 0
        found.append(
            {
                "line": _line_of(text, open_brace),
                "input": int(payload["input_tokens"]),
                "cache_read": int(cache_read or 0),
            }
        )
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", type=Path, help="run log file (utf-8)")
    parser.add_argument("--min-input", type=int, default=MIN_INPUT,
                        help="collapse candidate: input tokens at or above this")
    parser.add_argument("--max-hit", type=float, default=MAX_HIT,
                        help="collapse candidate: hit rate at or below this")
    args = parser.parse_args()

    text = args.log.read_text(encoding="utf-8", errors="replace")
    samples = extract_usage(text)
    if not samples:
        print("no usage_metadata found in this log")
        return 1

    print(f"{'line':>8}  {'input':>9}  {'cache_read':>10}  {'hit':>6}  note")
    collapsed = 0
    previous_input = 0
    for sample in samples:
        hit = sample["cache_read"] / sample["input"] if sample["input"] else 0.0
        notes = []
        if sample["input"] >= args.min_input and hit <= args.max_hit:
            collapsed += 1
            notes.append("COLLAPSE")
            if previous_input and sample["input"] < previous_input:
                notes.append(f"input shrank {previous_input} -> {sample['input']}"
                             " (compaction rewrote the history)")
        print(f"{sample['line']:>8}  {sample['input']:>9}  {sample['cache_read']:>10}  "
              f"{hit:>5.1%}  {'; '.join(notes)}")
        previous_input = sample["input"]

    total_input = sum(sample["input"] for sample in samples)
    total_cache = sum(sample["cache_read"] for sample in samples)
    full_price = total_input - total_cache
    print()
    print(f"samples={len(samples)} collapses={collapsed}")
    print(f"input={total_input} cached={total_cache} full-price={full_price}")
    if collapsed:
        print("after the summary_message fix, no call should sit at the 4096 floor:")
        print("rerun the same task and compare with this table.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
