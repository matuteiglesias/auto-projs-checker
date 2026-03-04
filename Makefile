# Auto Projects Checker
# Purpose: stable, low-friction interface for frontier + compiler + publish.
# Conventions:
# - make smoke: offline, bounded, no secrets
# - make run: full live cycle (frontier -> compile -> publish), requires SHEET_ID + SA

PROJECT := $(notdir $(CURDIR))
DATE ?= $(shell date +%F)

# Paths
FIXTURE_FRONTIER ?= fixtures/frontier_sample.csv
LIVE_FRONTIER    ?= out/frontier/latest.csv
OUT_COMPILER_DIR ?= out/compiler/$(DATE)
OUT_JSONL        ?= $(OUT_COMPILER_DIR)/prepared_blocks.jsonl

PY ?= python3
PY_ENV = PYTHONNOUSERSITE=1 $(PY)

.PHONY: help smoke run run_all live-cycle frontier compile-queue publish-queue check-env dirs clean-queue

help:
	@echo "Project: $(PROJECT)"
	@echo ""
	@echo "Core:"
	@echo "  make smoke         Offline check: compile queue from fixture CSV"
	@echo "  make run           Alias for live-cycle"
	@echo ""
	@echo "Live (requires env):"
	@echo "  make live-cycle    frontier -> compile-queue -> publish-queue"
	@echo "  make frontier      update frontier + write out/frontier/latest.csv"
	@echo "  make compile-queue compile from out/frontier/latest.csv"
	@echo "  make publish-queue publish today's prepared_blocks.jsonl to Sheets"
	@echo ""
	@echo "Utilities:"
	@echo "  make clean-queue   remove today's compiler output dir"
	@echo ""
	@echo "Env vars:"
	@echo "  SHEET_ID=<google sheet id>   SA=<path to service_account_file.json>"
	@echo "  DATE=YYYY-MM-DD (optional override)"
	@echo ""

# Offline bounded check
smoke:
	@$(PY_ENV) scripts/compile_blocks.py --frontier "$(FIXTURE_FRONTIER)" --date "$(DATE)" >/dev/null
	@echo "[SMOKE] compiler fixture compile ok (DATE=$(DATE))"

# Full live cycle is the "run" for this orchestrator repo
run: live-cycle
run_all: live-cycle




check-env:
	@test -n "$(SHEET_ID)" || (echo "missing SHEET_ID"; exit 2)
	@test -n "$(SA)" || (echo "missing SA"; exit 2)
	@test -f "$(SA)" || (echo "SA file not found: $(SA)"; exit 2)

dirs:
	@mkdir -p "$(OUT_COMPILER_DIR)"

medidas-alert: check-env
	@$(PY_ENV) scripts/medidas_alert.py --sheet-id "$(SHEET_ID)" --sa "$(SA)"


# Stage 1: frontier update (this should also write out/frontier/latest.csv)
frontier: check-env
	@$(PY_ENV) scripts/run_frontier.py --sheet-id "$(SHEET_ID)" --sa "$(SA)"

# Stage 2: compile blocks from deterministic latest frontier file
compile-queue: dirs
	@test -f "$(LIVE_FRONTIER)" || (echo "missing frontier input: $(LIVE_FRONTIER)"; exit 2)
	@$(PY_ENV) scripts/compile_blocks.py --frontier "$(LIVE_FRONTIER)" --date "$(DATE)"

# Stage 3: publish queue to Google Sheet tab(s)
publish-queue: check-env
	@test -f "$(OUT_JSONL)" || (echo "missing compiled queue: $(OUT_JSONL)"; exit 2)
	@$(PY_ENV) scripts/publish_block_queue.py --sheet-id "$(SHEET_ID)" --sa "$(SA)" --jsonl "$(OUT_JSONL)"

# One command: frontier -> compile -> publish
live-cycle: frontier compile-queue publish-queue medidas-alert
	@echo "[LIVE] ok: frontier -> compile -> publish (DATE=$(DATE))"

clean-queue:
	@rm -rf "$(OUT_COMPILER_DIR)"
	@echo "[CLEAN] removed $(OUT_COMPILER_DIR)"


# TODO


# D) One command “health probe”

# Add Makefile target:

# make health that prints:

# last run id

# whether latest.csv exists

# whether today prepared_blocks.jsonl exists

# whether BlockQueue tab exists (optional, only if cheap)

# If any check fails, exit nonzero.

# Then you can use:

# make health || journalctl --user -u auto-projs-checker.service -n 120 --no-pager
