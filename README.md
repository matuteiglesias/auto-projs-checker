# auto-projs-checker

Project health checker for local repos, driven by a Google Sheet policy.

## Quickstart
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/service_account.json
python runner.py --sheet-id <ID> --sa "$GOOGLE_SERVICE_ACCOUNT_FILE" --no-write
```

## Inbox PDF drain (bills/statements)

This repository now includes a minimal unattended inbox drain for PDF bills/statements:

- Entrypoint: `scripts/inbox_drain_entrypoint.sh`
- Worker: `scripts/inbox_drain.py`
- systemd units: `systemd/inbox-pdf-drain.service` + `systemd/inbox-pdf-drain.timer`
- Make target: `make drain-inbox`

See `notes/inbox_drain_runbook.md` for setup and operations.

