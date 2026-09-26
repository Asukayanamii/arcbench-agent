"""Rebuild the ARC-Bench submission ZIP.

Run this **instead of** `Compress-Archive` / right-click "Send to > Compressed folder".

Why it matters: the platform's extractor mis-reads MS-DOS style ZIP entries.
.NET's `ZipFile.CreateFromDirectory` (which is what `Compress-Archive` and the Windows
shell use) writes `create_system=0` with `external_attr=0`. Unix mode 0 has no `S_IFREG`
bit, so the extractor cannot classify those entries as regular files and the submission
came back as:

    ModuleNotFoundError: No module named 'app_type_handler'

The official template archive uses `create_system=3` (Unix) and `external_attr`
`0o100664 << 16`. This script writes exactly that, so the result matches the official
format byte-for-byte on the fields that matter.

Usage (from the repository root):
    python tools/build_submission_zip.py
    python tools/build_submission_zip.py --zip dist/my-agent-submission.zip
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    "__pycache__",
    "data",
    ".arc",
    ".my_agent",
    "node_modules",
    ".commandcode",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
    ".pytest_cache",
    "dist",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log"}
SKIP_NAMES = {".env"}

# Values taken from the official agent template archive.
CREATE_SYSTEM_UNIX = 3
FILE_MODE = 0o100664
DIR_MODE = 0o40775
FILE_ATTR = FILE_MODE << 16
DIR_ATTR = DIR_MODE << 16

REQUIRED_AT_ROOT = ("main.py", "requirements.txt", "arc_main.py")


def add_entry(archive: zipfile.ZipFile, path: Path, arcname: str, attr: int) -> None:
    info = zipfile.ZipInfo(arcname, date_time=(2026, 9, 25, 0, 0, 0))
    info.create_system = CREATE_SYSTEM_UNIX
    info.external_attr = attr
    info.compress_type = zipfile.ZIP_DEFLATED
    if path.is_dir():
        archive.writestr(info, b"")
        return
    info.file_size = path.stat().st_size
    archive.writestr(info, path.read_bytes())


def build(source: Path, target: Path, *, with_dir_entries: bool = False) -> int:
    source = source.resolve()
    for name in REQUIRED_AT_ROOT:
        if not (source / name).is_file():
            raise SystemExit(f"{name} is missing at the repository root: {source}")

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    files = 0
    dirs = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if any(part in SKIP_DIRS for part in relative.parts):
                continue
            if path.is_dir():
                if with_dir_entries and relative.parts:
                    add_entry(archive, path, relative.as_posix() + "/", DIR_ATTR)
                    dirs += 1
                continue
            if path.name in SKIP_NAMES or path.suffix in SKIP_SUFFIXES:
                continue
            add_entry(archive, path, relative.as_posix(), FILE_ATTR)
            files += 1

    with zipfile.ZipFile(target) as archive:
        infos = archive.infolist()
        names = archive.namelist()
        systems = {i.create_system for i in infos}
        attrs = {i.external_attr for i in infos}
        broken = archive.testzip()

    problems: list[str] = []
    if systems != {CREATE_SYSTEM_UNIX}:
        problems.append(f"create_system is {systems}, expected {{3}} (Unix)")
    if attrs != {FILE_ATTR}:
        problems.append(f"external_attr is {[hex(a) for a in attrs]}, expected {hex(FILE_ATTR)}")
    if broken is not None:
        problems.append(f"archive is corrupt at {broken}")
    for name in REQUIRED_AT_ROOT:
        if name not in names:
            problems.append(f"{name} is not at the ZIP root")
    if any("\\" in n for n in names):
        problems.append("entry names contain backslashes")
    if any(n.endswith((".pyc", ".pyo")) or "__pycache__" in n for n in names):
        problems.append("bytecode cache leaked into the archive")

    print(f"zip              : {target}")
    print(f"size             : {target.stat().st_size / 1024 / 1024:.2f} MB")
    print(f"entries          : {len(infos)}  (files={files}, dirs={dirs})")
    print(f"create_system    : {systems}")
    print(f"external_attr    : {[hex(a) for a in attrs]}")
    print(f"integrity        : {'OK' if broken is None else 'CORRUPT'}")
    print(f"root entrypoints : {[n for n in REQUIRED_AT_ROOT if n in names]}")

    if problems:
        print("")
        print("PROBLEMS:")
        for problem in problems:
            print("  - " + problem)
        return 1

    print("")
    print("result: OK - matches the official archive format")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default=str(REPO_ROOT), help="repository root to package")
    parser.add_argument(
        "--zip",
        default=str(REPO_ROOT.parent / "my_agent-arcbench-submission.zip"),
        help="output ZIP path",
    )
    parser.add_argument(
        "--with-dir-entries",
        action="store_true",
        help="also emit explicit directory entries (the official archive does not)",
    )
    args = parser.parse_args()
    return build(Path(args.source), Path(args.zip), with_dir_entries=args.with_dir_entries)


if __name__ == "__main__":
    sys.exit(main())
