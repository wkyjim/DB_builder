from __future__ import annotations

from datetime import date

from db_builder.global_economic_data import _period_to_date, WORLD_BANK_COUNTRIES, WORLD_BANK_INDICATORS, ECB_EXR_KEYS, ABS_DATAFLOWS


def test_period_to_date_handles_common_frequencies():
    assert _period_to_date("2026") == date(2026, 1, 1)
    assert _period_to_date("2026-05") == date(2026, 5, 1)
    assert _period_to_date("2026-Q2") == date(2026, 4, 1)
    assert _period_to_date("2026-06-30") == date(2026, 6, 30)


def test_global_registries_include_requested_economies_and_sources():
    assert {"CHN", "JPN", "DEU", "AUS", "EMU"} <= set(WORLD_BANK_COUNTRIES)
    assert "NY.GDP.MKTP.KD.ZG" in WORLD_BANK_INDICATORS
    assert "D.USD.EUR.SP00.A" in ECB_EXR_KEYS
    assert {"CPI", "LF", "ANA_AGG", "RT", "ITGS", "BOP"} <= set(ABS_DATAFLOWS)
