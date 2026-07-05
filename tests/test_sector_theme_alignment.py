from __future__ import annotations

from db_builder.sector_theme_alignment import align_sector_themes, signal_label


def test_signal_label_thresholds():
    assert signal_label(70) == "strong"
    assert signal_label(58) == "positive"
    assert signal_label(50) == "mixed"
    assert signal_label(40) == "weak"


def test_alignment_identifies_sector_and_theme_confirmation():
    sectors = [{"sector": "Healthcare", "score": 70}]
    themes = [{"theme": "Healthcare Innovation", "score": 68, "setup_score": 72}]

    rows = align_sector_themes(sectors, themes)

    assert rows[0]["theme_signal"] == "strong"
    assert rows[0]["interpretation"] == "sector and theme confirmation"


def test_alignment_identifies_theme_stronger_than_sector():
    sectors = [{"sector": "Technology", "score": 48}]
    themes = [
        {"theme": "AI Infrastructure", "score": 70, "setup_score": 65},
        {"theme": "Semiconductors", "score": 68, "setup_score": 64},
        {"theme": "Quality Growth", "score": 66, "setup_score": 61},
    ]

    rows = align_sector_themes(sectors, themes)

    assert rows[0]["sector_signal"] == "mixed"
    assert rows[0]["theme_signal"] == "strong"
    assert rows[0]["interpretation"] == "theme stronger than official sector"
