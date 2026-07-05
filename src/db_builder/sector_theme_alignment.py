"""Bridge sector and thematic strength into alignment diagnostics."""

from __future__ import annotations

from db_builder.rule_based_config import SECTOR_THEME_MAP


def signal_label(score: float) -> str:
    if score >= 65:
        return "strong"
    if score >= 55:
        return "positive"
    if score >= 45:
        return "mixed"
    if score >= 35:
        return "weak"
    return "very weak"


def align_sector_themes(sectors: list[dict], themes: list[dict]) -> list[dict]:
    themes_by_name = {row["theme"]: row for row in themes}
    output = []
    for sector in sectors:
        sector_name = sector["sector"]
        linked_theme_names = SECTOR_THEME_MAP.get(sector_name, [])
        linked_themes = [themes_by_name[name] for name in linked_theme_names if name in themes_by_name]
        if linked_themes:
            avg_theme_score = sum(float(row["score"]) for row in linked_themes) / len(linked_themes)
            avg_setup_score = sum(float(row.get("setup_score", row["score"])) for row in linked_themes) / len(linked_themes)
            theme_signal = signal_label(avg_theme_score)
            related_theme_text = ", ".join(row["theme"] for row in linked_themes)
        else:
            avg_theme_score = None
            avg_setup_score = None
            theme_signal = "unavailable"
            related_theme_text = "none"
        sector_score = float(sector["score"])
        sector_signal = signal_label(sector_score)
        output.append(
            {
                "sector": sector_name,
                "related_themes": related_theme_text,
                "sector_score": round(sector_score, 4),
                "sector_signal": sector_signal,
                "theme_score": round(avg_theme_score, 4) if avg_theme_score is not None else None,
                "theme_setup_score": round(avg_setup_score, 4) if avg_setup_score is not None else None,
                "theme_signal": theme_signal,
                "interpretation": _interpret_alignment(sector_signal, theme_signal),
            }
        )
    return output


def _interpret_alignment(sector_signal: str, theme_signal: str) -> str:
    if theme_signal == "unavailable":
        return "sector-only signal"
    if sector_signal in {"strong", "positive"} and theme_signal in {"strong", "positive"}:
        return "sector and theme confirmation"
    if sector_signal in {"weak", "very weak"} and theme_signal in {"weak", "very weak"}:
        return "broad weakness across sector and themes"
    if sector_signal in {"strong", "positive"} and theme_signal in {"weak", "very weak", "mixed"}:
        return "sector stronger than related themes"
    if sector_signal in {"weak", "very weak", "mixed"} and theme_signal in {"strong", "positive"}:
        return "theme stronger than official sector"
    return "mixed confirmation"
