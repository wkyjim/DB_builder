from __future__ import annotations

from db_builder import deepseek_snapshot_builder as builder


class Engine:
    pass


def test_snapshot_includes_market_state(monkeypatch):
    monkeypatch.setattr(builder, "fetch_market_state", lambda engine, window_hours: {"market_regime": "risk_on", "evidence_id": "REGIME_001"})
    monkeypatch.setattr(builder, "fetch_cross_asset", lambda engine: [])
    monkeypatch.setattr(builder, "fetch_technicals", lambda engine: [])
    monkeypatch.setattr(builder, "fetch_news", lambda engine, window_hours: [])
    monkeypatch.setattr(builder, "fetch_sector_data", lambda engine, window_hours: [])
    monkeypatch.setattr(builder, "fetch_secular_themes", lambda engine, window_hours: [])
    monkeypatch.setattr(builder, "fetch_opportunities", lambda engine, window_hours: [])
    monkeypatch.setattr(builder, "fetch_news_signals", lambda engine, window_hours: [])

    snapshot = builder.build_offline_snapshot(Engine(), window_hours=24)

    assert snapshot["market_state"]["market_regime"] == "risk_on"
    assert snapshot["market_state"]["evidence_id"] == "REGIME_001"


def test_snapshot_includes_technicals(monkeypatch):
    monkeypatch.setattr(builder, "fetch_market_state", lambda engine, window_hours: {})
    monkeypatch.setattr(builder, "fetch_cross_asset", lambda engine: [])
    monkeypatch.setattr(builder, "fetch_technicals", lambda engine: [{"ticker": "SPY", "evidence_id": "TECH_001"}])
    monkeypatch.setattr(builder, "fetch_news", lambda engine, window_hours: [])
    monkeypatch.setattr(builder, "fetch_sector_data", lambda engine, window_hours: [])
    monkeypatch.setattr(builder, "fetch_secular_themes", lambda engine, window_hours: [])
    monkeypatch.setattr(builder, "fetch_opportunities", lambda engine, window_hours: [])
    monkeypatch.setattr(builder, "fetch_news_signals", lambda engine, window_hours: [])

    snapshot = builder.build_offline_snapshot(Engine(), window_hours=24)

    assert snapshot["technicals"][0]["ticker"] == "SPY"


def test_snapshot_includes_top_news_sector_and_themes(monkeypatch):
    monkeypatch.setattr(builder, "fetch_market_state", lambda engine, window_hours: {})
    monkeypatch.setattr(builder, "fetch_cross_asset", lambda engine: [])
    monkeypatch.setattr(builder, "fetch_technicals", lambda engine: [])
    monkeypatch.setattr(builder, "fetch_news", lambda engine, window_hours: [{"title": "Fed keeps rates unchanged", "evidence_id": "NEWS_001"}])
    monkeypatch.setattr(builder, "fetch_sector_data", lambda engine, window_hours: [{"sector": "Technology", "evidence_id": "SECTOR_001"}])
    monkeypatch.setattr(builder, "fetch_secular_themes", lambda engine, window_hours: [{"theme": "AI Infrastructure", "evidence_id": "THEME_001"}])
    monkeypatch.setattr(builder, "fetch_opportunities", lambda engine, window_hours: [])
    monkeypatch.setattr(builder, "fetch_news_signals", lambda engine, window_hours: [])

    snapshot = builder.build_offline_snapshot(Engine(), window_hours=24)

    assert snapshot["news"][0]["title"] == "Fed keeps rates unchanged"
    assert snapshot["sector_data"][0]["sector"] == "Technology"
    assert snapshot["secular_themes"][0]["theme"] == "AI Infrastructure"


def test_snapshot_includes_evidence_ids(monkeypatch):
    monkeypatch.setattr(builder, "fetch_market_state", lambda engine, window_hours: {"evidence_id": "REGIME_001"})
    monkeypatch.setattr(builder, "fetch_cross_asset", lambda engine: [{"symbol": "^VIX", "evidence_id": "MACRO_001"}])
    monkeypatch.setattr(builder, "fetch_technicals", lambda engine: [{"ticker": "SPY", "evidence_id": "TECH_001"}])
    monkeypatch.setattr(builder, "fetch_news", lambda engine, window_hours: [])
    monkeypatch.setattr(builder, "fetch_sector_data", lambda engine, window_hours: [])
    monkeypatch.setattr(builder, "fetch_secular_themes", lambda engine, window_hours: [])
    monkeypatch.setattr(builder, "fetch_opportunities", lambda engine, window_hours: [])
    monkeypatch.setattr(builder, "fetch_news_signals", lambda engine, window_hours: [])

    snapshot = builder.build_offline_snapshot(Engine(), window_hours=24)
    ids = {row["evidence_id"] for row in snapshot["evidence_appendix"]}

    assert {"REGIME_001", "MACRO_001", "TECH_001"} <= ids
