"""Telegram notifications and command responses for market reports.

The module keeps Telegram I/O separate from report parsing so scheduled jobs can
dry-run summaries without sending messages or requiring network access.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import requests

from db_builder.config import local_engine
from db_builder.rule_based_market_data import fetch_macro_snapshot, fetch_market_technicals

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency in local env
    load_dotenv = None


MAX_TELEGRAM_MESSAGE_LENGTH = 3900

@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id: str
    username: str | None = None
    api_base: str = "https://api.telegram.org"

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        if load_dotenv is not None:
            load_dotenv()
        token = os.getenv("TG_token") or os.getenv("TG_TOKEN")
        chat_id = os.getenv("TG_chat_id") or os.getenv("TG_CHAT_ID")
        username = os.getenv("TG_username") or os.getenv("TG_USERNAME")
        if not token:
            raise RuntimeError("Missing Telegram bot token env var: TG_token")
        if not chat_id:
            raise RuntimeError("Missing Telegram chat id env var: TG_chat_id")
        return cls(token=token, chat_id=chat_id, username=username)

    @property
    def send_message_url(self) -> str:
        return f"{self.api_base}/bot{self.token}/sendMessage"

    @property
    def get_updates_url(self) -> str:
        return f"{self.api_base}/bot{self.token}/getUpdates"


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]

DEFAULT_DASHBOARD_REPORT = project_root() / "market-dashboard" / "data" / "latest-report.md"


def latest_report_path(explicit_path: Path | None = None) -> Path | None:
    if explicit_path:
        return explicit_path
    env_path = os.getenv("TG_REPORT_PATH") or os.getenv("LATEST_REPORT_PATH")
    if env_path:
        return Path(env_path)
    if DEFAULT_DASHBOARD_REPORT.exists():
        return DEFAULT_DASHBOARD_REPORT

    reports_dir = project_root() / "reports"
    candidates = sorted(reports_dir.glob("rule_based_market_update_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def read_report(path: Path | None = None) -> tuple[Path | None, str]:
    report_path = latest_report_path(path)
    if report_path is None or not report_path.exists():
        return report_path, ""
    return report_path, report_path.read_text(encoding="utf-8", errors="replace")


def split_telegram_message(text: str, limit: int = MAX_TELEGRAM_MESSAGE_LENGTH) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut < int(limit * 0.6):
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return chunks


def send_telegram_message(
    text: str,
    *,
    config: TelegramConfig | None = None,
    timeout: int = 30,
    dry_run: bool = False,
) -> list[str]:
    chunks = split_telegram_message(text)
    if dry_run:
        return chunks
    tg_config = config or TelegramConfig.from_env()
    sent: list[str] = []
    for chunk in chunks:
        response = requests.post(
            tg_config.send_message_url,
            json={
                "chat_id": tg_config.chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        sent.append(chunk)
    return sent


def normalize_heading(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def extract_section(markdown: str, heading: str) -> str:
    wanted = normalize_heading(heading)
    lines = markdown.splitlines()
    start: int | None = None
    start_level = 0
    for idx, line in enumerate(lines):
        match = re.match(r"^(#{2,6})\s+(.+?)\s*$", line)
        if not match:
            continue
        if normalize_heading(match.group(2)) == wanted:
            start = idx + 1
            start_level = len(match.group(1))
            break
    if start is None:
        return ""
    end = len(lines)
    for idx in range(start, len(lines)):
        match = re.match(r"^(#{2,6})\s+(.+?)\s*$", lines[idx])
        if match and len(match.group(1)) <= start_level:
            end = idx
            break
    return "\n".join(lines[start:end]).strip()


def first_lines(text: str, *, limit: int = 8) -> list[str]:
    rows = [line.strip() for line in text.splitlines() if line.strip()]
    return rows[:limit]


def markdown_table_rows(section: str) -> list[dict[str, str]]:
    rows = [line.strip() for line in section.splitlines() if line.strip().startswith("|")]
    if len(rows) < 3:
        return []
    headers = [cell.strip() for cell in rows[0].strip("|").split("|")]
    parsed: list[dict[str, str]] = []
    for row in rows[2:]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if len(cells) != len(headers):
            continue
        parsed.append(dict(zip(headers, cells)))
    return parsed


def clean_markdown_text(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    return text


def build_executive_dashboard(markdown: str) -> str:
    section = extract_section(markdown, "Executive Dashboard")
    if not section:
        return "Executive dashboard is unavailable. Generate a rule-based report first."
    return "Latest Executive Dashboard\n" + "\n".join(clean_markdown_text(line) for line in first_lines(section, limit=10))


def build_market_snapshot(markdown: str) -> str:
    dashboard = build_executive_dashboard(markdown)
    macro = extract_section(markdown, "Macro Snapshot")
    macro_rows = markdown_table_rows(macro)[:8]
    lines = [dashboard, "", "Macro Tape"]
    if macro_rows:
        for row in macro_rows:
            lines.append(
                f"- {row.get('Symbol', '')}: {row.get('Close', 'n/a')} ({row.get('Pct Chg', 'n/a')}), "
                f"{row.get('Status', 'n/a')} {row.get('Market Date', '')}"
            )
    else:
        lines.append("- Macro snapshot unavailable.")
    return "\n".join(lines)


def sector_rows(markdown: str) -> list[dict[str, str]]:
    return markdown_table_rows(extract_section(markdown, "Official Sector Strength"))


def build_sector_summary(markdown: str) -> str:
    rows = sector_rows(markdown)
    if not rows:
        return "Sector ranking is unavailable in the latest report."
    top = rows[0]
    bottom = rows[-1]
    return "\n".join(
        [
            "Top Sector Signals",
            f"- Highest score: {top.get('Sector', 'n/a')} ({top.get('Score', 'n/a')})",
            f"- Lowest score: {bottom.get('Sector', 'n/a')} ({bottom.get('Score', 'n/a')})",
            "",
            "Top 5 sectors:",
            *[
                f"- {row.get('Rank', idx)}. {row.get('Sector', 'n/a')}: {row.get('Score', 'n/a')} "
                f"| ETF Flow {row.get('ETF Flow', 'n/a')} | Reliability {row.get('Flow Reliability', 'n/a')}"
                for idx, row in enumerate(rows[:5], start=1)
            ],
        ]
    )


def build_signals_summary(markdown: str) -> str:
    themes = markdown_table_rows(extract_section(markdown, "Three-Month Outperformance Setup"))[:8]
    if not themes:
        return "Signals are unavailable in the latest report."
    lines = ["Latest Rule-Based Setup Signals"]
    for row in themes:
        lines.append(
            f"- {row.get('Theme', 'n/a')}: {row.get('Classification', 'n/a')} "
            f"(score {row.get('Score', 'n/a')})"
        )
    lines.append("")
    lines.append("Note: this report uses deterministic setup labels, not discretionary buy/sell calls.")
    return "\n".join(lines)


def build_macro_summary(markdown: str) -> str:
    section = extract_section(markdown, "Cross-Asset Confirmation")
    rows = markdown_table_rows(section)[:8]
    if not rows:
        return "Macro summary is unavailable in the latest report."
    lines = ["Latest Macro Summary"]
    for row in rows:
        lines.append(f"- {row.get('Area', 'n/a')}: {row.get('Signal', 'n/a')} - {row.get('Interpretation', 'n/a')}")
    return "\n".join(lines)


def build_risk_summary(markdown: str) -> str:
    risk = first_lines(extract_section(markdown, "Volatility and Risk Signals"), limit=8)
    flags = markdown_table_rows(extract_section(markdown, "Contradiction / Audit Flags"))[:5]
    lines = ["Current Market Risk Alerts"]
    lines.extend(clean_markdown_text(line) for line in risk if line)
    if flags:
        lines.append("")
        lines.append("Audit flags:")
        for row in flags:
            lines.append(f"- {row.get('Severity', 'n/a')}: {row.get('Issue', 'n/a')}")
    elif len(lines) == 1:
        lines.append("- No risk section found in latest report.")
    return "\n".join(lines)


def build_report_update_summary(markdown: str, *, report_path: Path | None = None) -> str:
    header = "Report Update Notification"
    if report_path:
        try:
            updated_at = datetime.fromtimestamp(report_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            header = f"{header}\nlatest-report.md updated at {updated_at}"
        except OSError:
            pass
    return "\n\n".join([header, build_executive_dashboard(markdown), build_sector_summary(markdown), build_risk_summary(markdown)])


def build_status_summary(report_path: Path | None, markdown: str) -> str:
    lines = ["System Status"]
    if report_path and report_path.exists():
        lines.append(f"- Latest report: {report_path}")
        lines.append(f"- Report modified: {datetime.fromtimestamp(report_path.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        lines.append("- Latest report: unavailable")
    lines.append(f"- Report parse: {'ok' if markdown else 'missing'}")
    try:
        engine = local_engine()
        macro = fetch_macro_snapshot(engine)
        tech = fetch_market_technicals(engine, tickers=["SPY", "QQQ", "IWM"])
        lines.append(f"- Local DB macro rows fetched: {len(macro)}")
        lines.append(f"- Local DB equity rows fetched: {len(tech)}")
    except Exception as exc:  # Status command should never crash bot polling.
        lines.append(f"- Local DB check failed: {type(exc).__name__}: {exc}")
    return "\n".join(lines)


def build_equity_lookup(ticker: str) -> str:
    ticker = ticker.upper().strip()
    if not ticker:
        return "Usage: /equity AAPL"
    try:
        engine = local_engine()
        df = fetch_market_technicals(engine, tickers=[ticker])
    except Exception as exc:
        return f"Equity lookup failed for {ticker}: {type(exc).__name__}: {exc}"
    if df.empty:
        return f"No latest equity/indicator row found for {ticker}."
    row = df.iloc[0].to_dict()
    return "\n".join(
        [
            f"Equity Snapshot: {ticker}",
            f"- Name: {row.get('name') or 'n/a'}",
            f"- Date: {row.get('date') or 'n/a'}",
            f"- Close: {row.get('close') or 'n/a'}",
            f"- 1D pct change: {row.get('pct_chg') or 'n/a'}",
            f"- RSI 14: {row.get('rsi_14') or 'n/a'}",
            f"- 20D return: {row.get('return_20d') or 'n/a'}",
            f"- 60D return: {row.get('return_60d') or 'n/a'}",
            f"- Volume ratio 20D: {row.get('volume_ratio_20') or 'n/a'}",
        ]
    )


START_TEXT = """Market Intelligence Bot

Commands:
/market - Show the latest market snapshot
/dashboard - Show the latest executive dashboard
/report - Show the latest full market report
/signals - Show latest deterministic setup signals
/sectors - Show top and bottom sector scores
/equity AAPL - Look up latest stock data and signals
/macro - Show latest macro summary
/risk - Show current market risk alerts
/status - Show database, API, and data update status
"""


def build_command_response(command_text: str, *, report_path: Path | None = None) -> str:
    path, markdown = read_report(report_path)
    command_text = command_text.strip()
    command, _, arg_text = command_text.partition(" ")
    command = command.lower()

    if command == "/start":
        return START_TEXT.strip()
    if command == "/market":
        return build_market_snapshot(markdown)
    if command == "/dashboard":
        return build_executive_dashboard(markdown)
    if command == "/report":
        if not markdown:
            return "Latest report is unavailable."
        return clean_markdown_text(markdown)
    if command == "/signals":
        return build_signals_summary(markdown)
    if command == "/sectors":
        return build_sector_summary(markdown)
    if command == "/equity":
        return build_equity_lookup(arg_text)
    if command == "/macro":
        return build_macro_summary(markdown)
    if command == "/risk":
        return build_risk_summary(markdown)
    if command == "/status":
        return build_status_summary(path, markdown)
    return "Unknown command. Use /start for available commands."


def get_updates(config: TelegramConfig, *, offset: int | None = None, timeout: int = 30) -> list[dict]:
    params: dict[str, int] = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    response = requests.get(config.get_updates_url, params=params, timeout=timeout + 10)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram getUpdates failed: {payload}")
    return payload.get("result", [])


def poll_updates(
    *,
    config: TelegramConfig | None = None,
    report_path: Path | None = None,
    once: bool = False,
    dry_run: bool = False,
) -> None:
    tg_config = config or TelegramConfig.from_env()
    offset: int | None = None
    while True:
        updates = get_updates(tg_config, offset=offset)
        for update in updates:
            offset = int(update["update_id"]) + 1
            message = update.get("message") or {}
            text = message.get("text")
            if not text:
                continue
            response = build_command_response(text, report_path=report_path)
            send_telegram_message(response, config=tg_config, dry_run=dry_run)
        if once:
            break


def build_named_message(kind: str, *, report_path: Path | None = None, message: str | None = None) -> str:
    path, markdown = read_report(report_path)
    if kind == "dashboard":
        return build_executive_dashboard(markdown)
    if kind == "market":
        return build_market_snapshot(markdown)
    if kind == "morning":
        return "Morning Positioning Report\n\n" + build_market_snapshot(markdown) + "\n\n" + build_sector_summary(markdown)
    if kind == "eod":
        return "End-of-Day Market Report\n\n" + build_market_snapshot(markdown) + "\n\n" + build_risk_summary(markdown)
    if kind == "sectors":
        return build_sector_summary(markdown)
    if kind == "macro":
        return build_macro_summary(markdown)
    if kind == "risk":
        return build_risk_summary(markdown)
    if kind == "status":
        return build_status_summary(path, markdown)
    if kind == "report-update":
        return build_report_update_summary(markdown, report_path=path)
    if kind == "report":
        return clean_markdown_text(markdown) if markdown else "Latest report is unavailable."
    if kind == "alert":
        return "System Alert\n" + (message or "A data/API update failure was reported.")
    raise ValueError(f"Unknown message type: {kind}")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram bot and notifications for market reports.")
    parser.add_argument(
        "--send",
        choices=["dashboard", "market", "morning", "eod", "sectors", "macro", "risk", "status", "report-update", "report", "alert"],
        help="Send one named notification.",
    )
    parser.add_argument("--message", help="Alert text for --send alert.")
    parser.add_argument("--report-path", type=Path, default=None)
    parser.add_argument("--poll", action="store_true", help="Run Telegram long polling for bot commands.")
    parser.add_argument("--once", action="store_true", help="Process one getUpdates batch and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Print message text instead of sending to Telegram.")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.poll:
        poll_updates(report_path=args.report_path, once=args.once, dry_run=args.dry_run)
        return 0
    if not args.send:
        raise SystemExit("Use --send <type> or --poll.")

    text = build_named_message(args.send, report_path=args.report_path, message=args.message)
    if args.dry_run:
        print(text)
        return 0
    send_telegram_message(text)
    return 0
