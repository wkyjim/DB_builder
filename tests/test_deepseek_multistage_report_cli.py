from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import deepseek_multistage_report as cli  # noqa: E402


def test_cli_accepts_stage_timeout(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["deepseek_multistage_report.py", "--stage-timeout", "180"])

    args = cli.parse_args()

    assert args.stage_timeout == 180


def test_cli_accepts_ollama_overrides(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deepseek_multistage_report.py",
            "--model",
            "deepseek-r1:14b",
            "--temperature",
            "0.1",
            "--top-p",
            "0.9",
            "--repeat-penalty",
            "1.2",
            "--num-ctx",
            "8192",
        ],
    )

    args = cli.parse_args()

    assert args.model == "deepseek-r1:14b"
    assert args.temperature == 0.1
    assert args.top_p == 0.9
    assert args.repeat_penalty == 1.2
    assert args.num_ctx == 8192


def test_cli_defaults_to_no_model_arg(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["deepseek_multistage_report.py"])

    args = cli.parse_args()

    assert args.model is None


def test_cli_accepts_diagnostics_flags(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deepseek_multistage_report.py",
            "--include-snapshot",
            "--include-prompts",
            "--include-stage-json",
            "--allow-warnings",
        ],
    )

    args = cli.parse_args()

    assert args.include_snapshot is True
    assert args.include_prompts is True
    assert args.include_stage_json is True
    assert args.allow_warnings is True
