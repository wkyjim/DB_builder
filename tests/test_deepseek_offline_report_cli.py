from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import deepseek_offline_report as cli  # noqa: E402


def test_cli_accepts_section_mode(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["deepseek_offline_report.py", "--section-mode"])

    args = cli.parse_args()

    assert args.section_mode is True


def test_cli_accepts_no_section_mode(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["deepseek_offline_report.py", "--no-section-mode"])

    args = cli.parse_args()

    assert args.section_mode is False


def test_cli_accepts_section_timeout(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["deepseek_offline_report.py", "--section-timeout", "180"])

    args = cli.parse_args()

    assert args.section_timeout == 180
