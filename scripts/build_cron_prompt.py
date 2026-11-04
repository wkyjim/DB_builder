"""Assemble the market-intelligence cron prompt from the authorized doctrine file.

Single source of truth: ``market-intelligence-report-prompt-authorized.txt`` in the
DB_builder root (git-tracked). This script splices its doctrine sections into the
cron job prompt stored in the Hermes profile's ``cron/jobs.json`` so the model no
longer has to read a 2,011-line file mid-run (it never did).

What gets merged:
  * header / workspace / facts-vs-judgments / shortcut guard   (current job prompt)
  * external-research mandate + evidence-density bar            (current job prompt)
  * NEWS SIGNAL DATABASE block + TOOL ENFORCEMENT block         (defined here)
  * authorized sections 1-36 (objective, sources, API, freshness,
    24-section content spec, writing style)
  * execution layer: mandatory workflow + narrative JSON schema (current job prompt)
  * quality gates 54, 56, 65, 67, 68                            (authorized file)
  * failure policy + model pin                                  (current job prompt)

What gets dropped and replaced by one note: sections 37-53, 55, 57-64 and 66.
Those describe hand-writing HTML/CSS, manual git staging and manual pushes. The
deterministic renderer (``scripts/generate_final_report.py``) owns the HTML and
publishes with ``--push``; leaving those instructions in place makes the model
bypass the builder or run conflicting git commands.

Idempotent: writes only when the assembled text differs. Keeps a backup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

AUTH_DEFAULT = Path(r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\market-intelligence-report-prompt-authorized.txt")
JOBS_DEFAULT = Path(r"C:\Users\User\AppData\Local\hermes\profiles\pm\cron\jobs.json")
BASE_DEFAULT = Path(r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\scripts\cron_prompt_base.txt")
JOB_ID = "00dc3141a6cc"

# Sections that describe hand-written HTML, manual git staging or manual pushes.
# The renderer and --push own those steps; keeping them invites conflicting work.
REPLACED = {37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53,
            55, 57, 58, 59, 60, 61, 62, 63, 64, 66, 68}

# Contradictions left inside otherwise-kept sections. Applied verbatim to the
# assembled text after splicing.
POST_REPLACEMENTS = [
    (
        "Primary analytical source:\nhttps://wkyjim.github.io/market-dashboard/data/latest-report.md",
        "Primary analytical source:\nmarket-dashboard/data/latest-report.md "
        "(local file written hourly by hourly_full_pipeline.ps1; the published "
        "GitHub Pages copy is a mirror and may lag, so it is NOT the baseline)",
    ),
    # The doctrine still speaks in CIO terms. The report no longer carries a CIO
    # layer: the headline is the AI regime score and the comparison is Source vs
    # AI. Reframe the kept doctrine text without editing the authorized file.
    ("11. EXECUTIVE SUMMARY — CENTRAL CIO VIEW", "11. EXECUTIVE SUMMARY — AI VIEW"),
    ("Begin with a concise CIO View of approximately 120–180 words.",
     "Begin with a concise AI View of approximately 120–180 words."),
    ("12. CIO DASHBOARD", "12. AI REGIME SCORECARD"),
    ("- Overall Regime Score",
     "- AI Regime Score (one number; the source rule-based value is disclosed in one small line beneath)"),
    ("This section should function as the one-minute CIO summary.",
     "This section should function as the one-minute AI summary."),
    ("CIO Dashboard\nCIO View", "AI Regime Scorecard\nAI View"),
    (
        "16. PYTHON VS CIO ATTRIBUTION (REVALIDATE cross-check) - The quantitative rules-based engine "
        "(sector / factor / theme scores, Source Regime Score) and the CIO qualitative stance are DIFFERENT "
        "methodologies and must never be conflated. When they disagree materially, explicitly disclose: "
        "(a) the inputs each derives from, (b) the horizon each targets, (c) the risk-based override or "
        "qualitative filter justifying the divergence, and (d) the expected reconciliation window. Never "
        "present a score difference as a mathematical adjustment when the underlying methodologies are "
        "incompatible. If no reconciliation is possible, disclose the residual disagreement.",
        "16. SOURCE VS AI ATTRIBUTION (REVALIDATE cross-check) - The quantitative rules-based engine "
        "(sector / factor / theme scores, Source Regime Score) and the AI two-step re-rating are DIFFERENT "
        "methodologies and must never be conflated. The report headline carries the AI regime score as one "
        "number, computed as sum(AI factor score x AI weight)/100, with the source rule-based score disclosed "
        "in one small line beneath it. When they disagree materially, disclose in adjustments.rationale: "
        "(a) the inputs each derives from, (b) which factor scores and weights the AI moved and by how much, "
        "(c) the research evidence justifying each material divergence, and (d) the expected reconciliation "
        "window. Never present the source score as a CIO layer, and never reintroduce a CIO weighted scorecard "
        "or a separate AI re-rating table.",
    ),
    ("Executive Summary / CIO scorecard", "Executive Summary / AI regime scorecard"),
]

RENDER_NOTE = """RENDERING AND PUBLICATION — THE BUILDER OWNS THE HTML
You do NOT hand-write HTML, CSS, a masthead, a table of contents, a theme
switcher, print styles or mobile styles. scripts/generate_final_report.py renders
every one of those from your narrative JSON. Never write .html by hand, never
inline <style> or <script>, never edit data/market-intelligence-report.html.
Publication is a single command (step e below) that validates, renders, commits
and pushes. Do not run manual git add / commit / push, and never force push."""

NEWS_BLOCK = """NEWS SIGNAL DATABASE — QUERY IT EVERY RUN (PRIMARY EVIDENCE, NOT DECORATION)
A local PostgreSQL database already ingests and classifies market news and
refreshes it every three hours. Query it BEFORE writing prose. It is the primary
evidence layer for news drivers, for the news confirmation score, and for the
"why did this move" question. Treat it as primary evidence, not as a summary of
somebody else's reporting.

Access (from the container):
  host: host.docker.internal   port: 5432   db: us_equities_historical
  credentials: /run/secrets/db_builder.env  (LOCAL_DB_USER, LOCAL_DB_PASSWORD,
  LOCAL_DB_NAME, LOCAL_DB_PORT)
  read-only. Never INSERT, UPDATE, DELETE or ALTER anything.

Tables:
  news_articles  (~80k rows)  article_id, source_name, source_type, title,
    summary, url, canonical_url, published_at, fetched_at, related_tickers,
    source_priority, source_category.
    DATA CAVEAT: roughly 75% of rows have published_at IS NULL. Always filter
    "WHERE published_at IS NOT NULL" when you need a publication date, and say
    "undated" in prose rather than implying a date you do not have.
  news_signals   (~1M rows)   run_time, window_hours (6/24/72), dimension_type
    ('theme' or 'ticker'), dimension_value, article_count, high_impact_count,
    avg_sentiment_score, weighted_sentiment_score, avg_impact_score,
    opportunity_score, risk_score, top_article_ids.

Reference queries:
  -- latest classified signals, 24h window. Each window (6/24/72) writes its own
  -- run_time, so filter the window BEFORE taking the max timestamp.
  WITH latest AS (
    SELECT max(run_time) AS t FROM news_signals WHERE window_hours = 24
  )
  SELECT dimension_type, dimension_value, article_count,
         weighted_sentiment_score, opportunity_score, risk_score
  FROM news_signals, latest
  WHERE window_hours = 24 AND run_time = latest.t
  ORDER BY opportunity_score DESC NULLS LAST, risk_score DESC NULLS LAST
  LIMIT 20;

  -- most recent dated headlines
  SELECT published_at, source_name, title, url
  FROM news_articles
  WHERE published_at IS NOT NULL
    AND published_at > now() - interval '72 hours'
  ORDER BY published_at DESC
  LIMIT 20;

Cite this layer as "local news database (news_signals, 24h window, run_time <ts>)"
so the reader knows the claim is model-classified news, not a primary document.
Keep the classification labels: a high opportunity_score is a signal, not a fact.
"""

TOOL_BLOCK = """TOOL ENFORCEMENT — A RUN WITH NO WEB CALLS IS A FAILED RUN
Toolsets enabled for this job: browser, web, memory, file, terminal, code-execution.

- Call web_search and/or web_extract at least 6 times per run.
- Cover at least 4 different institutions from the priority list in section 3.
- Open the PRIMARY document for every material claim. An aggregator or a search
  snippet is not a primary document. Record what you opened and what it said.
- The narrative "sources" array MUST carry at least 10 distinct http(s) URLs from
  at least 4 distinct registered domains. report_narrative.validate_narrative
  enforces both floors and HALTs the run when either is unmet.
- If a URL cannot be opened, cite the document by name and date, mark the access
  failure in that source's note, and never invent a replacement URL.
- Zero web_search / web_extract calls = failed run, even if the JSON validates.
  Report the failure; do not publish a report that only restates latest-report.md.

The news database above does NOT replace external research. It supplements it:
the database tells you what the tape is talking about, the primary documents tell
you whether the tape is right.
"""

GEN_NOTE = """This prompt is GENERATED. Doctrine sections are copied from
market-intelligence-report-prompt-authorized.txt (sha256 {sha}). Rebuild with
scripts/build_cron_prompt.py after editing that file; do not hand-edit this prompt."""


def parse_sections(text: str) -> dict[int, tuple[str, str]]:
    """Return {number: (title, body)} for monotonic numbered headings."""
    lines = text.split("\n")
    heads: list[tuple[int, str, int]] = []
    expected = 1
    for i, raw in enumerate(lines):
        line = raw.strip()
        m = re.match(r"^(\d{1,2})\.\s+(\S.{2,110})$", line)
        if not m:
            continue
        num = int(m.group(1))
        title = m.group(2).strip()
        if num < expected or num > expected + 2:
            continue
        # Headings are upper-case titles; skip numbered list items inside a section.
        letters = [c for c in title if c.isalpha()]
        if letters and sum(1 for c in letters if c.islower()) > len(letters) * 0.35:
            continue
        heads.append((num, title, i))
        expected = num + 1
    out: dict[int, tuple[str, str]] = {}
    for idx, (num, title, start) in enumerate(heads):
        end = heads[idx + 1][2] if idx + 1 < len(heads) else len(lines)
        body = "\n".join(lines[start + 1:end]).strip()
        out[num] = (title, body)
    return out


EXEC_START = "EXECUTION LAYER (workflow and narrative schema)"
EXEC_ALT = "MANDATORY WORKFLOW (GENERATE"
EXEC_END = "RENDERING AND PUBLICATION — THE BUILDER OWNS THE HTML"


def _first_index(text: str, needles: list[str], default: int) -> int:
    hits = [text.find(n) for n in needles if text.find(n) >= 0]
    return min(hits) if hits else default


def split_current(prompt: str) -> dict[str, str]:
    """Split the prompt into base blocks using markers unique to each block.

    Anchors must stay unique in BOTH the pre-merge and post-merge prompt: the
    merged prompt already contains the doctrine and gates, so a naive
    "next marker" split would re-ingest them and the prompt would grow on every
    run. Each end marker below appears exactly once in either shape.
    """
    n = len(prompt)
    h = _first_index(prompt, ["EXTERNAL RESEARCH IS MANDATORY"], n)
    r = _first_index(prompt, ["EVIDENCE DENSITY AND SPECIFICITY"], n)
    d = _first_index(prompt, ["NEWS SIGNAL DATABASE", "AUTHORIZED DOCTRINE",
                              "MANDATORY WORKFLOW (GENERATE"], n)
    e = _first_index(prompt, [EXEC_START], n)
    if e >= n:
        e = _first_index(prompt, [EXEC_ALT], n)
    f = _first_index(prompt, ["FAILURE POLICY"], n)
    g = _first_index(prompt, [EXEC_END], n)

    # Execution block runs from the workflow header to the render note; when the
    # merged shape is present the "EXECUTION LAYER" banner precedes it.
    exec_start = e
    if e < n and EXEC_START in prompt and prompt.find(EXEC_START) == e:
        # skip the banner line itself
        exec_start = prompt.find("\n", e) + 1 or e
    exec_end = g if g < n and g > exec_start else f

    return {
        "header": prompt[:h].strip(),
        "research": prompt[h:r].strip() if h < r else "",
        "density": prompt[r:d].strip() if r < d else "",
        "exec": prompt[exec_start:exec_end].strip() if exec_start < exec_end else "",
        "failure": prompt[f:].strip() if f < n else "",
    }


def build(auth_path: Path, jobs_path: Path, base_path: Path, dry_run: bool) -> int:
    if not auth_path.exists():
        print("HALT: authorized prompt not found: %s" % auth_path)
        return 2
    if not jobs_path.exists():
        print("HALT: jobs.json not found: %s" % jobs_path)
        return 2
    if not base_path.exists():
        print("HALT: base template not found: %s" % base_path)
        return 2

    auth_text = auth_path.read_text(encoding="utf-8")
    # Always assemble from the versioned base template, never from the live
    # prompt: re-reading the target makes the merge non-idempotent and it grows
    # on every run (the merged text re-ingests its own doctrine).
    base_text = base_path.read_text(encoding="utf-8")
    sha = hashlib.sha256(auth_text.encode("utf-8")).hexdigest()

    data = json.loads(jobs_path.read_text(encoding="utf-8"))
    job = next((j for j in (data.get("jobs") or []) if j.get("id") == JOB_ID), None)
    if job is None:
        print("HALT: job %s not found" % JOB_ID)
        return 3
    old_prompt = job.get("prompt") or ""

    sections = parse_sections(auth_text)
    if not sections:
        print("HALT: no numbered sections parsed")
        return 4

    kept = {n: v for n, v in sections.items() if n not in REPLACED}
    dropped = sorted(n for n in sections if n in REPLACED)
    missing = [n for n in REPLACED if n not in sections]

    # Doctrine = sections 1-36, in order.
    doctrine_parts = []
    for n in sorted(kept):
        if n > 36:
            continue
        title, body = kept[n]
        doctrine_parts.append("%d. %s\n%s" % (n, title, body))
    doctrine = "\n\n".join(doctrine_parts).strip()

    # Gates kept after the execution layer.
    gate_parts = []
    for n in (54, 56, 65, 67, 68):
        if n in kept:
            title, body = kept[n]
            gate_parts.append("%d. %s\n%s" % (n, title, body))
    gates = "\n\n".join(gate_parts).strip()

    cur = split_current(base_text)
    header = cur["header"]
    # Point at the generated prompt instead of telling the model to read the file.
    header = re.sub(
        r"^- Authoritative procedure:.*$",
        "- " + GEN_NOTE.format(sha=sha[:16]).replace("\n", " "),
        header,
        count=1,
        flags=re.M,
    )
    research = cur["research"]
    # The 3-URL sentence is superseded by the tool-enforcement block.
    research = re.sub(
        r"^The narrative JSON MUST include a \"sources\" array.*$\n?",
        "",
        research,
        count=1,
        flags=re.M,
    ).strip()
    density = cur["density"]
    execu = cur["exec"]
    failure = cur["failure"]

    assembled = "\n\n".join(
        [
            header,
            research,
            density,
            NEWS_BLOCK.strip(),
            TOOL_BLOCK.strip(),
            "AUTHORIZED DOCTRINE (sections 1-36)",
            doctrine,
            "EXECUTION LAYER (workflow and narrative schema)",
            execu,
            RENDER_NOTE.strip(),
            "QUALITY GATES",
            gates,
            failure,
        ]
    ).strip() + "\n"

    for old, new in POST_REPLACEMENTS:
        if old in assembled:
            assembled = assembled.replace(old, new, 1)
        else:
            print("WARNING post-replacement anchor not found: %r" % old[:60])

    print("authorized sha256 :", sha[:16])
    print("sections parsed   : %d (%d..%d)" % (len(sections), min(sections), max(sections)))
    print("sections dropped  : %d -> %s" % (len(dropped), dropped))
    if missing:
        print("WARNING dropped numbers not found:", missing)
    print("doctrine chars    : %d" % len(doctrine))
    print("gates chars       : %d" % len(gates))
    print("assembled chars   : %d" % len(assembled))
    print("old prompt chars  : %d" % len(old_prompt))
    print("delta             : %+d" % (len(assembled) - len(old_prompt)))

    if dry_run:
        print("DRY-RUN: no write")
        out = Path("cron_prompt_preview.txt")
        out.write_text(assembled, encoding="utf-8")
        print("preview written to", out.resolve())
        return 0

    if assembled == old_prompt:
        print("NO CHANGE: prompt already up to date")
        return 0

    shutil.copy(jobs_path, str(jobs_path) + ".bak_generated")
    job["prompt"] = assembled
    jobs_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # Verify by reading back.
    check = json.loads(jobs_path.read_text(encoding="utf-8"))
    saved = next(j for j in check["jobs"] if j["id"] == JOB_ID)["prompt"]
    ok = saved == assembled
    print("WROTE %s -> %d chars; read-back match: %s" % (jobs_path, len(saved), ok))
    for marker in ("NEWS SIGNAL DATABASE", "TOOL ENFORCEMENT", "AUTHORIZED DOCTRINE",
                   "EXECUTION LAYER", "QUALITY GATES", "RENDERING AND PUBLICATION"):
        print("  has %-28s %s" % (marker, marker in saved))
    return 0 if ok else 5


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--auth", default=str(AUTH_DEFAULT))
    ap.add_argument("--jobs", default=str(JOBS_DEFAULT))
    ap.add_argument("--base", default=str(BASE_DEFAULT))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return build(Path(args.auth), Path(args.jobs), Path(args.base), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
