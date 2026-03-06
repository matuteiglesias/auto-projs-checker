# Inbox PDF drain runbook

## What exists in this repo today

There is no existing runtime path that consumes PDF bills/statements from an inbox.
Current automation is for the frontier/compile/publish pipeline (`make live-cycle`) and does not read `data/inbox`.

## New unattended path (timer-based)

This change adds a minimal timer-based drain model:

- `scripts/inbox_drain.py`: scans `data/inbox` for `*.pdf`, claims each file by atomic rename into `data/archive/inflight/`, runs a processor command, and moves files to:
  - `data/processed/` on success
  - `data/failed/` on failure
- `data/.inbox_drain.lock` lock file prevents overlapping runs.
- `systemd/inbox-pdf-drain.service` + `systemd/inbox-pdf-drain.timer` run every 5 minutes.
- `scripts/inbox_drain_entrypoint.sh` loads env and calls `make drain-inbox`.

## Required config

Create `private/inbox_runtime.env` (not committed):

```ini
# Required: command that processes one PDF path argument
PDF_PROCESSOR_CMD=/usr/local/bin/your_pdf_processor

# Optional overrides for paths
INBOX_DIR=data/inbox
PROCESSED_DIR=data/processed
FAILED_DIR=data/failed
ARCHIVE_DIR=data/archive
INBOX_LOCK=data/.inbox_drain.lock
```

`PDF_PROCESSOR_CMD` must accept the PDF path as the final argument and return:

- `0` for success
- non-zero for failure

## Install under user systemd

```bash
mkdir -p ~/.config/systemd/user
cp systemd/inbox-pdf-drain.service ~/.config/systemd/user/
cp systemd/inbox-pdf-drain.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now inbox-pdf-drain.timer
```

## Operations

Manual run:

```bash
scripts/inbox_drain_entrypoint.sh
```

Inspect logs:

```bash
journalctl --user -u inbox-pdf-drain.service -n 200 --no-pager
```

Check queue state:

```bash
find data/inbox data/archive/inflight data/processed data/failed -maxdepth 1 -type f | sort
```

## Assumptions and gaps

- Assumes Linux local filesystem semantics where rename within same filesystem is atomic.
- Assumes PDFs are dropped directly into `data/inbox` (no recursive scanning).
- Assumes the actual bill/statement parsing logic is external and supplied via `PDF_PROCESSOR_CMD`.
- Does not implement retries/backoff metadata; failed files are parked in `data/failed` for operator review.
