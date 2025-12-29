#!/usr/bin/env python3
# runner_sequential.py
# Minimal sequential runner that reads your Google Sheet and runs all plugins
# Writes plugin-specific columns if missing and appends per-plugin history.

import os, sys, time, argparse, importlib, pkgutil, traceback
from datetime import datetime, timezone
from typing import List, Dict, Any

import gspread
from google.oauth2.service_account import Credentials

# -------------------------
# Defaults you provided
# -------------------------
DEFAULT_SHEET_ID = "1mImijqIwcbBqcO05xKzPWMITo-53ypjd1BEGicTp3jE"
DEFAULT_SA = "/home/matias/Documents/auto-projs-checker/private/service_account_file.json"

PROJECTS_SHEET = "PROJECTS"
HISTORY_SHEET = "HISTORY"
WORKER_ID = f"runner-seq-{os.getpid()}-{int(time.time())}"
DEFAULTS = {"plugins": ""}  # empty -> run all discovered plugins
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# -------------------------
# utils
# -------------------------
def now_iso():
    return datetime.now(timezone.utc).isoformat()




def worst_status(statuses):
    order = {"ERROR":4, "FAIL":3, "NA":2, "PASS":1}
    if not statuses:
        return "NA"
    return max(statuses, key=lambda s: order.get(s,2))




# -------------------------
# process_single_row_returning_writes
# -------------------------
def process_single_row_returning_writes(header: list, original_row: list, sheet_row_index: int,
                                       plugins_registry: dict, dry_run: bool = True) -> dict:
    """
    Process one project row WITHOUT making any Sheets API calls.
    Returns a dict:
      {
        "project_id": str,
        "agg_status": "PASS|FAIL|NA|ERROR",
        "updates": { colname: value, ... },   # only values to write
        "history_rows": [ [...], [...], ... ] # rows to append to history
      }
    Requirements:
      - header and original_row come from the initial get_all_values() (so we preserve other columns).
    """
    # build current row dict for comparisons
    row_map = row_to_dict(header, original_row)
    project_id = row_map.get("project_id") or f"r{sheet_row_index}"
    enabled = str(row_map.get("enabled", "")).strip().upper() in ("TRUE", "1", "Y", "YES")
    if not enabled:
        return {"project_id": project_id, "agg_status": "SKIPPED", "updates": {}, "history_rows": []}

    # decide plugin list for this project
    sheet_plugins = (row_map.get("plugins") or "").strip()
    if sheet_plugins:
        plugin_names = [p.strip() for p in sheet_plugins.split(",") if p.strip()]
    else:
        plugin_names = list(plugins_registry.keys())

    results = []
    history_rows = []
    for pname in plugin_names:
        plugin = plugins_registry.get(pname)
        if not plugin:
            # plugin not installed -> NA
            now = now_iso()
            rdict = {"name": pname, "status": "NA", "timestamp": now, "evidence": [], "message": f"plugin {pname} not installed", "duration_s": 0.0}
            results.append(rdict)
            # add history row
            history_rows.append([project_id, pname, rdict["status"], rdict["message"], ",".join(rdict["evidence"])[:1000], now, WORKER_ID, f"{rdict['duration_s']:.2f}"])
            continue

        try:
            res = plugin.run({"project_id": project_id, "repo_path": row_map.get("repo_path", "")}, {"dry_run": dry_run, "timeouts": {"shell": 120}})
            # normalize
            if hasattr(res, "__dict__"):
                r = {k: v for k, v in res.__dict__.items()}
            elif isinstance(res, dict):
                r = res.copy()
            else:
                r = dict(res)
            # ensure keys exist
            r.setdefault("name", pname)
            r.setdefault("status", "ERROR")
            r.setdefault("evidence", [])
            r.setdefault("message", "")
            r.setdefault("duration_s", 0.0)
        except Exception as e:
            now = now_iso()
            r = {"name": pname, "status": "ERROR", "timestamp": now, "evidence": [], "message": str(e)[:500], "duration_s": 0.0}
        # add to results and history rows
        results.append(r)
        hist = [
            project_id,
            r.get("name"),
            r.get("status"),
            (r.get("message") or "")[:800],
            ",".join(r.get("evidence", []))[:1000],
            r.get("timestamp") or now_iso(),
            WORKER_ID,
            f"{float(r.get('duration_s',0.0)):.2f}"
        ]
        history_rows.append(hist)

    # aggregate status
    statuses = [r.get("status", "ERROR") for r in results]
    agg = worst_status(statuses)

    # prepare updates dict (only changed cells)
    updates = {}
    # per-plugin columns
    for r in results:
        pname = r.get("name")
        s_col = f"{pname}_status"
        m_col = f"{pname}_message"
        e_col = f"{pname}_evidence"
        # values to write
        s_val = r.get("status", "")
        m_val = (r.get("message") or "")[:800]
        e_val = ",".join(r.get("evidence", []))[:1000]
        # write only if different from original
        if row_map.get(s_col, "") != s_val:
            updates[s_col] = s_val
        if row_map.get(m_col, "") != m_val:
            updates[m_col] = m_val
        if row_map.get(e_col, "") != e_val:
            updates[e_col] = e_val

    # common aggregate columns
    status_details = "; ".join([f"{r.get('name')}:{(r.get('message') or '')}" for r in results])[:1000]
    evidence_concat = []
    for r in results:
        ev = r.get("evidence", [])
        if isinstance(ev, str):
            evidence_concat.append(ev)
        else:
            evidence_concat.extend(ev)
    evidence_str = ",".join(evidence_concat)[:1500]

    if row_map.get("status", "") != agg:
        updates["status"] = agg
    if row_map.get("status_details", "") != status_details:
        updates["status_details"] = status_details
    if row_map.get("evidence_link", "") != evidence_str:
        updates["evidence_link"] = evidence_str
    updates["last_update_ts"] = now_iso()
    updates["last_update_by"] = WORKER_ID

    return {"project_id": project_id, "agg_status": agg, "updates": updates, "history_rows": history_rows}

