from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "market-dashboard"


def test_dashboard_has_short_positioning_route_filters_and_detail_view():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "app.js").read_text(encoding="utf-8")

    assert 'id="short-positioning"' in html
    assert 'href="#short-positioning"' in html
    assert 'id="short-filter-form"' in html
    assert 'id="short-detail-form"' in html
    assert 'id="explorer"' in html
    assert 'embedded-explorer' in html
    assert html.count('id="data-form"') == 1
    assert "Single-Symbol Explorer" not in html
    assert "/short-analytics/latest" in script
    assert "loadShortTicker" in script
    assert "loadSingleStock" in script
    assert "formatNumber(row[key])" in script
    assert "regime_reason_json" in script
    assert "Short-Positioning Regime Evidence" in script
    assert "regime-evidence-table" in script


def test_dashboard_explains_relative_metrics_against_spy():
    script = (ROOT / "app.js").read_text(encoding="utf-8")

    assert "Weakness versus SPY" in script
    assert "Capture asymmetry" in script


def test_main_report_is_not_used_as_short_analytics_data_source():
    script = (ROOT / "app.js").read_text(encoding="utf-8")

    assert "loadShortAnalytics" in script
    assert "apiFetch(`/short-analytics/latest?" in script
