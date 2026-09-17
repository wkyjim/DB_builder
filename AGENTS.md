# DB Builder — Agent Guide

## Project Summary

DB_builder is a local-first financial data pipeline and investment intelligence platform.

- **Ingests** U.S. equities, macro data, news, ETF flows, and short-positioning data
- **Stores** in local PostgreSQL (source of truth)
- **Syncs** selected data to Neon (cloud subset for API/deployment)
- **Publishes** reports via FastAPI API, Telegram bot, and GitHub Pages dashboard

Key components:
- **Local PostgreSQL** — source of truth for all raw and derived data
- **Neon** — selected cloud subset consumed by API/Telegram/dashboard
- **API** (`neon-api/`) — FastAPI app, reads from Neon only
- **Telegram** (`market-intelligence-telegram-bot/`) — command interface + scheduled reports
- **Dashboard** (`market-dashboard/`) — GitHub Pages static site

## Documentation

| Document | Purpose |
|----------|---------|
| `ARCHITECTURE.md` | Full current architecture |
| `TECHNICAL_DEBT.md` | Prioritized improvement backlog |
| `docs/PROJECT_MAP.md` | Annotated directory tree |
| `docs/DATA_FLOW.md` | Sources, pipelines, table relationships |
| `docs/DEPLOYMENT.md` | Infrastructure and deployment |

## Agent Quick Start

```bash
cd /workspace/DB_builder
git status
git log --oneline -10

# Run all tests
python -m pytest

# Run specific module tests
python -m pytest tests/test_indicators.py
python -m pytest tests/test_eastmoney.py
python -m pytest tests/test_etf_flow_analytics.py

# Run nested repo tests
python -m pytest neon-api/tests/test_api.py
python -m pytest market-intelligence-telegram-bot/tests/test_main.py

# Dry-run commands (safe, no writes)
python scripts/rule_based_market_update.py --dry-run --window-hours 24
python scripts/etf_flow_analytics.py --dry-run --start-date 2026-01-01
python scripts/pgSQL_equities_auto.py --dry-run --tickers AAPL
```

## Critical Safety Rules

- **Never** access or modify `DB_builder_env/` (external secret directory)
- **Never** print, log, or expose secrets in any output
- **Never** run destructive SQL (DROP, TRUNCATE, DELETE, ALTER DROP) without explicit human approval
- **Never** push, deploy, or change remote infrastructure without explicit approval
- **Keep** `DB_BUILDER_AUDIT_MODE=1` during audits (never unset it)

## Production-Sensitive Areas

Exercise extreme caution when modifying:

- `scripts/auto_*.bat` — scheduled production workflows
- `deploy/oracle/` — Oracle Cloud deployment configuration
- `neon-api/` — deployed FastAPI API (separate git repo)
- `market-intelligence-telegram-bot/` — deployed bot (separate git repo)
- `migrations/` — database schema changes
- `src/db_builder/neon_sync.py` — local-to-Neon sync logic
- `src/db_builder/rule_based_regime.py` — market regime scoring

## Architecture Invariants

- **Local PostgreSQL is source of truth** — Neon is a deployment subset
- **Eastmoney primary, yfinance fallback** for equity data
- **Rule-based reports are deterministic** — do not introduce LLM scoring unless explicitly requested
- **FINRA short volume ≠ short interest** — distinct datasets, distinct tables
- **Economic indicators are local-only** — not synced to Neon unless explicitly changed
- **Nested repos have independent deployment lifecycles** — do not merge without analysis
- **`*.md` and `*.sql` are gitignored** — use `git add -f` to track docs and migrations

## Where To Look

| Task | Location |
|------|----------|
| Equity ingestion | `scripts/pgSQL_equities_auto.py`, `src/db_builder/eastmoney.py` |
| Macro data | `scripts/macro_data_fetch.py`, `src/db_builder/config.py` |
| Technical indicators | `src/db_builder/indicators.py` |
| ETF flow analytics | `src/db_builder/etf_flow/`, `scripts/etf_flow_analytics.py` |
| Short analytics | `src/db_builder/finra_short_*.py`, `src/db_builder/short_pipeline.py` |
| Market regime | `src/db_builder/rule_based_regime.py` |
| Report generation | `src/db_builder/report_renderer.py`, `scripts/rule_based_market_update.py` |
| API endpoints | `neon-api/main.py` |
| Telegram commands | `market-intelligence-telegram-bot/main.py` |
| Deployment | `deploy/oracle/`, `neon-api/Dockerfile` |
| Tests | `tests/`, `neon-api/tests/`, `market-intelligence-telegram-bot/tests/` |

## Before Making Changes

1. Inspect affected tests and relevant architecture docs before changing code
2. Use `git add -f` to track documentation (`*.md`) and migration (`*.sql`) files

## Autonomous Maintenance Loop

For every maintenance task, follow this loop:

```
REFRESH
→ DEFINE SCOPE
→ DEFINE ACCEPTANCE CRITERIA
→ INSPECT EXISTING BEHAVIOR
→ IMPLEMENT
→ TEST
→ ADVERSARIAL SELF-REVIEW
→ SELF-FIX
→ RETEST
→ RUNTIME/PLATFORM VALIDATION
→ FINAL DIFF + GIT HYGIENE
→ CONSOLIDATED REPORT
→ PROTECTED APPROVAL GATE
```

### REFRESH
- `git status --short`, `git log --oneline -3`, `git diff --stat`
- Verify HEAD matches the assumed parent before any commit-scope statement
- Never describe already-pushed commits as part of a new commit

### DEFINE SCOPE
- Explicit list of files allowed to modify
- Explicit list of files prohibited from modification
- Written acceptance criteria (observable behavior, test commands, exit-code contract, integration points)

### DEFINE ACCEPTANCE CRITERIA
- Observable behavior changes
- Test commands to verify
- Exit-code contract (if applicable)
- Integration points (if applicable)

### INSPECT EXISTING BEHAVIOR
- Read callers, tests, schema, config
- Trace actual code paths, not just intended ones
- Compare comment claims to actual implementation

### IMPLEMENT
- Smallest safe change within approved scope
- No speculative refactoring
- No dead code

### TEST
- Run focused tests: `python -m pytest tests/test_<module>.py -v`
- Capture pass/fail count
- Any failure within scope → fix and retest

### ADVERSARIAL SELF-REVIEW
Actively look for:
- Correctness bugs
- Mismatch between documented and implemented behavior
- Edge cases
- Windows / PowerShell / batch compatibility
- Calendar and timezone errors
- Incorrect exit-code or failure semantics
- Misleading logging
- Concurrency and cleanup problems
- Database safety and query efficiency
- Security issues
- Error-handling gaps
- CLI parsing problems
- Hidden coupling between workflows
- Unnecessary complexity or dead code
- Missing tests
- Unintended changes outside scope

### SELF-FIX
If the self-review finds issues that can be fixed within the already-approved scope:
- Fix them yourself
- Add or update regression tests
- Rerun the tests
- Perform another self-review
- Repeat until no material correctness issue remains

**Do not return to the user for routine defects found during self-review if:**
- The correction remains inside the already-approved task scope
- It is non-destructive
- It does not require new architecture/product decisions
- It does not cross a protected action gate

**Escalate to the user only when:**
- A required fix would materially expand the approved scope
- There are two materially different architecture choices requiring a product decision
- Requirements are genuinely ambiguous and cannot be resolved from repository evidence
- A destructive or difficult-to-reverse action is required
- Production deployment/configuration would be changed beyond the approved scope
- Secrets or credentials are involved
- A database schema/data migration is required
- A protected action gate is reached

### RETEST
- Re-run focused suite after every fix
- Any failure → fix and retest again

### RUNTIME/PLATFORM VALIDATION
- `.bat` syntax, PowerShell semantics, Windows path handling
- Control-flow order (SUCCESS/failure logging, cleanup)
- Exit-code propagation to alerting mechanisms

### FINAL DIFF + GIT HYGIENE
Before requesting commit approval, verify:
- `git status --short` shows no leftover task-related working-tree changes
- `git diff --name-only` lists only intended files
- `git diff -w --stat` shows substantive-only change counts
- `git diff -w -- <file>` where necessary to verify specific changes
- CRLF/LF-only changes are excluded from substantive diff
- Tests have been actually executed (not just assumed passing)
- Test count matches expected
- Final control flow matches documented behavior
- Exit-code paths traced against contract
- Commit scope matches Git state (HEAD is correct parent)
- Parent commits are not described as part of the new commit
- Untracked review artifacts are excluded

### CONSOLIDATED REPORT
Return one consolidated report, not every intermediate iteration:
- Task objective
- Final implementation
- Material issues discovered during self-review
- Fixes applied
- Tests and results
- Remaining known limitations
- Substantive Git diff summary
- Any unresolved decision requiring the user
- The next protected approval gate

### PROTECTED APPROVAL GATE
Commit / push / deploy / config each require separate explicit approval.
Approval does not cascade: code approval ≠ commit approval ≠ push approval ≠ deploy approval.

## Standing Rules for Maintenance

### SR-25: Self-Review Is Part of Implementation

Self-review is not a post-implementation check. Before declaring any implementation complete:
1. Trace every exit path against the documented contract
2. Compare every comment claim to the actual code
3. Identify and remove dead code before first user review
4. Verify new files that connect to production systems are classified as production code

### SR-26: Refresh Git State Before Every Commit-Scope Statement

Before any statement about what a commit will include or exclude:
1. Run `git log --oneline -3` and `git status --short`
2. Verify HEAD matches the assumed parent
3. Include the HEAD hash in the commit-scope report
4. Never describe already-pushed commits as part of a new commit

### SR-27: Task-Related Tests Travel with the Production Change They Verify

When a commit includes production behavior changes, the tests that verify those changes belong in the same commit. Separate test-only commits are for:
- Test corrections that do not correspond to a production change in the same session
- Adding coverage to existing untested behavior

### SR-28: Explicit Control-Flow Verification for Pipeline Changes

When `.bat`, `.ps1`, or scheduled-workflow files change:
1. Show the user the control flow in order
2. Identify the exact line where SUCCESS/failure is logged
3. Confirm mutex/cleanup is in a `finally` block or explicitly called on every exit path
4. Confirm the exit code reaches the existing alerting mechanism

### SR-29: Explicit Calendar/Timezone Semantic Verification

When code involves date arithmetic, market calendars, or timestamps:
1. State whether the computation uses calendar days, business days, or NYSE sessions
2. Verify the implementation matches the documented semantics
3. **`now()` returns `timestamptz`**. If its value is stored into a `timestamp without time zone` column, PostgreSQL converts it using the current database/session `TimeZone` and discards timezone information. Do not later assume such a column represents UTC unless the session/storage convention has been verified.
4. Never claim "trading days" when the code computes `date1 - date2`

### SR-30: Final Diff/Git Hygiene Review Before Commit Approval

Before requesting commit approval:
1. Run `git status --short` to see all working-tree changes
2. Run `git diff --name-only` to list changed files
3. Run `git diff -w --stat` to get substantive-only change counts
4. Run `git diff -w -- <file>` where necessary to verify specific changes
5. Verify no task-related changes remain in the working tree
6. Use all four commands together to distinguish substantive changes from whitespace/CRLF noise

### SR-31: Reports Must Be Generated From Current Evidence

Before producing any status, commit, push, deployment, or maintenance report:
1. Refresh the facts the report depends on
2. Verify current Git/filesystem/runtime evidence where relevant
3. Do not reuse prior-state claims without re-verification
4. Current evidence overrides prior reports, summaries, and memory
5. Explicitly correct earlier statements when current evidence conflicts with them

## Self-Review Trigger Rules

Automatically run another review/fix cycle when:
- Test count changes unexpectedly
- Implementation and summary disagree
- New CLI arguments are introduced
- `.bat` / PowerShell control flow changes
- Exit-code behavior changes
- Market-calendar/date logic changes
- Timezone-aware timestamps are involved
- Mutex / finally / cleanup paths change
- Git shows leftover task-related changes after commit
- A report misclassifies new production code
- Comment claims behavior that code does not implement
- `FRESHNESS_RULES` list changes
- `--datasets` argument changes in `.bat` files
- User explicitly requests control-flow verification
- SUCCESS/failure logging order could mask actual state

## What NOT to Do

- Do not redesign the repository structure
- Do not rename `db_builder` or any existing modules
- Do not merge nested repositories
- Do not remove files merely because they look deprecated
- Do not make speculative refactors or "improvements"
- Do not change `.gitignore` without understanding its current behavior
- **Do not create, patch, edit, or delete any Hermes skill, SOUL, memory file, profile configuration, or agent instruction unless explicitly authorized**
- **Self-improvement review is READ-ONLY: it may identify improvements and recommend changes, but may not apply them without explicit approval**
