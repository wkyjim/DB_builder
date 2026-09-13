from __future__ import annotations

from pathlib import Path

import pytest
import requests

from db_builder.telegram_bot import (
    TelegramConfig,
    _raise_for_telegram_status,
    build_command_response,
    build_report_update_summary,
    build_sector_summary,
    extract_section,
    split_telegram_message,
)


def test_telegram_http_error_does_not_expose_token_url():
    response = requests.Response()
    response.status_code = 403
    response.url = "https://api.telegram.org/botsecret-token/sendMessage"

    with pytest.raises(RuntimeError) as exc_info:
        _raise_for_telegram_status(response, "sendMessage")

    assert "secret-token" not in str(exc_info.value)
    assert str(exc_info.value) == "Telegram sendMessage failed with HTTP 403"


SAMPLE_REPORT = """# Rule-Based Institutional Market Update

Generated at: 21 July 2026, 12:11:19 (HKT)

## Executive Dashboard

- Regime score: **61.0 / 100** (Mild Risk-On)
- US equity strength: **72.0 / 100** (constructive)
- Evidence quality: **68.0 / 100**
- Top sector score: **Technology** `81.0`

## Sector and Theme Leadership

### Official Sector Strength

| Rank | Sector | Score | ETF Flow | Flow Reliability |
|---|---|---|---|---|
| 1 | Technology | 81.0 | 72.0 | 80.0 |
| 2 | Healthcare | 75.0 | 63.0 | 70.0 |
| 3 | Energy | 31.0 | 28.0 | 65.0 |

## Three-Month Outperformance Setup

| Rank | Theme | Score | Classification |
|---|---|---|---|
| 1 | Semiconductors | 78.0 | strong setup |

## Cross-Asset Confirmation

| Area | Signal | Interpretation |
|---|---|---|
| Equities | constructive | Trend is positive. |
| Volatility | calm | VIX is not stressed. |

## Macro Snapshot

| Symbol | Name | Close | Pct Chg | Market Date | Status |
|---|---|---|---|---|---|
| ^GSPC | S&P 500 | 6400 | 0.50 | 2026-07-21 | closed |

## Volatility and Risk Signals

- VIX unavailable from macro snapshot.

## Contradiction / Audit Flags

No contradiction flags were triggered by current deterministic rules.
"""


def test_extract_section_reads_nested_section():
    section = extract_section(SAMPLE_REPORT, "Official Sector Strength")
    assert "Technology" in section
    assert "Three-Month" not in section


def test_sector_summary_extracts_top_and_bottom():
    summary = build_sector_summary(SAMPLE_REPORT)
    assert "Highest score: Technology" in summary
    assert "Lowest score: Energy" in summary


def test_command_start_lists_commands(tmp_path: Path):
    report = tmp_path / "latest-report.md"
    report.write_text(SAMPLE_REPORT, encoding="utf-8")
    response = build_command_response("/start", report_path=report)
    assert "/market" in response
    assert "/equity AAPL" in response


def test_command_dashboard_uses_latest_report(tmp_path: Path):
    report = tmp_path / "latest-report.md"
    report.write_text(SAMPLE_REPORT, encoding="utf-8")
    response = build_command_response("/dashboard", report_path=report)
    assert "Latest Executive Dashboard" in response
    assert "Regime score: 61.0 / 100" in response


def test_report_update_summary_contains_dashboard_and_sectors(tmp_path: Path):
    report = tmp_path / "latest-report.md"
    report.write_text(SAMPLE_REPORT, encoding="utf-8")
    response = build_report_update_summary(SAMPLE_REPORT, report_path=report)
    assert "Report Update Notification" in response
    assert "Latest Executive Dashboard" in response
    assert "Top Sector Signals" in response


def test_split_telegram_message_chunks_long_text():
    chunks = split_telegram_message("a" * 8000, limit=3900)
    assert len(chunks) == 3
    assert all(len(chunk) <= 3900 for chunk in chunks)


def test_telegram_config_loads_standalone_bot_env(monkeypatch):
    import db_builder.telegram_bot as telegram_bot

    loaded_paths = []
    monkeypatch.delenv("TG_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)

    def fake_load_external_env(path, *, override):
        loaded_paths.append(Path(path))
        if Path(path).parent.name == "market-intelligence-telegram-bot":
            monkeypatch.setenv("TG_TOKEN", "test-token")
            monkeypatch.setenv("TG_CHAT_ID", "test-chat")

    monkeypatch.setattr(telegram_bot, "load_external_env", fake_load_external_env)

    config = TelegramConfig.from_env()

    assert config.token == "test-token"
    assert config.chat_id == "test-chat"
    assert loaded_paths[-1].name == ".env"
    assert loaded_paths[-1].parent.name == "market-intelligence-telegram-bot"
