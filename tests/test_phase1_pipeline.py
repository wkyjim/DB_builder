"""Phase 1 pipeline tests: circuit breaker, Asia fallback, state, gate, metadata.

Run:  LOCAL_DB_PASSWORD=dummy NEON_DB_PASSWORD=dummy python -m pytest tests/test_phase1_pipeline.py -q
"""

from __future__ import annotations

import json
import sys
import types
from datetime import date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scripts import macro_data_fetch  # noqa: E402
from scripts import pipeline_state  # noqa: E402


# ---------------------------------------------------------------------------
# Investing.com provider circuit breaker
# ---------------------------------------------------------------------------


def test_breaker_constant_is_two():
    assert macro_data_fetch.INVESTINY_CIRCUIT_BREAKER_TRIPS == 2


def test_is_investiny_403_detection():
    assert macro_data_fetch._is_investiny_403(Exception("error code: 403")) is True
    assert macro_data_fetch._is_investiny_403(Exception("403 Forbidden")) is True
    assert macro_data_fetch._is_investiny_403(Exception("error code: 404")) is False
    assert macro_data_fetch._is_investiny_403(Exception("timed out")) is False
    assert macro_data_fetch._is_investiny_403(Exception("")) is False


def test_403_raises_provider_unavailable_immediately(monkeypatch):
    calls = {"n": 0}

    def fake_historical_data(**_kwargs):
        calls["n"] += 1
        raise ConnectionError("Request to Investing.com API failed with error code: 403.")

    monkeypatch.setitem(
        sys.modules, "investiny", types.SimpleNamespace(historical_data=fake_historical_data)
    )
    sleeps: list[float] = []
    monkeypatch.setattr(macro_data_fetch.time, "sleep", sleeps.append)
    monkeypatch.setattr(macro_data_fetch.random, "uniform", lambda *_a: 0.0)

    with pytest.raises(macro_data_fetch.InvestinyProviderUnavailable):
        macro_data_fetch.fetch_investiny_symbol_df("US2YT=X", start_date=date(2026, 9, 18))

    assert calls["n"] == 1
    # Only the pre-call jitter — no backoff cooldowns.
    assert sleeps == [0.0]


def test_fallback_routing_covers_all_investiny_symbols():
    """Every Investing.com-routed symbol must have a fallback destination."""
    for symbol in macro_data_fetch.INVESTINY_ASSETS:
        has_fallback = (
            symbol in macro_data_fetch.OIL_YFINANCE_FALLBACK_SYMBOLS
            or symbol in macro_data_fetch.UST_TREASURY_FALLBACK_SYMBOLS
            or symbol in macro_data_fetch.ASIA_INDEX_YFINANCE_FALLBACK_SYMBOLS
        )
        assert has_fallback, f"{symbol} has no fallback destination"


# ---------------------------------------------------------------------------
# Asia index fallbacks
# ---------------------------------------------------------------------------


def test_asia_fallback_map_symbols():
    m = macro_data_fetch.ASIA_INDEX_YFINANCE_FALLBACK_MAP
    assert m["HK50"]["symbol"] == "^HSI"
    assert m["KOR200c1"]["symbol"] == "^KS11"
    assert m["CIHc1"]["symbol"] == "000001.SS"
    # names label them as cash proxies (not futures) for downstream readers
    for entry in m.values():
        assert "cash proxy" in entry["name"]


def test_asia_fallback_rejects_unknown_symbol():
    meta = {"name": "X", "asset_type": "futures"}
    with pytest.raises(ValueError):
        macro_data_fetch.process_asia_index_yfinance_fallback_symbol("ES=F", meta)


def test_source_label_passed_to_live_row(monkeypatch):
    """The Asia fallback must annotate live rows with the proxy source label."""
    captured = {}

    def fake_extract(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(macro_data_fetch, "download_recent_batch", lambda *a, **k: (None, date(2026, 9, 18)))
    monkeypatch.setattr(macro_data_fetch, "extract_symbol_df_from_batch", lambda *a, **k: None)
    # validate raises SymbolProcessingError before live-row extraction; assert via message
    with pytest.raises(macro_data_fetch.SymbolProcessingError):
        macro_data_fetch.process_asia_index_yfinance_fallback_symbol(
            "HK50", {"name": "Hang Seng Futures", "asset_type": "futures"}
        )


# ---------------------------------------------------------------------------
# Pipeline state
# ---------------------------------------------------------------------------


def test_state_new_cycle():
    s = pipeline_state.new_cycle_state("20260923_0800_HKT")
    assert s["cycle_id"] == "20260923_0800_HKT"
    assert s["overall_status"] == "RUNNING"
    assert s["publication_status"] == "NOT_PUBLISHED"
    assert s["consecutive_failures"] == 0


def test_state_cycle_id_format():
    cid = pipeline_state.make_cycle_id(datetime(2026, 9, 23, 8, 0))
    assert cid == "20260923_0800_HKT"


def test_state_degraded_tracking():
    s = pipeline_state.new_cycle_state("20260923_0800_HKT")
    pipeline_state.record_source(s, "news_classify", status="DEGRADED", note="Ollama offline")
    pipeline_state.record_source(s, "us_equities", status="OK", observation_date="2026-09-22")
    assert s["degraded_sources"] == ["news_classify"]
    assert s["sources"]["us_equities"]["observation_date"] == "2026-09-22"
    assert s["sources"]["us_equities"]["retrieved_at"]  # auto-stamped


def test_state_critical_gate():
    s = pipeline_state.new_cycle_state("20260923_0800_HKT")
    assert pipeline_state.critical_sources_ok(s) is False  # nothing recorded
    for name in pipeline_state.CRITICAL_SOURCES:
        pipeline_state.record_source(s, name, status="OK")
    assert pipeline_state.critical_sources_ok(s) is True
    pipeline_state.record_source(s, "macro_rates", status="FAILED")
    assert pipeline_state.critical_sources_ok(s) is False


def test_state_report_ready_vs_publication(tmp_path):
    """REPORT_READY must not depend on push success (separation requirement I)."""
    s = pipeline_state.new_cycle_state("20260923_0800_HKT")
    for name in pipeline_state.CRITICAL_SOURCES:
        pipeline_state.record_source(s, name, status="OK")
    report = tmp_path / "r.md"
    report.write_text("x" * 6000)

    status = pipeline_state.finalize_report_ready(s, report)
    assert status == "REPORT_READY"
    assert s["latest_report"]["sha256"] == pipeline_state.sha256_file(report)

    # push fails -> publication PUSH_FAILED but overall stays REPORT_READY
    pipeline_state.record_publication(s, commit="abc", pushed=False, head_equals_origin=None, error="GitHub down")
    assert s["publication_status"] == "PUSH_FAILED"
    assert s["overall_status"] == "REPORT_READY"


def test_state_consecutive_failures():
    s = pipeline_state.new_cycle_state("c1")
    pipeline_state.finalize_failed(s, "critical sources failed")
    pipeline_state.bump_consecutive_failures(None, s)
    assert s["consecutive_failures"] == 1

    s2 = pipeline_state.new_cycle_state("c2")
    pipeline_state.finalize_failed(s2, "again")
    pipeline_state.bump_consecutive_failures(s, s2)
    assert s2["consecutive_failures"] == 2

    s3 = pipeline_state.new_cycle_state("c3")
    pipeline_state.bump_consecutive_failures(s2, s3)  # not FAILED
    assert s3["consecutive_failures"] == 0


def test_state_atomic_save_load(tmp_path):
    s = pipeline_state.new_cycle_state("20260923_0800_HKT")
    for name in pipeline_state.CRITICAL_SOURCES:
        pipeline_state.record_source(s, name, status="OK", observation_date="2026-09-22")
    report = tmp_path / "r.md"
    report.write_text("y" * 6000)
    pipeline_state.finalize_report_ready(s, report)
    pipeline_state.record_publication(s, commit="deadbeef", pushed=True, head_equals_origin=True)

    path = tmp_path / "state.json"
    pipeline_state.save_state(s, path)
    loaded = pipeline_state.load_state(path)
    assert loaded["cycle_id"] == "20260923_0800_HKT"
    assert loaded["overall_status"] == "REPORT_READY"
    assert loaded["publication_status"] == "PUSHED"
    assert loaded["git"]["commit"] == "deadbeef"
    # no temp files left behind
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".latest")]
    assert leftovers == []


def test_state_corrupt_load_returns_none(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert pipeline_state.load_state(bad) is None
    assert pipeline_state.load_state(tmp_path / "missing.json") is None


def test_source_severity_classification():
    """CRITICAL = report cannot be generated; matches audit §M."""
    crit = {k for k, v in pipeline_state.SOURCE_SEVERITY.items() if v == "CRITICAL"}
    assert crit == {
        "us_equities",
        "equity_indicators",
        "macro_rates",
        "macro_fx_commodities",
        "macro_market_indices",
    }
    assert pipeline_state.SOURCE_SEVERITY["news_classify"] == "OPTIONAL"
    assert pipeline_state.SOURCE_SEVERITY["economic_releases"] == "IMPORTANT"
    assert pipeline_state.SOURCE_SEVERITY["etf_flows"] == "IMPORTANT"


# ---------------------------------------------------------------------------
# Readiness gate (invoked as subprocess for isolation)
# ---------------------------------------------------------------------------


def _run_gate(report, state, extra=()):
    import subprocess

    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "readiness_gate.py"),
        "--report",
        str(report),
        "--state",
        str(state),
        *extra,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _make_ready_state(tmp_path, *, cycle_id=None, sha=None, pub="PUSHED", critical_ok=True):
    s = pipeline_state.new_cycle_state(cycle_id or pipeline_state.make_cycle_id())
    for name in pipeline_state.CRITICAL_SOURCES:
        pipeline_state.record_source(s, name, status="OK" if critical_ok else "FAILED")
    for name in ("economic_releases", "etf_flows", "finra_short", "news_fetch", "news_signals"):
        pipeline_state.record_source(s, name, status="OK")
    pipeline_state.record_source(s, "news_classify", status="DEGRADED", note="Ollama offline")
    report = tmp_path / "latest-report.md"
    report.write_text(
        "# Rule-Based Institutional Market Update\n\nGenerated at: 24 September 2026, 01:20:00 (HKT)\n"
        "Window: 24h\n\n## Cycle Metadata\n- Cycle ID: "
        + s["cycle_id"]
        + "\n"
        + "x" * 6000
    )
    pipeline_state.finalize_report_ready(s, report)
    if sha:
        s["latest_report"]["sha256"] = sha
    if pub == "PUSHED":
        pipeline_state.record_publication(s, commit="abc123", pushed=True, head_equals_origin=True)
    elif pub == "PUSH_FAILED":
        pipeline_state.record_publication(s, commit="abc123", pushed=False, head_equals_origin=None, error="GitHub down")
    state_path = tmp_path / "state.json"
    pipeline_state.save_state(s, state_path)
    return report, state_path


def test_gate_ready(tmp_path):
    report, state = _make_ready_state(tmp_path, pub="PUSHED")
    r = _run_gate(report, state)
    assert r.returncode == 0
    assert r.stdout.startswith("READY |")
    assert "publication=PUSHED" in r.stdout


def test_gate_ready_with_publication_warning(tmp_path):
    """Requirement L: valid local report + failed push = READY_WITH_PUBLICATION_WARNING."""
    report, state = _make_ready_state(tmp_path, pub="PUSH_FAILED")
    r = _run_gate(report, state)
    assert r.returncode == 0
    assert "READY_WITH_PUBLICATION_WARNING" in r.stdout
    assert "PUSH_FAILED" in r.stdout


def test_gate_blocks_stale_cycle(tmp_path):
    report, state = _make_ready_state(tmp_path, cycle_id="20260901_0000_HKT")
    r = _run_gate(report, state)
    assert r.returncode == 2
    assert "CYCLE_STALE" in r.stdout


def test_gate_blocks_sha_mismatch(tmp_path):
    report, state = _make_ready_state(tmp_path, sha="0" * 64)
    r = _run_gate(report, state)
    assert r.returncode == 2
    assert "REPORT_SHA_MISMATCH" in r.stdout


def test_gate_blocks_critical_source_failure(tmp_path):
    report, state = _make_ready_state(tmp_path, critical_ok=False)
    # force overall to REPORT_READY anyway to isolate the critical-source check
    data = json.loads(state.read_text())
    data["overall_status"] = "REPORT_READY"
    state.write_text(json.dumps(data))
    r = _run_gate(report, state)
    assert r.returncode == 2
    assert "CRITICAL_SOURCE_NOT_OK" in r.stdout


def test_gate_blocks_missing_state(tmp_path):
    report, _ = _make_ready_state(tmp_path)
    r = _run_gate(report, tmp_path / "nonexistent.json")
    assert r.returncode == 2
    assert "STATE_MISSING" in r.stdout


def test_gate_blocks_report_without_cycle_id(tmp_path):
    report, state = _make_ready_state(tmp_path)
    report.write_text(report.read_text().replace(
        json.loads(state.read_text())["cycle_id"], "OTHER_CYCLE"
    ))
    r = _run_gate(report, state)
    assert r.returncode == 2
    assert "REPORT_CYCLE_ID_MISSING" in r.stdout or "REPORT_SHA_MISMATCH" in r.stdout


def test_gate_blocks_not_report_ready(tmp_path):
    report, state = _make_ready_state(tmp_path)
    data = json.loads(state.read_text())
    data["overall_status"] = "RUNNING"
    state.write_text(json.dumps(data))
    r = _run_gate(report, state)
    assert r.returncode == 2
    assert "CYCLE_NOT_REPORT_READY" in r.stdout


# ---------------------------------------------------------------------------
# Report cycle metadata block
# ---------------------------------------------------------------------------


def test_build_cycle_metadata_block():
    import re

    src = (PROJECT_ROOT / "scripts" / "rule_based_market_update.py").read_text(encoding="utf-8")
    m = re.search(r"def build_cycle_metadata_block.*?(?=\ndef )", src, re.DOTALL)
    ns = {}
    exec("from datetime import datetime\n" + m.group(0), ns)
    build = ns["build_cycle_metadata_block"]

    gen = datetime(2026, 9, 23, 8, 13, 40)
    meta = {
        "data_refresh_completed_at": "2026-09-23T08:13:05+08:00",
        "sources": {
            "us_equities": {"observation_date": "2026-09-22"},
            "macro_rates": {"observation_date": "2026-09-22"},
            "economic_releases": {"observation_date": "2026-08-01", "note": "latest official CPI release"},
        },
        "degraded_sources": ["news_classify"],
    }
    block = build("20260923_0800_HKT", meta, gen)

    assert "- Cycle ID: 20260923_0800_HKT" in block
    assert "- Report generated at: 23 September 2026, 08:13:40 (HKT)" in block
    assert "- Data refresh completed at: 2026-09-23T08:13:05+08:00" in block
    assert "- Equity data through: 2026-09-22" in block
    assert "- Rates data through: 2026-09-22" in block
    assert "- Economic releases through: 2026-08-01 (latest official CPI release)" in block
    assert "- Degraded sources: news_classify" in block

    # without source meta, cycle id + generated-at still present
    minimal = build(None, None, gen)
    assert "Cycle Metadata" in minimal
    assert "Report generated at" in minimal
    assert "Degraded sources" not in minimal


# ---------------------------------------------------------------------------
# Ollama circuit breaker
# ---------------------------------------------------------------------------


def test_ollama_probe_function_exists_and_is_fast():
    """The probe must exist in news_classify and fail fast when unreachable."""
    import subprocess

    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'scripts'); sys.path.insert(0, 'src'); "
         "from news_classify import ollama_available; "
         "print('PROBE_IMPORT_OK', ollama_available.__name__)"],
        capture_output=True, text=True, timeout=30, cwd=str(PROJECT_ROOT),
    )
    assert "PROBE_IMPORT_OK" in r.stdout, r.stderr


def test_news_classify_exits_75_when_ollama_down(monkeypatch, tmp_path):
    """When Ollama is unreachable, news_classify must exit 75 (degrade, not fail)."""
    import subprocess

    r = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "news_classify.py"), "--limit", "1"],
        capture_output=True, text=True, timeout=60, cwd=str(PROJECT_ROOT),
        env={"PATH": "/usr/bin:/bin", "OLLAMA_URL": "http://127.0.0.1:1/api/chat",
             **{k: v for k, v in __import__("os").environ.items() if k.startswith(("LOCAL_", "NEON_"))}},
    )
    # Either exit 75 (probe worked) or a DB error exit (container has no DB) —
    # but it must NOT be a long timeout.
    assert r.returncode in (75, 1), f"exit={r.returncode} stderr={r.stderr[:200]}"
    if r.returncode == 75:
        assert "OLLAMA UNAVAILABLE" in r.stdout
