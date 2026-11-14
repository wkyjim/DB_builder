from __future__ import annotations

import copy

import hourly_ai_macro_run as ai


def _sources():
    sources = []
    for idx in range(12):
        ticker = ("AAA", "BBB", "QQQ")[idx % 3] if idx < 9 else None
        kind = "filing" if idx % 3 == 0 else ("etf" if ticker == "QQQ" else "news")
        claims = ["Diluted EPS 1.00", "Forward P/E 20.0", "Top holdings 50% weight"]
        sources.append({
            "id": f"S{idx:03d}", "status": "ok", "kind": kind, "ticker": ticker,
            "url": f"https://source{idx % 4}.example.com/{idx}",
            "claims": claims,
        })
    return sources


def _report(*, contaminated: bool):
    sources = _sources()
    ids = [s["id"] for s in sources]
    stock_a = {
        "ticker": "AAA", "security_type": "Stock", "source_ids": ids[:4],
        "eps": 1.0, "forward_pe": 20.0, "thesis": "same thesis",
        "valuation_assumptions": "Diluted EPS 1.00; forward P/E 20.0",
        "research_view": "KEEP", "base_target": 20, "bull_target": 25, "bear_target": 15,
        "assumption_inputs": {"base_multiple": 20, "bull_multiple": 25, "bear_multiple": 15},
    }
    stock_b = {
        "ticker": "BBB", "security_type": "Stock", "source_ids": ids[4:8],
        "eps": 2.0, "forward_pe": 25.0, "thesis": "different thesis",
        "valuation_assumptions": "Diluted EPS 2.00; forward P/E 25.0",
        "research_view": "KEEP", "base_target": 30, "bull_target": 35, "bear_target": 25,
        "assumption_inputs": {"base_multiple": 25, "bull_multiple": 30, "bear_multiple": 20},
    }
    if contaminated:
        stock_b.update({"eps": 1.0, "forward_pe": 20.0, "thesis": "same thesis",
                        "source_ids": ids[:4]})
    etf = {
        "ticker": "QQQ", "security_type": "ETF", "source_ids": ids[8:10],
        "top_holdings": ["AAA", "BBB"] if contaminated else ["AAA", "BBB", "CCC"],
        "portfolio_valuation": "top holdings" if contaminated else "Top holdings; 55% weight",
        "sector_exposure": "Technology", "underlying_drivers": "AI capex",
        "research_view": "KEEP", "thesis": "ETF thesis",
        "valuation_assumptions": "holdings/portfolio valuation",
        "assumption_inputs": {"base_return_pct": 5, "bull_return_pct": 15, "bear_return_pct": -10},
    }
    sectors = [{"sector": s, "source_ids": [ids[0]], "evidence": "e", "view": "v"}
               for s in ai.GICS]
    scenario_range = "S&P 500 range" if contaminated else "S&P 500 -2% to +2%"
    return {
        "ideas": [stock_a, stock_b, etf], "sectors": sectors,
        "macro_source_ids": ids[:3], "sources": [
            {"url": s["url"], "label": "x", "date": "2026-11-13"} for s in sources[:10]],
        "scenarios": {
            "short_term": [
                {"scenario": x, "probability": p, "range": scenario_range, "return_pct": 2, "assumptions": "a"}
                for x, p in (("Bull", 25), ("Base", 50), ("Bear", 25))],
            "long_term": [
                {"scenario": x, "probability": p, "range": scenario_range, "return_pct": 2, "assumptions": "a"}
                for x, p in (("Bull", 30), ("Base", 40), ("Bear", 30))],
        },
    }


def _validate(report):
    sources = _sources()
    evidence = {"candidates": [{"ticker": t} for t in ("AAA", "BBB", "QQQ")],
                "external_sources": {"sources": sources},
                "ticker_evidence": {
                    "AAA": {"ticker": "AAA", "security_type": "Stock", "forward_eps": 1.0,
                            "forward_pe": 20.0, "source_ids": [s["id"] for s in sources if s.get("ticker") == "AAA"]},
                    "BBB": {"ticker": "BBB", "security_type": "Stock", "forward_eps": 2.0,
                            "forward_pe": 25.0, "source_ids": [s["id"] for s in sources if s.get("ticker") == "BBB"]},
                    "QQQ": {"ticker": "QQQ", "security_type": "ETF", "holdings": [{"ticker": "AAA", "weight_pct": 10}, {"ticker": "BBB", "weight_pct": 9}, {"ticker": "CCC", "weight_pct": 8}],
                            "holdings_coverage_pct": 27.0, "source_ids": [s["id"] for s in sources if s.get("ticker") == "QQQ"]},
                }}
    return ai.validate(report, evidence, {"forecast_id": "f"})


def test_rejects_cross_ticker_valuation_source_and_narrative_reuse():
    fails = _validate(_report(contaminated=True))
    assert any(x.startswith("cross_ticker_valuation_reuse") for x in fails)
    assert any(x.startswith("cross_ticker_source_id_reuse") for x in fails)
    assert any(x.startswith("cross_ticker_narrative_reuse") for x in fails)


def test_rejects_incomplete_etf_holdings_and_placeholder_ranges():
    fails = _validate(_report(contaminated=True))
    assert "etf_holdings_incomplete:QQQ" in fails
    assert "etf_holdings_weights_not_numeric:QQQ" in fails
    assert "scenarios_short_term_range_not_numeric" in fails
    assert "scenarios_long_term_range_not_numeric" in fails


def test_accepts_distinct_source_backed_ideas_and_numeric_ranges():
    fails = _validate(_report(contaminated=False))
    assert not any(x.startswith("cross_ticker_valuation_reuse") for x in fails)
    assert "etf_holdings_incomplete:QQQ" not in fails
    assert "etf_holdings_weights_not_numeric:QQQ" not in fails
    assert "scenarios_short_term_range_not_numeric" not in fails


def test_apply_evidence_bindings_computes_targets_and_rejects_ai_numbers():
    report = {
        "ideas": [{
            "ticker": "AAA", "security_type": "Stock", "source_ids": ["S000"],
            "eps": 999, "forward_pe": 999, "base_target": 999, "bull_target": 999,
            "bear_target": 999, "price": 999,
            "assumption_inputs": {"base_multiple": 20, "bull_multiple": 25, "bear_multiple": 15},
        }],
        "scenarios": {"short_term": [{"scenario": "Bull", "return_pct": 2.0, "range": "bad"}]},
    }
    evidence = {"ticker_evidence": {"AAA": {
        "ticker": "AAA", "security_type": "Stock", "forward_eps": 2.0, "forward_pe": 20.0,
        "price": 40.0, "valuation_date": "2026-11-13", "source_ids": ["S000"],
        "diluted_eps_reported": 1.0, "diluted_eps_definition": "reported diluted EPS",
        "forward_eps_basis": "source.forwardEps", "fiscal_period": "2026-09-30",
    }}}
    out = ai.apply_evidence_bindings(report, evidence)
    idea = out["ideas"][0]
    assert idea["eps"] == 2.0
    assert idea["forward_pe"] == 20.0
    assert idea["base_target"] == 40.0
    assert idea["bull_target"] == 50.0
    assert idea["bear_target"] == 30.0
    assert idea["price"] == 40.0
    assert out["scenarios"]["short_term"][0]["range"] == "+2.00%"


def test_evidence_builder_does_not_use_historical_eps_as_forward_eps():
    evidence = {"candidates": [{"ticker": "AAA", "security_type": "Stock", "close": 40.0, "date": "2026-11-13"}]}
    ext = {"sources": [
        {"id": "S1", "status": "ok", "kind": "filing", "ticker": "AAA",
         "facts": {"diluted_eps": 1.0, "fiscal_end": "2026-09-30", "filed": "2026-10-20",
                   "definition": "reported diluted EPS"}},
        {"id": "S2", "status": "ok", "kind": "stock", "ticker": "AAA",
         "facts": {"forward_eps": 2.0, "forward_pe": 20.0, "valuation_date": "2026-11-13"}},
    ]}
    ev = ai.build_ticker_evidence(evidence, ext)["AAA"]
    assert ev["diluted_eps_reported"] == 1.0
    assert ev["forward_eps"] == 2.0
    assert ev["forward_pe"] == 20.0
    assert "source.forwardEps" in ev["forward_eps_basis"]


def test_evidence_builder_keeps_missing_forward_eps_null():
    evidence = {"candidates": [{"ticker": "AAA", "security_type": "Stock", "close": 40.0, "date": "2026-11-13"}]}
    ext = {"sources": [
        {"id": "S1", "status": "ok", "kind": "filing", "ticker": "AAA",
         "facts": {"diluted_eps": 1.0, "fiscal_end": "2026-09-30"}},
    ]}
    ev = ai.build_ticker_evidence(evidence, ext)["AAA"]
    assert ev["diluted_eps_reported"] == 1.0
    assert ev["forward_eps"] is None
    assert ev["forward_pe"] is None
