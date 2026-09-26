"""Package the ARC-Bench submission ZIP with Unix entry attributes.

The platform's extractor mis-handles MS-DOS style entries. .NET's
`ZipFile.CreateFromDirectory` writes `create_system=0` with `external_attr=0`,
and a mode of 0 has no `S_IFREG` bit at all - an extractor that classifies
entries by Unix mode cannot tell those files from anything else, and the
submission ended up with `ModuleNotFoundError: No module named 'app_type_handler'`.

The official template zip uses `create_system=3` (Unix) and `external_attr` =
`0o100664 << 16`, i.e. a real regular-file mode. This writer matches that exactly.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

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
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log"}
SKIP_NAMES = {".env"}

# Match the official template archive exactly.
CREATE_SYSTEM_UNIX = 3
FILE_MODE = 0o100664          # regular file, rw-rw-r--
DIR_MODE = 0o40775            # directory, rwxrwxr-x
FILE_ATTR = FILE_MODE << 16
DIR_ATTR = DIR_MODE << 16

ROOT_DIR = Path(__file__).resolve().parent


def should_skip_dir(name: str) -> bool:
    return name in SKIP_DIRS


def should_skip_file(name: str) -> bool:
    if name in SKIP_NAMES:
        return True
    return Path(name).suffix in SKIP_SUFFIXES


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(ROOT_DIR / "my_agent"))
    parser.add_argument("--zip", default=str(ROOT_DIR / "my_agent-arcbench-submission.zip"))
    parser.add_argument("--with-dir-entries", action="store_true",
                        help="also emit explicit directory entries (some extractors need them)")
    parser.add_argument("--diagnostic", action="store_true",
                        help="swap in a main.py that only reports the extracted layout")
    args = parser.parse_args()

    source = Path(args.source).resolve()
    target = Path(args.zip)
    if not (source / "main.py").is_file():
        raise SystemExit(f"main.py missing under {source}")
    if not (source / "requirements.txt").is_file():
        raise SystemExit(f"requirements.txt missing under {source}")
    if target.exists():
        target.unlink()

    diagnostic_bytes: bytes | None = None
    if args.diagnostic:
        diagnostic_bytes = ROOT_DIR.joinpath("_diag_payload.py").read_bytes()

    files = 0
    dirs = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            parts = relative.parts

            if any(part in SKIP_DIRS for part in parts):
                continue
            if path.is_dir():
                if args.with_dir_entries and relative.parts:
                    add_entry(archive, path, relative.as_posix() + "/", DIR_ATTR)
                    dirs += 1
                continue
            if should_skip_file(path.name):
                continue

            if diagnostic_bytes is not None and relative.as_posix() == "main.py":
                info = zipfile.ZipInfo("main.py", date_time=(2026, 9, 25, 0, 0, 0))
                info.create_system = CREATE_SYSTEM_UNIX
                info.external_attr = FILE_ATTR
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, diagnostic_bytes)
                files += 1
                continue

            add_entry(archive, path, relative.as_posix(), FILE_ATTR)
            files += 1

    with zipfile.ZipFile(target) as archive:
        infos = archive.infolist()
        systems = {i.create_system for i in infos}
        attrs = {i.external_attr for i in infos}
        stored = sum(1 for i in infos if i.compress_type == 0)
        names = archive.namelist()
        bad = archive.testzip()

    print(f"zip        : {target}")
    print(f"size       : {target.stat().st_size / 1024 / 1024:.2f} MB")
    print(f"entries    : {len(infos)}  (files={files}, dirs={dirs})")
    print(f"integrity  : {'OK' if bad is None else 'CORRUPT ' + str(bad)}")
    print(f"create_system set : {systems}")
    print(f"external_attr set : {[hex(a) for a in attrs]}")
    print(f"stored entries    : {stored}")
    print(f"main.py at root   : {'main.py' in names}")
    print(f"requirements.txt  : {'requirements.txt' in names}")
    print(f"backslash names   : {sum(1 for n in names if chr(92) in n)}")

    ok = (
        systems == {3}
        and FILE_ATTR in attrs
        and bad is None
        and "main.py" in names
        and "requirements.txt" in names
    )
    print("")
    print("result: " + ("OK - matches the official archive format" if ok else "PROBLEM"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
