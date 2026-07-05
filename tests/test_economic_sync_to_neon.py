from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts.economic_sync_to_neon import COLUMNS


def test_economic_sync_columns_include_raw_payload():
    assert COLUMNS[-1] == "raw_payload"
    assert "series_id" in COLUMNS
    assert "realtime_start" in COLUMNS
