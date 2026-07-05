"""Generate local markdown investment intelligence reports."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from db_builder.critical_house_view import (
    build_critical_house_view,
    render_critical_pm_view,
    render_final_house_view,
    render_sector_critical_table,
)
from db_builder.llm_analyst_overlay import (
    generate_deepseek_cio_commentary,
    generate_qwen_overlay,
    render_qwen_overlay_markdown,
)
from db_builder.market_intelligence_report import (
    render_daily_house_view_report,
    render_market_pulse_report,
)
from db_builder.news_classifier import _ollama_chat, extract_json_object, ollama_model, ollama_url
from db_builder.opportunity_scanner import WATCHLIST_UNIVERSE


LOW_ARTICLE_COUNT_THRESHOLD = 10
QUALITY_SUMMARY_FIELDS = {
    "executive_summary",
    "top_risks",
    "top_opportunities",
    "positioning_bias",
}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _float(value, default: float = 0.0) -> float:
    if pd.isna(value):
        return default
    return float(value)


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if hasattr(value, "tolist"):
        return value.tolist()
    return []


def format_value(value, digits: int = 4) -> str:
    if value is None or pd.isna(value):
        return "n/a"
    if isinstance(value, float):
        return str(round(value, digits))
    return str(value)


def fetch_latest_market_regime(engine, *, window_hours: int) -> pd.DataFrame:
    sql = text("""
        SELECT *
        FROM public.market_regime_signals
        WHERE window_hours = :window_hours
        ORDER BY run_time DESC
        LIMIT 1
    """)
    return pd.read_sql(sql, engine, params={"window_hours": window_hours})


def fetch_latest_news_signals(engine, *, window_hours: int, limit: int = 20) -> pd.DataFrame:
    sql = text("""
        WITH latest_run AS (
            SELECT MAX(run_time) AS run_time
            FROM public.news_signals
            WHERE window_hours = :window_hours
        )
        SELECT s.*
        FROM public.news_signals s
        JOIN latest_run r ON r.run_time = s.run_time
        WHERE s.window_hours = :window_hours
        ORDER BY GREATEST(s.opportunity_score, s.risk_score) DESC,
                 s.article_count DESC,
                 s.dimension_value
        LIMIT :limit
    """)
    return pd.read_sql(sql, engine, params={"window_hours": window_hours, "limit": limit})


def fetch_latest_opportunity_signals(engine, *, window_hours: int, limit: int = 20) -> pd.DataFrame:
    sql = text("""
        WITH latest_run AS (
            SELECT MAX(run_time) AS run_time
            FROM public.opportunity_signals
            WHERE window_hours = :window_hours
        )
        SELECT o.*
        FROM public.opportunity_signals o
        JOIN latest_run r ON r.run_time = o.run_time
        WHERE o.window_hours = :window_hours
        ORDER BY o.opportunity_score DESC, o.risk_score DESC, o.ticker
        LIMIT :limit
    """)
    return pd.read_sql(sql, engine, params={"window_hours": window_hours, "limit": limit})


def fetch_latest_sector_signals(engine, *, window_hours: int, limit: int = 20) -> pd.DataFrame:
    sql = text("""
        WITH latest_run AS (
            SELECT MAX(run_time) AS run_time
            FROM public.sector_signals
            WHERE window_hours = :window_hours
        )
        SELECT s.*
        FROM public.sector_signals s
        JOIN latest_run r ON r.run_time = s.run_time
        WHERE s.window_hours = :window_hours
        ORDER BY s.rank ASC, s.final_score DESC, s.sector_name
        LIMIT :limit
    """)
    try:
        return pd.read_sql(sql, engine, params={"window_hours": window_hours, "limit": limit})
    except Exception:
        return pd.DataFrame()


def fetch_latest_market_regime_v2(engine, *, window_hours: int) -> pd.DataFrame:
    sql = text("""
        SELECT *
        FROM public.market_regime_v2
        WHERE window_hours = :window_hours
        ORDER BY run_time DESC
        LIMIT 1
    """)
    try:
        return pd.read_sql(sql, engine, params={"window_hours": window_hours})
    except Exception:
        return pd.DataFrame()


def fetch_latest_sector_regimes(engine, *, window_hours: int, limit: int = 20) -> pd.DataFrame:
    sql = text("""
        WITH latest_run AS (
            SELECT MAX(run_time) AS run_time
            FROM public.sector_regimes
            WHERE window_hours = :window_hours
        )
        SELECT s.*
        FROM public.sector_regimes s
        JOIN latest_run r ON r.run_time = s.run_time
        WHERE s.window_hours = :window_hours
        ORDER BY s.final_score DESC, s.sector_name
        LIMIT :limit
    """)
    try:
        return pd.read_sql(sql, engine, params={"window_hours": window_hours, "limit": limit})
    except Exception:
        return pd.DataFrame()


def fetch_latest_sector_rotation(engine, *, window_hours: int, limit: int = 30) -> pd.DataFrame:
    sql = text("""
        WITH latest_run AS (
            SELECT MAX(run_time) AS run_time
            FROM public.sector_rotation_signals
            WHERE window_hours = :window_hours
        )
        SELECT s.*
        FROM public.sector_rotation_signals s
        JOIN latest_run r ON r.run_time = s.run_time
        WHERE s.window_hours = :window_hours
        ORDER BY s.rotation_rank ASC, s.sector_name
        LIMIT :limit
    """)
    try:
        return pd.read_sql(sql, engine, params={"window_hours": window_hours, "limit": limit})
    except Exception:
        return pd.DataFrame()


def fetch_latest_secular_theme_signals(engine, *, window_hours: int, limit: int = 20) -> pd.DataFrame:
    sql = text("""
        WITH latest_run AS (
            SELECT MAX(run_time) AS run_time
            FROM public.secular_theme_signals
            WHERE window_hours = :window_hours
        )
        SELECT s.*
        FROM public.secular_theme_signals s
        JOIN latest_run r ON r.run_time = s.run_time
        WHERE s.window_hours = :window_hours
        ORDER BY s.secular_score DESC, s.tactical_score DESC, s.theme_name
        LIMIT :limit
    """)
    try:
        return pd.read_sql(sql, engine, params={"window_hours": window_hours, "limit": limit})
    except Exception:
        return pd.DataFrame()


def fetch_recent_articles(engine, *, window_hours: int, limit: int = 50) -> pd.DataFrame:
    sql = text("""
        SELECT
            a.article_id,
            a.title,
            a.summary,
            a.source_name,
            a.source_type,
            a.source_priority,
            a.source_category,
            a.url,
            a.published_at,
            a.fetched_at,
            c.sentiment_score,
            c.impact_score,
            c.confidence_score,
            c.themes,
            c.affected_tickers
        FROM public.news_articles a
        LEFT JOIN public.news_classifications c ON c.article_id = a.article_id
        WHERE COALESCE(a.published_at, a.fetched_at, c.classified_at)
            >= (now() - (:window_hours * INTERVAL '1 hour'))
        ORDER BY COALESCE(c.impact_score, 0) DESC,
                 COALESCE(a.published_at, a.fetched_at) DESC
        LIMIT :limit
    """)
    return pd.read_sql(sql, engine, params={"window_hours": window_hours, "limit": limit})


def fetch_latest_macro(engine, *, limit: int = 12) -> pd.DataFrame:
    sql = text("""
        SELECT DISTINCT ON (symbol)
            symbol,
            name,
            asset_type,
            date,
            close,
            pct_chg
        FROM public.macro
        WHERE pct_chg IS NOT NULL
        ORDER BY symbol, date DESC
    """)
    df = pd.read_sql(sql, engine)
    if df.empty:
        return df
    df["_abs_move"] = df["pct_chg"].astype(float).abs()
    key_symbols = [
        "^GSPC",
        "^IXIC",
        "^RUT",
        "^MOVE",
        "^FVX",
        "^TNX",
        "^TYX",
        "DXY",
        "DX-Y.NYB",
        "GC=F",
        "CL=F",
        "BZ=F",
        "HG=F",
        "^VIX",
        "BTC-USD",
        "ETH-USD",
        "HYG",
        "LQD",
        "JNK",
        "RSP",
        "IWF",
        "IWD",
        "TLT",
        "IEF",
        "SHY",
    ]
    key_rows = df[df["symbol"].isin(key_symbols)].copy()
    top_movers = df.sort_values("_abs_move", ascending=False).head(limit)
    combined = pd.concat([key_rows, top_movers], ignore_index=True)
    combined["_symbol_order"] = combined["symbol"].apply(
        lambda symbol: key_symbols.index(symbol) if symbol in key_symbols else len(key_symbols)
    )
    return (
        combined.sort_values(["_symbol_order", "_abs_move"], ascending=[True, False])
        .drop_duplicates(subset=["symbol"], keep="first")
        .drop(columns=["_abs_move", "_symbol_order"])
    )


def fetch_latest_watchlist_quality(engine, *, tickers: list[str] | None = None) -> pd.DataFrame:
    selected_tickers = tickers or sorted(WATCHLIST_UNIVERSE)
    params = {"tickers": selected_tickers}
    sql = text(f"""
        WITH latest_raw AS (
            SELECT DISTINCT ON (ticker)
                ticker,
                date,
                close,
                pct_chg
            FROM public.us_equities
            ORDER BY ticker, date DESC
        ),
        latest_indicators AS (
            SELECT DISTINCT ON (ticker)
                ticker,
                date AS indicator_date,
                rsi_14,
                ma_50,
                ma_200,
                return_20d
            FROM public.us_equities_indicators
            ORDER BY ticker, date DESC
        )
        SELECT
            r.ticker,
            r.date,
            r.close,
            r.pct_chg,
            i.indicator_date,
            i.rsi_14,
            i.ma_50,
            i.ma_200,
            i.return_20d
        FROM latest_raw r
        LEFT JOIN latest_indicators i ON i.ticker = r.ticker
        WHERE r.ticker = ANY(:tickers)
        ORDER BY r.ticker
    """)
    return pd.read_sql(sql, engine, params=params)


def collect_report_data(engine, *, window_hours: int) -> dict:
    regime = fetch_latest_market_regime(engine, window_hours=window_hours)
    regime_v2 = fetch_latest_market_regime_v2(engine, window_hours=window_hours)
    news_signals = fetch_latest_news_signals(engine, window_hours=window_hours)
    opportunities = fetch_latest_opportunity_signals(engine, window_hours=window_hours)
    sector_signals = fetch_latest_sector_signals(engine, window_hours=window_hours)
    sector_regimes = fetch_latest_sector_regimes(engine, window_hours=window_hours)
    sector_rotation = fetch_latest_sector_rotation(engine, window_hours=window_hours)
    secular_themes = fetch_latest_secular_theme_signals(engine, window_hours=window_hours)
    articles = fetch_recent_articles(engine, window_hours=window_hours)
    macro = fetch_latest_macro(engine)
    watchlist = fetch_latest_watchlist_quality(engine)
    return {
        "generated_at": datetime.now(timezone.utc),
        "window_hours": window_hours,
        "regime": regime.to_dict(orient="records"),
        "regime_v2": regime_v2.to_dict(orient="records"),
        "news_signals": news_signals.to_dict(orient="records"),
        "opportunities": opportunities.to_dict(orient="records"),
        "sector_signals": sector_signals.to_dict(orient="records"),
        "sector_regimes": sector_regimes.to_dict(orient="records"),
        "sector_rotation": sector_rotation.to_dict(orient="records"),
        "secular_themes": secular_themes.to_dict(orient="records"),
        "articles": articles.to_dict(orient="records"),
        "macro": macro.to_dict(orient="records"),
        "watchlist": watchlist.to_dict(orient="records"),
    }


def article_lookup(articles: list[dict]) -> dict[str, dict]:
    return {str(article.get("article_id")): article for article in articles if article.get("article_id")}


def format_article(article: dict) -> str:
    title = str(article.get("title") or "Untitled")
    source = str(article.get("source_name") or "unknown source")
    url = article.get("url")
    if url:
        return f"[{title}]({url}) ({source})"
    return f"{title} ({source})"


def render_theme_articles(signal: dict, articles_by_id: dict[str, dict], *, limit: int = 3) -> list[str]:
    rendered = []
    for article_id in _as_list(signal.get("top_article_ids"))[:limit]:
        article = articles_by_id.get(str(article_id))
        if article:
            rendered.append(format_article(article))
    return rendered


def _driver_lines(drivers, *, limit: int = 8) -> list[str]:
    if isinstance(drivers, str):
        try:
            drivers = json.loads(drivers)
        except json.JSONDecodeError:
            return [drivers]
    if isinstance(drivers, dict):
        lines = []
        for group, values in drivers.items():
            for value in _as_list(values):
                lines.append(f"{group}: {value}")
                if len(lines) >= limit:
                    return lines
        return lines
    return [str(value) for value in _as_list(drivers)[:limit]]


def render_investment_report(data: dict) -> str:
    generated_at = data.get("generated_at") or datetime.now(timezone.utc)
    window_hours = int(data.get("window_hours") or 24)
    regime_rows = data.get("regime") or []
    regime_v2_rows = data.get("regime_v2") or []
    news_signals = data.get("news_signals") or []
    opportunities = data.get("opportunities") or []
    sector_signals = data.get("sector_signals") or []
    sector_regimes = data.get("sector_regimes") or []
    sector_rotation = data.get("sector_rotation") or []
    secular_themes = data.get("secular_themes") or []
    articles = data.get("articles") or []
    macro = data.get("macro") or []
    watchlist = data.get("watchlist") or []
    articles_by_id = article_lookup(articles)
    article_count = len(articles)
    critical_view = data.get("critical_house_view") or build_critical_house_view(data)
    data["critical_house_view"] = critical_view

    regime = regime_rows[0] if regime_rows else {}
    regime_label = regime.get("regime_label", "unknown")
    confidence = _float(regime.get("confidence_score"))

    lines = [
        "# Daily Investment Intelligence Report",
        "",
        f"Generated at: {generated_at.isoformat()}",
        f"Window: {window_hours}h",
        "",
        "## Executive Summary",
        "",
        f"- Current market regime: **{regime_label}** with confidence `{round(confidence, 4)}`.",
        f"- News signals reviewed: `{len(news_signals)}`.",
        f"- Classified/recent articles reviewed: `{article_count}`.",
    ]
    if opportunities:
        top = opportunities[0]
        lines.append(
            f"- Top filtered opportunity: `{top.get('ticker')}` "
            f"({top.get('signal_label')}, score `{top.get('opportunity_score')}`)."
        )
    else:
        lines.append("- No high-conviction opportunities passed current filters.")
    lines.extend(["", "## Market Regime", ""])

    if regime:
        lines.extend(
            [
                f"- Regime: **{regime_label}**",
                f"- Confidence: `{round(confidence, 4)}`",
                f"- Risk-on score: `{regime.get('risk_on_score')}`",
                f"- Risk-off score: `{regime.get('risk_off_score')}`",
                f"- Source counts: news `{regime.get('news_signal_count')}`, macro `{regime.get('macro_signal_count')}`",
            ]
        )
        drivers = _as_list(regime.get("drivers"))
        if drivers:
            lines.append("- Drivers:")
            lines.extend([f"  - {driver}" for driver in drivers[:8]])
    else:
        lines.append("No market regime signal is available yet.")

    lines.extend(["", "## Market Regime 2.0", ""])
    regime_v2 = regime_v2_rows[0] if regime_v2_rows else {}
    if regime_v2:
        lines.extend(
            [
                f"- Market regime: **{regime_v2.get('market_regime')}**",
                f"- Market phase: **{regime_v2.get('market_phase')}**",
                f"- Confidence: `{format_value(regime_v2.get('confidence'))}`",
                f"- Market strength: `{regime_v2.get('market_strength')}`",
                f"- Trend: `{regime_v2.get('trend_state')}`",
                f"- Momentum: `{regime_v2.get('momentum_state')}`",
                f"- Volatility: `{regime_v2.get('volatility_state')}`",
                f"- Breadth: `{regime_v2.get('breadth_state')}`",
                f"- Risk appetite: `{regime_v2.get('risk_appetite_state')}`",
            ]
        )
        drivers = _driver_lines(regime_v2.get("drivers"))
        if drivers:
            lines.append("- Top drivers:")
            lines.extend([f"  - {driver}" for driver in drivers[:8]])
    else:
        lines.append("No Market Regime 2.0 signal is available yet.")

    lines.extend(["", render_critical_pm_view(critical_view).rstrip(), ""])
    lines.extend([render_sector_critical_table(critical_view).rstrip(), ""])
    lines.extend([render_final_house_view(critical_view).rstrip(), ""])

    lines.extend(["", "## Top News Themes", ""])
    theme_signals = [s for s in news_signals if s.get("dimension_type") == "theme"]
    if theme_signals:
        for signal in sorted(theme_signals, key=lambda s: _float(s.get("opportunity_score")), reverse=True)[:8]:
            lines.append(
                f"- **{signal.get('dimension_value')}**: articles `{signal.get('article_count')}`, "
                f"sentiment `{signal.get('weighted_sentiment_score')}`, "
                f"opportunity `{signal.get('opportunity_score')}`, risk `{signal.get('risk_score')}`"
            )
            for article_text in render_theme_articles(signal, articles_by_id):
                lines.append(f"  - {article_text}")
    else:
        lines.append("No theme-level news signals are available.")

    lines.extend(["", "## Risk Signals", ""])
    risk_signals = sorted(news_signals, key=lambda s: _float(s.get("risk_score")), reverse=True)[:8]
    if risk_signals:
        for signal in risk_signals:
            lines.append(
                f"- **{signal.get('dimension_type')}={signal.get('dimension_value')}**: "
                f"risk `{signal.get('risk_score')}`, sentiment `{signal.get('weighted_sentiment_score')}`"
            )
    else:
        lines.append("No risk signals are available.")
    if macro:
        lines.append("- Macro moves:")
        for row in macro[:6]:
            lines.append(
                f"  - {row.get('symbol')} {row.get('name')}: pct_chg `{row.get('pct_chg')}` "
                f"close `{row.get('close')}`"
            )

    lines.extend(["", "## Opportunity Signals", ""])
    if opportunities:
        for signal in opportunities[:10]:
            lines.append(
                f"- **{signal.get('ticker')}** ({signal.get('signal_label')}): "
                f"opportunity `{signal.get('opportunity_score')}`, risk `{signal.get('risk_score')}`, "
                f"technical `{signal.get('technical_score')}`"
            )
            for reason in _as_list(signal.get("reasons"))[:4]:
                lines.append(f"  - {reason}")
    else:
        lines.append("No high-conviction opportunities passed current filters.")

    lines.extend(["", "## Sector Intelligence", ""])
    if sector_signals:
        for signal in sorted(sector_signals, key=lambda s: _float(s.get("final_score")), reverse=True)[:5]:
            etfs = ", ".join(_as_list(signal.get("related_etfs"))) or "n/a"
            themes = ", ".join(_as_list(signal.get("top_themes"))) or "n/a"
            lines.append(
                f"- **{signal.get('sector_name')}**: final `{signal.get('final_score')}`, "
                f"ETFs `{etfs}`, opportunity `{signal.get('opportunity_score')}`, "
                f"risk `{signal.get('risk_score')}`, momentum `{signal.get('momentum_score')}`, "
                f"trend `{signal.get('trend_score')}`"
            )
            lines.append(f"  - Top themes: {themes}")
    else:
        lines.append("No sector intelligence signals are available yet.")

    lines.extend(["", "## Sector Regimes", ""])
    if sector_regimes:
        for signal in sorted(sector_regimes, key=lambda s: _float(s.get("final_score")), reverse=True):
            etfs = ", ".join(_as_list(signal.get("related_etfs"))) or "n/a"
            lines.append(
                f"- **{signal.get('sector_name')}**: regime `{signal.get('sector_regime')}`, "
                f"phase `{signal.get('cycle_phase')}`, confidence `{format_value(signal.get('confidence'))}`, "
                f"final `{format_value(signal.get('final_score'))}`, ETFs `{etfs}`"
            )
    else:
        lines.append("No sector regime signals are available yet.")

    lines.extend(["", "## Sector Rotation", ""])
    if sector_rotation:
        overweight = [
            signal for signal in sector_rotation
            if signal.get("allocation_bias") in {"overweight", "modest_overweight"}
        ][:5]
        defensive = [
            signal for signal in sector_rotation
            if signal.get("allocation_bias") in {"underweight", "avoid"}
        ][:5]
        if overweight:
            lines.append("- Overweight candidates:")
            for signal in overweight:
                etfs = ", ".join(_as_list(signal.get("related_etfs"))) or "n/a"
                lines.append(
                    f"  - **{signal.get('sector_name')}**: rank `{signal.get('rotation_rank')}`, "
                    f"score `{format_value(signal.get('rotation_score'))}`, "
                    f"action `{signal.get('recommended_action')}`, ETFs `{etfs}`"
                )
        else:
            lines.append("- No overweight sector rotation candidates are available.")
        if defensive:
            lines.append("- Underweight / avoid candidates:")
            for signal in defensive:
                etfs = ", ".join(_as_list(signal.get("related_etfs"))) or "n/a"
                lines.append(
                    f"  - **{signal.get('sector_name')}**: bias `{signal.get('allocation_bias')}`, "
                    f"action `{signal.get('recommended_action')}`, ETFs `{etfs}`"
                )
        else:
            lines.append("- No underweight or avoid sector rotation signals are available.")
    else:
        lines.append("No sector rotation signals are available yet.")

    lines.extend(["", "## Secular Themes", ""])
    if secular_themes:
        for signal in sorted(secular_themes, key=lambda s: _float(s.get("secular_score")), reverse=True)[:8]:
            etfs = ", ".join(_as_list(signal.get("related_etfs"))) or "n/a"
            subthemes = ", ".join(_as_list(signal.get("top_subthemes"))) or "n/a"
            lines.append(
                f"- **{signal.get('theme_name')}** ({signal.get('parent_theme')}): "
                f"secular `{format_value(signal.get('secular_score'))}`, "
                f"tactical `{format_value(signal.get('tactical_score'))}`, "
                f"phase `{signal.get('theme_phase')}`, confidence `{format_value(signal.get('confidence'))}`, "
                f"ETFs `{etfs}`"
            )
            lines.append(f"  - Top subthemes: {subthemes}")
    else:
        lines.append("No secular theme signals are available yet.")

    lines.extend(["", "## Tactical vs Secular Divergence", ""])
    if secular_themes:
        divergences = [
            signal for signal in secular_themes
            if abs(_float(signal.get("secular_score")) - _float(signal.get("tactical_score"))) >= 12
        ]
        if divergences:
            for signal in sorted(
                divergences,
                key=lambda s: abs(_float(s.get("secular_score")) - _float(s.get("tactical_score"))),
                reverse=True,
            )[:6]:
                secular = _float(signal.get("secular_score"))
                tactical = _float(signal.get("tactical_score"))
                if secular > tactical:
                    label = "long-term bullish, short-term correction"
                else:
                    label = "tactical rally, not yet structural"
                lines.append(
                    f"- **{signal.get('theme_name')}**: {label}; "
                    f"secular `{format_value(secular)}`, tactical `{format_value(tactical)}`"
                )
        else:
            lines.append("No major tactical/secular divergences are visible in the current window.")
    else:
        lines.append("No secular theme data is available for divergence analysis.")

    lines.extend(["", "## Watchlist Commentary", ""])
    if watchlist:
        for row in watchlist[:12]:
            rsi = row.get("rsi_14")
            comment = "technical data pending" if pd.isna(rsi) else f"RSI `{round(float(rsi), 2)}`"
            lines.append(
                f"- **{row.get('ticker')}**: close `{format_value(row.get('close'))}`, "
                f"pct_chg `{format_value(row.get('pct_chg'))}`, {comment}"
            )
    else:
        lines.append("No local watchlist equity data is available.")

    lines.extend(["", "## Data Quality Notes", ""])
    if article_count < LOW_ARTICLE_COUNT_THRESHOLD:
        lines.append(
            f"- Warning: article count is low (`{article_count}` articles in {window_hours}h); "
            "treat conclusions as directional only."
        )
    else:
        lines.append(f"- Article coverage looks usable for this window (`{article_count}` articles).")
    if not opportunities:
        lines.append("- Opportunity scanner filters are currently strict; zero passing names is an expected valid result.")
    if not regime:
        lines.append("- Market regime table has no matching local rows.")
    if not regime_v2_rows:
        lines.append("- Market Regime 2.0 table has no matching local rows.")
    if not news_signals:
        lines.append("- News signal table has no matching local rows.")
    if not secular_themes:
        lines.append("- Secular theme table has no matching local rows.")

    return "\n".join(lines).rstrip() + "\n"


def quality_summary_prompt(markdown: str) -> list[dict]:
    system = (
        "You are an investment strategist reviewing a generated market intelligence report. "
        "Return one valid JSON object only. No markdown. No prose outside JSON."
    )
    user = {
        "task": "Summarize the report into a concise investment decision layer.",
        "required_json_schema": {
            "executive_summary": "exactly 5 bullet strings",
            "top_risks": "list of concise risk strings",
            "top_opportunities": "list of concise opportunity strings",
            "positioning_bias": "one concise string describing portfolio stance",
        },
        "rules": [
            "Use only evidence from the report.",
            "If opportunities are absent, say that explicitly.",
            "Do not invent tickers or macro facts.",
            "Keep each bullet under 30 words.",
        ],
        "report_markdown": markdown[:20000],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=True)},
    ]


def normalize_quality_summary(payload: dict) -> dict:
    missing = sorted(QUALITY_SUMMARY_FIELDS - set(payload))
    if missing:
        raise ValueError(f"Missing quality summary fields: {missing}")

    executive_summary = payload.get("executive_summary")
    top_risks = payload.get("top_risks")
    top_opportunities = payload.get("top_opportunities")
    positioning_bias = payload.get("positioning_bias")

    if not isinstance(executive_summary, list) or not all(isinstance(item, str) for item in executive_summary):
        raise ValueError("executive_summary must be list[str]")
    if not isinstance(top_risks, list) or not all(isinstance(item, str) for item in top_risks):
        raise ValueError("top_risks must be list[str]")
    if not isinstance(top_opportunities, list) or not all(isinstance(item, str) for item in top_opportunities):
        raise ValueError("top_opportunities must be list[str]")

    summary = [item.strip() for item in executive_summary if item.strip()][:5]
    while len(summary) < 5:
        summary.append("Insufficient report evidence for an additional summary point.")

    return {
        "executive_summary": summary,
        "top_risks": [item.strip() for item in top_risks if item.strip()],
        "top_opportunities": [item.strip() for item in top_opportunities if item.strip()],
        "positioning_bias": str(positioning_bias or "Neutral until more evidence is available.").strip(),
    }


def summarize_report_with_deepseek(markdown: str, *, timeout: int = 120) -> dict:
    response = _ollama_chat(
        quality_summary_prompt(markdown),
        url=ollama_url(),
        model=ollama_model(),
        timeout=timeout,
    )
    content = response.get("message", {}).get("content", "")
    return normalize_quality_summary(extract_json_object(content))


def render_quality_summary(summary: dict) -> str:
    lines = ["## Report Quality Summary", "", "### 5-Bullet Executive Summary", ""]
    lines.extend([f"- {item}" for item in summary["executive_summary"]])
    lines.extend(["", "### Top Risks", ""])
    risks = summary["top_risks"] or ["No distinct top risks were identified by the quality layer."]
    lines.extend([f"- {item}" for item in risks])
    lines.extend(["", "### Top Opportunities", ""])
    opportunities = summary["top_opportunities"] or ["No high-conviction opportunities were identified."]
    lines.extend([f"- {item}" for item in opportunities])
    lines.extend(["", "### Positioning Bias", "", f"- {summary['positioning_bias']}"])
    return "\n".join(lines).rstrip() + "\n"


def append_quality_summary(markdown: str, *, timeout: int = 120, summarizer=summarize_report_with_deepseek) -> str:
    try:
        summary = summarizer(markdown, timeout=timeout)
        return markdown.rstrip() + "\n\n" + render_quality_summary(summary)
    except Exception as exc:
        fallback = {
            "executive_summary": [
                "Quality summary could not be generated from the local model.",
                "Use the base report sections for regime, themes, risks, opportunities, and data quality.",
                "Review model availability before relying on the quality layer.",
                "No additional positioning conclusions were added.",
                "The base report remains unchanged.",
            ],
            "top_risks": [f"Quality summary failure: {exc}"],
            "top_opportunities": ["No model-generated opportunity summary is available."],
            "positioning_bias": "No model-generated positioning bias is available.",
        }
        return markdown.rstrip() + "\n\n" + render_quality_summary(fallback)


def generate_investment_report(
    engine,
    *,
    window_hours: int,
    quality_summary: bool = True,
    timeout: int = 120,
    with_qwen_overlay: bool = False,
    with_deepseek_cio: bool = False,
    qwen_timeout: int = 120,
    deepseek_timeout: int = 300,
    report_type: str = "daily",
    include_appendix: bool = False,
) -> str:
    data = collect_report_data(engine, window_hours=window_hours)
    qwen_overlay = None
    if with_qwen_overlay:
        qwen_overlay = generate_qwen_overlay(
            engine,
            window_hours=window_hours,
            timeout=qwen_timeout,
            critical_pm_view=data.get("critical_house_view"),
        )
    if report_type == "pulse":
        markdown = render_market_pulse_report(data, qwen_overlay=qwen_overlay)
    elif report_type in {"daily", "house_view"}:
        markdown = render_daily_house_view_report(
            data,
            qwen_overlay=qwen_overlay,
            include_appendix=include_appendix,
        )
    elif report_type == "legacy":
        markdown = render_investment_report(data)
        if qwen_overlay:
            markdown = markdown.rstrip() + "\n\n" + render_qwen_overlay_markdown(qwen_overlay)
    else:
        raise ValueError(f"Unknown report_type: {report_type}")
    if with_deepseek_cio:
        commentary = generate_deepseek_cio_commentary(
            markdown,
            engine=engine,
            window_hours=window_hours,
            qwen_overlay=qwen_overlay,
            timeout=deepseek_timeout,
        )
        markdown = markdown.rstrip() + "\n\n" + commentary.rstrip() + "\n"
    if not quality_summary:
        return markdown
    return append_quality_summary(markdown, timeout=timeout)


def save_report(
    markdown: str,
    *,
    generated_at: datetime | None = None,
    reports_dir: Path | None = None,
    prefix: str = "investment_report",
) -> Path:
    timestamp = (generated_at or datetime.now()).strftime("%Y%m%d_%H%M%S")
    output_dir = reports_dir or (_project_root() / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{prefix}_{timestamp}.md"
    path.write_text(markdown, encoding="utf-8")
    return path
