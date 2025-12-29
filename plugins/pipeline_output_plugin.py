# plugins/pipeline_output_plugin.py
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Tuple, DefaultDict
from collections import defaultdict

from .base import BasePlugin, result
from ._utils import (
    FileHit,
    walk_files_deterministic,
    bytes_human,
    age_human,
    snippet_sha256_of_file,
    best_effort_du_lines,
    split_list_field,
    DEFAULT_OUTPUT_DIR_HINTS,
)

class PipelineOutputPlugin(BasePlugin):
    """Observability-style pipeline output probe.

    Goal:
      - Provide bounded, deterministic evidence that the project produced artifacts recently.
      - No declared exact output path required; we search under a bounded root with exclusions and caps.

    Expected ctx:
      ctx["project"]["repo_path"] (or "workdir") as root.
      Optional:
        - ctx["project"]["output_dirs_hint"] (comma/semicolon separated or list)
        - ctx["project"]["output_extensions"] (list or string)
        - ctx["project"]["freshness_hours"] or freshness_days
        - ctx["timeouts"]["search_s"]
        - ctx["config"]["pipeline_output"] overrides (optional)
    """

    name = "pipeline_output"
    version = "1.0.0"

    DEFAULT_EXTS = [
        ".parquet", ".csv", ".jsonl", ".json", ".feather", ".xlsx", ".pdf", ".txt", ".md"
    ]

    DEFAULT_MAX_DEPTH = 6
    DEFAULT_MAX_MATCHES = 5000
    DEFAULT_MAX_FILES_SEEN = 200000
    DEFAULT_TIMEOUT_S = 10.0
    DEFAULT_FRESHNESS_HOURS = 72.0  # 3 days
    REPORT_TOP_N = 10

    def run(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        t0 = time.time()
        project = ctx.get("project") or ctx  # tolerate direct project ctx
        repo_root = project.get("repo_path") or project.get("workdir") or project.get("path")
        repo_root = str(repo_root).strip()

        # resolve knobs with reasonable precedence
        cfg = (ctx.get("config") or {}).get("pipeline_output") or {}
        timeouts = ctx.get("timeouts") or {}

        timeout_s = float(cfg.get("timeout_s") or timeouts.get("search_s") or self.DEFAULT_TIMEOUT_S)
        max_depth = int(cfg.get("max_depth") or project.get("output_max_depth") or self.DEFAULT_MAX_DEPTH)
        max_matches = int(cfg.get("max_matches") or project.get("output_max_matches") or self.DEFAULT_MAX_MATCHES)
        max_files_seen = int(cfg.get("max_files_seen") or self.DEFAULT_MAX_FILES_SEEN)

        freshness_hours = None
        if project.get("freshness_hours") is not None:
            try:
                freshness_hours = float(project.get("freshness_hours"))
            except Exception:
                freshness_hours = None
        if freshness_hours is None and project.get("freshness_days") is not None:
            try:
                freshness_hours = float(project.get("freshness_days")) * 24.0
            except Exception:
                freshness_hours = None
        if freshness_hours is None:
            try:
                freshness_hours = float(cfg.get("freshness_hours") or self.DEFAULT_FRESHNESS_HOURS)
            except Exception:
                freshness_hours = self.DEFAULT_FRESHNESS_HOURS

        exts = split_list_field(project.get("output_extensions") or cfg.get("extensions") or self.DEFAULT_EXTS)
        # normalize extensions: ensure leading dot, lowercase
        norm_exts = []
        for e in exts:
            e = e.strip().lower()
            if not e:
                continue
            if not e.startswith("."):
                e = "." + e
            norm_exts.append(e)
        if not norm_exts:
            norm_exts = list(self.DEFAULT_EXTS)

        # candidate search roots: hints first, then repo_root
        hints = split_list_field(project.get("output_dirs_hint") or cfg.get("output_dirs_hint"))
        if not hints:
            hints = list(DEFAULT_OUTPUT_DIR_HINTS)

        candidate_roots: List[str] = []
        repo_root_abs = os.path.abspath(repo_root)

        for h in hints:
            p = os.path.join(repo_root_abs, h)
            if os.path.isdir(p):
                candidate_roots.append(p)

        # Always include repo_root as fallback (but scan after hints)
        if repo_root_abs not in candidate_roots:
            candidate_roots.append(repo_root_abs)

        deadline = time.time() + timeout_s
        now = time.time()

        matches: List[FileHit] = []
        counts_by_ext: DefaultDict[str,int] = defaultdict(int)
        recent_24h = 0
        cap_hit = False
        timed_out = False

        # Scan roots in deterministic order (sorted by path length then path)
        candidate_roots.sort(key=lambda p: (len(os.path.relpath(p, repo_root_abs)), p))

        files_seen_total = 0
        for root in candidate_roots:
            if time.time() > deadline:
                timed_out = True
                break

            # Hinted roots should be scanned shallower to reduce noise
            rel_from_repo = os.path.relpath(root, repo_root_abs)
            root_depth = 4 if rel_from_repo != "." else max_depth

            for abspath, rel_from_root in walk_files_deterministic(
                root,
                max_depth=root_depth,
                deadline_epoch=deadline,
                max_files_seen=max_files_seen,
            ):
                files_seen_total += 1
                if time.time() > deadline:
                    timed_out = True
                    break

                _, ext = os.path.splitext(rel_from_root)
                ext = ext.lower()
                if ext not in norm_exts:
                    continue

                try:
                    st = os.stat(abspath)
                except Exception:
                    continue

                rel_from_repo2 = os.path.relpath(abspath, repo_root_abs)
                hit = FileHit(
                    relpath=rel_from_repo2,
                    abspath=abspath,
                    size_bytes=int(st.st_size),
                    mtime_epoch=float(st.st_mtime),
                )
                matches.append(hit)
                counts_by_ext[ext] += 1
                if now - hit.mtime_epoch <= 86400:
                    recent_24h += 1

                if len(matches) >= max_matches:
                    cap_hit = True
                    break

            if cap_hit or timed_out:
                break

        # Determine top hits by newest mtime then stable relpath
        matches.sort(key=lambda h: (-h.mtime_epoch, h.relpath))
        top_hits = matches[: self.REPORT_TOP_N]

        newest_age_s = None
        if top_hits:
            newest_age_s = max(0.0, now - top_hits[0].mtime_epoch)

        # Optional du lines for common dirs that exist
        du_targets = []
        for d in ["outputs", "output", "artifacts", "reports", "data", "results"]:
            p = os.path.join(repo_root_abs, d)
            if os.path.isdir(p):
                du_targets.append(p)
        du_lines = best_effort_du_lines(du_targets, timeout_s=min(2.0, timeout_s / 2.0))

        evidence: List[str] = []
        # include top hits in evidence
        for h in top_hits:
            sha = snippet_sha256_of_file(h.abspath, max_read=4000) or ""
            sha_s = sha[:10] if sha else ""
            evidence.append(f"hit:{h.relpath} age={age_human(now-h.mtime_epoch)} size={bytes_human(h.size_bytes)} sha10={sha_s}")

        # compose status + bucket
        if not os.path.isdir(repo_root_abs):
            return result(
                status="NA",
                bucket="MISSING_METADATA:workdir",
                message="repo_path/workdir missing or not a directory",
                evidence=[],
                meta={"repo_root": repo_root},
            )

        if not matches:
            status = "FAIL"
            bucket = "NO_MATCHES:extensions"
            msg = f"No matches for {norm_exts} under {os.path.basename(repo_root_abs) or repo_root_abs}"
        else:
            # freshness decision
            assert newest_age_s is not None
            newest_h = newest_age_s / 3600.0
            if newest_h <= freshness_hours:
                status = "PASS"
                bucket = "FOUND_RECENT:ok"
                msg = f"Found {len(matches)} artifacts; newest {age_human(newest_age_s)} ago: {top_hits[0].relpath}"
            else:
                status = "WARN"
                bucket = "FOUND_STALE:older_than_threshold"
                msg = f"Newest artifact is {age_human(newest_age_s)} old (threshold {freshness_hours:.1f}h): {top_hits[0].relpath}"

        # override bucket when cap/timeout hit (keep main decision in meta)
        if timed_out:
            status = "WARN" if matches else "ERROR"
            bucket = "TIMEOUT:search"
            msg = (msg + "; timed out while scanning").strip()
        elif cap_hit:
            bucket = "TOO_MANY_MATCHES:cap_hit"
            msg = (msg + "; cap hit").strip()

        meta: Dict[str, Any] = {
            "repo_root": repo_root_abs,
            "candidate_roots": candidate_roots,
            "extensions": norm_exts,
            "freshness_hours": freshness_hours,
            "max_depth": max_depth,
            "timeout_s": timeout_s,
            "max_matches": max_matches,
            "files_seen_total": files_seen_total,
            "match_count": len(matches),
            "recent_24h": recent_24h,
            "counts_by_ext": dict(sorted(counts_by_ext.items())),
            "top_hits": [
                {
                    "relpath": h.relpath,
                    "age_seconds": float(now - h.mtime_epoch),
                    "size_bytes": int(h.size_bytes),
                    "mtime_epoch": float(h.mtime_epoch),
                }
                for h in top_hits
            ],
            "cap_hit": cap_hit,
            "timed_out": timed_out,
            "du_lines": du_lines,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

        # add du lines as compact evidence (limited)
        for line in du_lines[:5]:
            evidence.append(f"du:{line}")

        return result(status=status, bucket=bucket, message=msg, evidence=evidence, meta=meta)
