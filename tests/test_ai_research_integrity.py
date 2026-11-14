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
    }
    stock_b = {
        "ticker": "BBB", "security_type": "Stock", "source_ids": ids[4:8],
        "eps": 2.0, "forward_pe": 25.0, "thesis": "different thesis",
        "valuation_assumptions": "Diluted EPS 2.00; forward P/E 25.0",
        "research_view": "KEEP", "base_target": 30, "bull_target": 35, "bear_target": 25,
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
                {"scenario": x, "probability": p, "range": scenario_range, "assumptions": "a"}
                for x, p in (("Bull", 25), ("Base", 50), ("Bear", 25))],
            "long_term": [
                {"scenario": x, "probability": p, "range": scenario_range, "assumptions": "a"}
                for x, p in (("Bull", 30), ("Base", 40), ("Bear", 30))],
        },
    }


def _validate(report):
    sources = _sources()
    evidence = {"candidates": [{"ticker": t} for t in ("AAA", "BBB", "QQQ")],
                "external_sources": {"sources": sources}}
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
