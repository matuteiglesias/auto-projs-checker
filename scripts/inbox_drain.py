#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import os
import shlex
import subprocess
import sys
from pathlib import Path

PDF_EXTENSIONS = {".pdf"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Drain PDF inbox with claim + lock semantics")
    p.add_argument("--inbox", default="data/inbox")
    p.add_argument("--processed", default="data/processed")
    p.add_argument("--failed", default="data/failed")
    p.add_argument("--archive", default="data/archive")
    p.add_argument("--lock-file", default="data/.inbox_drain.lock")
    p.add_argument("--processor-cmd", default=os.environ.get("PDF_PROCESSOR_CMD", ""))
    p.add_argument("--max-files", type=int, default=0, help="0 means no limit")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def ensure_dirs(*dirs: Path) -> None:
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def now_utc() -> str:
    return dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def unique_target(dst_dir: Path, name: str) -> Path:
    candidate = dst_dir / name
    if not candidate.exists():
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    return dst_dir / f"{stem}.{now_utc()}{suffix}"


def find_pdfs(inbox: Path) -> list[Path]:
    files = [p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() in PDF_EXTENSIONS]
    files.sort(key=lambda p: p.name)
    return files


def run_processor(processor_cmd: str, pdf_path: Path, log_file: Path, dry_run: bool) -> int:
    if dry_run:
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"[DRY_RUN] {pdf_path}\n")
        return 0

    cmd = shlex.split(processor_cmd) + [str(pdf_path)]
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write("+ " + " ".join(cmd) + "\n")
        fh.flush()
        proc = subprocess.run(cmd, stdout=fh, stderr=fh)
        return proc.returncode


def main() -> int:
    args = parse_args()
    inbox = Path(args.inbox)
    processed = Path(args.processed)
    failed = Path(args.failed)
    archive = Path(args.archive)
    inflight = archive / "inflight"
    logs_dir = archive / "logs"
    lock_file = Path(args.lock_file)

    ensure_dirs(inbox, processed, failed, archive, inflight, logs_dir, lock_file.parent)

    if not args.processor_cmd and not args.dry_run:
        print("missing --processor-cmd / PDF_PROCESSOR_CMD", file=sys.stderr)
        return 2

    with lock_file.open("w", encoding="utf-8") as lock_fh:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("drain already running; exiting")
            return 0

        pdfs = find_pdfs(inbox)
        if args.max_files > 0:
            pdfs = pdfs[: args.max_files]

        if not pdfs:
            print("no files in inbox")
            return 0

        run_id = now_utc()
        ok = 0
        ko = 0

        for src in pdfs:
            claimed = unique_target(inflight, src.name)
            try:
                src.rename(claimed)
            except FileNotFoundError:
                continue

            file_log = logs_dir / f"{claimed.stem}.{run_id}.log"
            rc = run_processor(args.processor_cmd, claimed, file_log, args.dry_run)

            if rc == 0:
                dst = unique_target(processed, claimed.name)
                claimed.rename(dst)
                ok += 1
                print(f"OK {src.name} -> {dst}")
            else:
                dst = unique_target(failed, claimed.name)
                claimed.rename(dst)
                ko += 1
                print(f"FAIL {src.name} rc={rc} -> {dst}")

        print(f"done run_id={run_id} ok={ok} failed={ko}")
        return 0 if ko == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
