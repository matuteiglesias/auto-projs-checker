# Diseño (lock-in) — Hardening runner & datos canonical

Voy a dejarte un **documento de diseño y requisitos** que podés pegar en las notas del proyecto. Es el “contrato” que dejarás listo hoy para retomar mañana: define fuentes, modelo de merge de las dos sheets, estructura mínima, plugins esenciales, modos de ejecución, outputs y reglas operativas. Está pensado para no sobre-ingenierizar y ser utilísimo al primer run.

---

## 1) Visión rápida

Objetivo: tener un runner que recorra todos los proyectos (todos-para-todos), aplique checks ligeros/seguros sobre código, pipeline y datos, y entregue un `hardening_report` + `batches` para remediación. Hoy cerramos el **diseño**: inputs, esquema de merge entre `projects` y `repos`, checks mínimos, outputs esperables, y reglas operacionales.

---

## 2) Fuentes y merge (two sheets → canonical table)

Tienes:

* Sheet A: `projects_sheet` (filas por proyecto, metadata humana)
* Sheet B: `repos_sheet` (filas por repo, rutas, URLs, branch, owner técnico)

Reglas de mingling / merge:

1. Identificador común: `project_id` (slug). Si no existe, crear: `project_id = normalize(title or repo_name)` (lowercase, `-` for spaces).
2. Join key: `projects_sheet.project_id` ⟵ if missing, try to match `repos_sheet.project_name` or `repo_url` by basename; fallback: manual fill required.
3. Canonical row fields = union + precedence:

   * If field present in `projects_sheet`, keep (human overrides).
   * Else if present in `repos_sheet`, take it.
4. Fields in canonical table (minimal):

   * `project_id, title, path_or_repo, repo_type (local|git_url), repo_branch, owner_contact, priority_manual, applies_pipeline, applies_endpoint, applies_privacy, benefit_score, effort_est_hours, data_ground_truth_path, notes`
5. If a repo maps to multiple projects, create separate canonical rows that reference same repo path but different `project_id`.
6. Output of merge: `canonical_projects.csv` (one row per project).

Nota práctica: guarda una copia `canonical_projects.raw.csv` antes de editar.

---

## 3) Estructura mínima de un proyecto (convención para checks)

Colocar en `path_or_repo` (local path or mounted workspace) si posible:

```
project/
  runbook.md            # mínimo: short instructions
  reproduce.sh          # must accept --dry-run (or echo placeholder)
  results/              # outputs, timestamped
  logs/                 # logs
  data/ground_truth/    # canonical data + _meta/
  backups/              # optional
  .github/workflows/    # CI config (optional)
  OWNERS or CODEOWNERS  # owner list
```

Si algo no existe, plugin devuelve `NA` o `FAIL` según `applies_*`.

---

## 4) Checks esenciales (versión final para implementación)

Cada check = plugin. Los 13 esenciales (prioridad práctica):

1. **runbook** — busca runbook/readme/reproduce.sh; PASS if file edited <90d (configurable). Evidence: file + mtime.
2. **commit_recent** — `git log -1 --pretty=%cI` days <=30 OR runbook.Note POSTPONED => PASS. Meta: `days_since`.
3. **pipeline_output** — `find results -newermt "7 days ago"` => PASS.
4. **smoke_tests** — if reproduce.sh present: run `bash reproduce.sh --dry-run` with timeout. PASS=exit0. Collect log.
5. **endpoint** — grep docs for URL; if found curl with timeout; PASS if 200. NA if none.
6. **cron_runs** — heurística: presence of crontab/systemd timer notes or scheduler logs; PASS if last run <7d.
7. **repro_env** — presence of lockfile (`poetry.lock`, `requirements.txt` with pinned versions, `environment.yml`, `Dockerfile`). PASS if any.
8. **secrets** — grep patterns (no values logged). FAIL → immediate urgent tag.
9. **privacy** — detect raw microdata, known PII column names in CSVs; FAIL → urgent.
10. **backups** — `backups/` with recent file and checksum meta. PASS if backup <31d.
11. **ci_status** — presence of workflow that runs reproduce.sh + last run success within 30d → PASS.
12. **access_owners** — OWNER file or owner_contact in canonical table. FAIL if none.
13. **acceptance_metric** — runbook contains `Acceptance:` or `Metric:` line. PASS if found.

Cada plugin retorna `CheckResult` (see next section).

---

## 5) Contrato de plugin — `CheckResult` (schema)

Cada plugin debe devolver un JSON con keys:

```json
{
  "name":"runbook",
  "status":"PASS|FAIL|NA|ERROR",
  "timestamp":"2025-12-22T10:01:00Z",
  "evidence":["path/to/file","http_status:200"],
  "message":"short human message",
  "meta": {"days_since_commit": 5},
  "duration_s": 0.42
}
```

Reglas: evidence no debe contener secrets; `NA` = no aplica; `ERROR` = transitorio/permisos.

---

## 6) Scoring & thresholds

Pesos base (configurable):

```
pesos = {
  runbook:2, commit_recent:1, pipeline_output:3, smoke_tests:3,
  endpoint:2, cron_runs:1, repro_env:2, secrets:4,
  privacy:4, backups:3, ci_status:2, access_owners:3, acceptance_metric:1
}
```

Score calculation:

* Exclude NA
* `S = sum(w_i * (1 if PASS else 0))`
* `W = sum(w_i for applicable checks)`
* `score_pct = round(100*S/W)`
  Buckets:
* > =85 OK; 60..84 ATTENTION; <60 HIGH RISK

Critical: any `secrets == FAIL` or `privacy == FAIL` always escalates (urgent) regardless of score.

---

## 7) Runs, modes y políticas

* **light_run** (weekly): run all checks but with smoke_tests run in `--dry-run` and short timeouts. Fast, parallel, safe.
* **daily_critical_run** (optional): only secrets & privacy on public projects.
* **deep_run** (monthly): run smoke full (subject to resource limits), pip-audit/trivy, restore test for backups. Run serially per project or with low parallelism.

Timeouts/retries:

* HTTP: 8s, retries 2 with backoff (2s, 6s)
* smoke dry-run: 30s; deep-run: 600s
* Transient errors → retry up to 3 → if persists mark `ERROR` and create manual probe item.

Concurrency:

* `max_workers` default 6 for plugin threads; adjust by CPU/IO.

Dry-run safety:

* Default `dry_run=True` — no destructive actions; remediation never auto-applied.

---

## 8) Outputs & artefactos

Per run (timestamped):

* `artifacts/<project_id>/<ts>/checks.json` (all CheckResults)
* `artifacts/<project_id>/<ts>/console.log` (plugin logs)
* `hardening_report_<ts>.json` (all projects)
* `hardening_report_<ts>.md` (human table summary)
* `batches_<ts>.json` (groups of projects by failing aspect, with impact/effort)
* For critical FAILs: `urgent_tickets/<ts>/*` (templates ready for manual creation)

Retention: keep artifacts 30 days by default (configurable).

---

## 9) Batching y priorización (operativa)

Batch grouping:

* Group by aspect (e.g., `runbook_missing`) → list of projects with FAIL.
* Compute group `impact = sum(benefit_score*confidence)` (confidence default 0.7 if missing).
* Compute `effort = sum(effort_est_hours)` (use small=1, medium=4, large=8 if generic).
* Efficiency = impact/effort → sort desc → schedule first.

Batch session standard:

* Duration: 90 min (15m prep, 60m apply, 15m push+recheck)
* Goal: move projects from FAIL → minimally acceptable PASS (document next steps if needed)

---

## 10) Next action mapping (auto suggestions)

Map FAIL → suggested remediation + estimate:

* `runbook FAIL` → create `runbook_minimal.md` (est 0.5–1h)
* `pipeline_output FAIL` → run pipeline dry-run, gather logs (1–3h)
* `smoke_tests FAIL` → collect reproduce log, fix deps (2–8h)
* `endpoint FAIL` → check DNS/certs/redeploy (1–4h)
* `secrets FAIL` → *urgent*: stop, notify owner, rotate creds (manual)
* `privacy FAIL` → *urgent*: remove public data, anonymize, legal check (manual)
* `backups FAIL` → create backup job & test restore (2–4h)
* `access_owners FAIL` → assign owner (0.25h)

Each suggestion includes `template_path` (e.g., `templates/runbook_minimal.md`) to standardize batch fixes.

---

## 11) Seguridad & privacidad operacional

* Runner runs with least-privilege; avoid root.
* Tokens: read-only where possible; use ephemeral tokens for git APIs.
* Evidence sanitization: never record secret values; store only path+hash snippet.
* PII: never upload previews to public endpoints. Artifacts stored locally or in secured bucket.
* Critical FAILs require manual sign-off before any remediation.

---

## 12) Checklists para preparar antes del primer run (quick list)

1. Produce `canonical_projects.csv` via merge (save raw).
2. Ensure `path_or_repo` are valid local paths (or accessible URLs).
3. Create placeholders in 3–5 priority projects:

   * empty `runbook.md`, `reproduce.sh` (echo dry-run), `results/`, `logs/`
4. Mark `applies_privacy=TRUE` for projects using microdata.
5. Create `artifacts_root` path and ensure runner can write.
6. Choose run mode for first time: `light_run`.
7. Backup your sheets (CSV export) before letting runner write back.

---

## 13) Artefactos de diseño para tus notas (copy-paste)

* `canonical_projects.csv` header (copy):

```
project_id,title,path_or_repo,repo_type,repo_branch,owner_contact,priority_manual,applies_pipeline,applies_endpoint,applies_privacy,benefit_score,effort_est_hours,data_ground_truth_path,notes
```

* `templates/runbook_minimal.md` (one-liner you can paste tomorrow):

```
# Runbook minimal - {project_id}
Owner: {owner_contact}
Date: {date}
Prereqs: (python>=3.10, pip install -r requirements.txt)
Reproduction (dry-run):
  ./reproduce.sh --dry-run
Expected output:
  results/summary.csv (rows > 0)
Acceptance:
  Metric: summary.csv row count > 0
Next steps: ...
```

* `templates/reproduce_placeholder.sh`:

```bash
#!/usr/bin/env bash
# placeholder dry-run
echo "DRY RUN: reproduce script placeholder for $0"
exit 0
```

---

## 14) Plan de retomada (qué hacer mañana)

1. Pegar `canonical_projects.csv` en las notas (o subir aquí) — yo te revisaré diseño/consistencias si querés.
2. Crear placeholders en 3 proyectos prioritarios (usar templates arriba).
3. Ejecutar mental checklist: paths accesibles, artifacts dir creado, applies_privacy correctamente marcado.
4. Ejecutar `light_run` por primera vez (esto lo hará tu builder cuando estés listo).
5. Review report + schedule first batch (runbook_missing) 90 min.

---

## 15) Qué dejo documentado aquí (resumen)

* Merge rules entre sheets y campo canónico.
* Lista de checks (13) y sus contratos.
* Scoring, buckets y reglas de escalado urgente.
* Modes (light/daily/deep), timeouts, retries.
* Outputs/artefactos, batch logic, templates y prep checklist.

---

Si querés, te dejo esto listo como archivo `design_hardening_lockin.md` que podés copiar a tus notas. También puedo inmediatamente generar (en texto) los tres templates (`runbook_minimal.md`, `reproduce_placeholder.sh`, `data_meta_template.json`) para que los copies; no los ejecuto ni los guardo, solo te los dejo listos para pegar cuando estés con fuerzas. ¿Querés que te entregue las tres plantillas ahora en texto para copiarlas a mano?

