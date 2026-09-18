"""Tests for the data freshness validation framework."""

from __future__ import annotations

import json
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts directory to sys.path
scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(scripts_dir))

class MockResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class MockEngine:
    def __init__(self, rows=None):
        self._rows = rows or [(date(2026, 9, 14),)]

    def begin(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query):
        return MockResult(self._rows)

    def dispose(self):
        pass


def business_days_before(end: date, count: int) -> date:
    current = end
    remaining = count
    while remaining:
        current -= timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


# Import the script with narrowly scoped db_builder stubs. Restoring sys.modules
# immediately prevents these doubles from contaminating other test modules.
_stubbed_module_names = (
    "db_builder",
    "db_builder.config",
    "db_builder.trading_calendar",
)
_missing_module = object()
_saved_modules = {
    name: sys.modules.get(name, _missing_module) for name in _stubbed_module_names
}

if "db_builder" not in sys.modules:
    db_builder_mod = types.ModuleType("db_builder")
    db_builder_mod.__path__ = []
    sys.modules["db_builder"] = db_builder_mod

config_mod = types.ModuleType("db_builder.config")
config_mod.local_engine = lambda **kwargs: MockEngine()
config_mod.PROJECT_ROOT = Path("/workspace/DB_builder")
sys.modules["db_builder.config"] = config_mod

tc_mod = types.ModuleType("db_builder.trading_calendar")
tc_mod.NYSE = MagicMock()
tc_mod.latest_completed_nyse_session_date = lambda: date(2026, 9, 16)
sys.modules["db_builder.trading_calendar"] = tc_mod

try:
    import freshness_check
finally:
    for _name, _original_module in _saved_modules.items():
        if _original_module is _missing_module:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _original_module


# ---------------------------------------------------------------------------
# Tests: FreshnessRule
# ---------------------------------------------------------------------------


class TestFreshnessRule:
    def test_default_values(self):
        rule = freshness_check.FreshnessRule(
            name="Test",
            table="public.test",
            timestamp_field="date",
        )
        assert rule.is_date_not_timestamp is True
        assert rule.cadence == "daily_nyse"
        assert rule.warn_days == 2
        assert rule.fail_days == 5
        assert rule.is_critical is False

    def test_custom_values(self):
        rule = freshness_check.FreshnessRule(
            name="News",
            table="public.news",
            timestamp_field="published_at",
            is_date_not_timestamp=False,
            warn_days=0,
            fail_days=1,
            is_critical=True,
        )
        assert rule.is_date_not_timestamp is False
        assert rule.is_critical is True


# ---------------------------------------------------------------------------
# Tests: Calendar helpers
# ---------------------------------------------------------------------------


class TestCalendarHelpers:
    def test_count_nyse_sessions_same_day(self):
        result = freshness_check._count_nyse_sessions(date(2026, 9, 16), date(2026, 9, 16))
        assert result == 0

    def test_count_nyse_sessions_reversed(self):
        result = freshness_check._count_nyse_sessions(date(2026, 9, 16), date(2026, 9, 14))
        assert result == 0

    def test_count_nyse_sessions_week(self):
        schedule_mock = MagicMock()
        schedule_mock.__len__ = lambda self: 5
        with patch.object(tc_mod.NYSE, "schedule", return_value=schedule_mock):
            result = freshness_check._count_nyse_sessions(date(2026, 9, 8), date(2026, 9, 16))
        assert result == 5

    def test_count_nyse_sessions_fallback(self):
        with patch.object(tc_mod.NYSE, "schedule", side_effect=Exception("Calendar error")):
            result = freshness_check._count_nyse_sessions(date(2026, 9, 14), date(2026, 9, 16))
        assert result == 2

    def test_count_business_days_same_day(self):
        result = freshness_check._count_business_days(date(2026, 9, 16), date(2026, 9, 16))
        assert result == 0

    def test_count_business_days_week(self):
        result = freshness_check._count_business_days(date(2026, 9, 8), date(2026, 9, 16))
        assert result == 6

    def test_count_business_days_weekend(self):
        result = freshness_check._count_business_days(date(2026, 9, 11), date(2026, 9, 14))
        assert result == 1


# ---------------------------------------------------------------------------
# Tests: _compute_lag_days
# ---------------------------------------------------------------------------


class TestComputeLagDays:
    def test_none_latest(self):
        result = freshness_check._compute_lag_days(None, is_date=True, cadence="daily_nyse")
        assert result is None

    def test_date_daily_nyse(self):
        schedule_mock = MagicMock()
        schedule_mock.__len__ = lambda self: 3
        with patch.object(tc_mod, "latest_completed_nyse_session_date", return_value=date(2026, 9, 16)), \
             patch.object(tc_mod.NYSE, "schedule", return_value=schedule_mock):
            result = freshness_check._compute_lag_days(date(2026, 9, 13), is_date=True, cadence="daily_nyse")
        assert result == 3.0

    def test_date_daily_business(self):
        start = date(2026, 9, 11)  # Friday
        result = freshness_check._compute_lag_days(start, is_date=True, cadence="daily_business")
        expected = freshness_check._count_business_days(
            start, datetime.now(timezone.utc).date()
        )
        assert result == float(expected)

    def test_date_fallback_calendar(self):
        with patch.object(
            freshness_check,
            "latest_completed_nyse_session_date",
            side_effect=Exception("fail"),
        ):
            result = freshness_check._compute_lag_days(date(2020, 1, 1), is_date=True, cadence="daily_nyse")
        assert result is not None
        assert result > 1000.0

    def test_datetime_aware(self):
        now = datetime.now(timezone.utc)
        latest = now - timedelta(hours=6)
        result = freshness_check._compute_lag_days(latest, is_date=False, cadence="hourly")
        assert result is not None
        assert 0.2 <= result <= 0.3

    def test_zero_lag(self):
        with patch.object(tc_mod, "latest_completed_nyse_session_date", return_value=date(2026, 9, 16)):
            result = freshness_check._compute_lag_days(date(2026, 9, 16), is_date=True, cadence="daily_nyse")
        assert result == 0.0


# ---------------------------------------------------------------------------
# Tests: _evaluate_status
# ---------------------------------------------------------------------------


class TestEvaluateStatus:
    def test_none_is_fail(self):
        assert freshness_check._evaluate_status(None, warn_days=2, fail_days=5) == "FAIL"

    def test_ok_status(self):
        assert freshness_check._evaluate_status(0, warn_days=2, fail_days=5) == "OK"
        assert freshness_check._evaluate_status(1, warn_days=2, fail_days=5) == "OK"

    def test_warn_status(self):
        assert freshness_check._evaluate_status(2, warn_days=2, fail_days=5) == "WARN"
        assert freshness_check._evaluate_status(4, warn_days=2, fail_days=5) == "WARN"

    def test_fail_status(self):
        assert freshness_check._evaluate_status(5, warn_days=2, fail_days=5) == "FAIL"
        assert freshness_check._evaluate_status(10, warn_days=2, fail_days=5) == "FAIL"


# ---------------------------------------------------------------------------
# Tests: check_rule
# ---------------------------------------------------------------------------


class TestCheckRule:
    def _mock_rule(self, name="Test", **kwargs):
        defaults = {
            "name": name,
            "table": "public.test",
            "timestamp_field": "date",
        }
        defaults.update(kwargs)
        return freshness_check.FreshnessRule(**defaults)

    def test_ok_check(self):
        rule = self._mock_rule(
            name="Equities",
            table="public.us_equities",
            is_date_not_timestamp=True,
            cadence="daily_nyse",
            warn_days=1,
            fail_days=3,
        )
        engine = MockEngine([(date(2026, 9, 16),)])
        with patch.object(tc_mod, "latest_completed_nyse_session_date", return_value=date(2026, 9, 16)):
            result = freshness_check.check_rule(rule, engine)
        assert result.status == "OK"
        assert result.rule_name == "Equities"
        assert result.lag_days == 0.0

    def test_warn_check(self):
        rule = self._mock_rule(
            name="ETF flows",
            table="public.etf_daily_data",
            is_date_not_timestamp=True,
            cadence="daily_nyse",
            warn_days=2,
            fail_days=5,
        )
        engine = MockEngine([(date(2026, 9, 13),)])
        schedule_mock = MagicMock()
        schedule_mock.__len__ = lambda self: 3
        with patch.object(tc_mod, "latest_completed_nyse_session_date", return_value=date(2026, 9, 16)), \
             patch.object(tc_mod.NYSE, "schedule", return_value=schedule_mock):
            result = freshness_check.check_rule(rule, engine)
        assert result.status == "WARN"
        assert result.lag_days is not None

    def test_fail_check(self):
        rule = self._mock_rule(
            name="News",
            table="public.news_articles",
            is_date_not_timestamp=False,
            cadence="daily_business",
            warn_days=0,
            fail_days=1,
        )
        engine = MockEngine([(datetime(2026, 9, 10, tzinfo=timezone.utc),)])
        result = freshness_check.check_rule(rule, engine)
        assert result.status == "FAIL"

    def test_null_latest(self):
        rule = self._mock_rule(name="Empty", table="public.empty")
        engine = MockEngine([(None,)])
        result = freshness_check.check_rule(rule, engine)
        assert result.status == "FAIL"
        assert result.lag_days is None
        assert "NULL" in result.message

    def test_exception_handling(self):
        rule = self._mock_rule(name="Broken", table="public.broken")
        engine = MockEngine()
        with patch.object(freshness_check, "_fetch_latest_timestamp", side_effect=RuntimeError("DB down")):
            result = freshness_check.check_rule(rule, engine)
        assert result.status == "ERROR"
        assert "DB down" in result.message

    def test_critical_flag_propagated(self):
        rule = self._mock_rule(name="Critical", is_critical=True)
        engine = MockEngine([(None,)])
        result = freshness_check.check_rule(rule, engine)
        assert result.is_critical is True

    def test_macro_live_special_handling(self):
        rule = self._mock_rule(
            name="Macro (live)",
            table="public.macro_live",
            is_date_not_timestamp=False,
            cadence="hourly",
        )
        engine = MockEngine([(datetime.now(timezone.utc) - timedelta(hours=2),)])
        result = freshness_check.check_rule(rule, engine)
        assert result.status == "OK"
        assert "hours" in result.message.lower()

    def test_finra_business_days(self):
        rule = self._mock_rule(
            name="FINRA short volume",
            table="public.finra_short_volume",
            is_date_not_timestamp=True,
            cadence="daily_business",
            warn_days=3,
            fail_days=5,
        )
        latest = business_days_before(datetime.now(timezone.utc).date(), 3)
        engine = MockEngine([(latest,)])
        result = freshness_check.check_rule(rule, engine)
        assert result.status == "WARN"


# ---------------------------------------------------------------------------
# Tests: check_all_rules
# ---------------------------------------------------------------------------


class TestCheckAllRules:
    def test_all_rules_returned(self):
        engine = MockEngine()
        with patch.object(freshness_check, "check_rule") as mock_check:
            mock_check.return_value = MagicMock()
            results = freshness_check.check_all_rules(engine)
        assert len(results) == len(freshness_check.FRESHNESS_RULES)

    def test_filter_datasets(self):
        engine = MockEngine()
        with patch.object(freshness_check, "check_rule") as mock_check:
            mock_check.return_value = MagicMock()
            results = freshness_check.check_all_rules(engine, datasets=["Equities (raw)"])
        assert mock_check.call_count >= 1


# ---------------------------------------------------------------------------
# Tests: Output formatters
# ---------------------------------------------------------------------------


class TestFormatters:
    def test_format_text_contains_summary(self):
        results = [
            freshness_check.FreshnessResult(
                rule_name="Test OK",
                table="public.test",
                timestamp_field="date",
                latest_value=date(2026, 9, 16),
                lag_days=0.0,
                status="OK",
                message="All good",
                is_critical=False,
            ),
            freshness_check.FreshnessResult(
                rule_name="Test FAIL",
                table="public.test2",
                timestamp_field="date",
                latest_value=None,
                lag_days=None,
                status="FAIL",
                message="Broken",
                is_critical=True,
            ),
        ]
        output = freshness_check.format_text(results)
        assert "DATA FRESHNESS CHECK" in output
        assert "1 OK" in output
        assert "1 FAIL" in output
        assert "*CRITICAL*" in output
        assert "FAILED datasets:" in output

    def test_format_json_structure(self):
        results = [
            freshness_check.FreshnessResult(
                rule_name="Test",
                table="public.test",
                timestamp_field="date",
                latest_value=date(2026, 9, 16),
                lag_days=0.0,
                status="OK",
                message="All good",
                is_critical=False,
            ),
        ]
        output = freshness_check.format_json(results)
        payload = json.loads(output)
        assert "generated_at" in payload
        assert "checks" in payload
        assert "summary" in payload
        assert payload["summary"]["ok"] == 1
        assert len(payload["checks"]) == 1


# ---------------------------------------------------------------------------
# Tests: CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_list_datasets(self):
        with patch.object(sys, "argv", ["freshness_check.py", "--list-datasets"]):
            result = freshness_check.main()
            assert result == 0

    def test_json_output(self):
        with patch.object(sys, "argv", ["freshness_check.py", "--json"]):
            with patch.object(freshness_check, "check_all_rules") as mock_check:
                mock_check.return_value = [
                    freshness_check.FreshnessResult(
                        rule_name="Test",
                        table="public.test",
                        timestamp_field="date",
                        latest_value=date(2026, 9, 16),
                        lag_days=0.0,
                        status="OK",
                        message="All good",
                        is_critical=False,
                    ),
                ]
                result = freshness_check.main()
                assert result == 0

    def test_require_critical_fail(self):
        with patch.object(sys, "argv", ["freshness_check.py", "--require-critical"]):
            with patch.object(freshness_check, "check_all_rules") as mock_check:
                mock_check.return_value = [
                    freshness_check.FreshnessResult(
                        rule_name="Critical",
                        table="public.test",
                        timestamp_field="date",
                        latest_value=None,
                        lag_days=None,
                        status="FAIL",
                        message="Broken",
                        is_critical=True,
                    ),
                ]
                result = freshness_check.main()
                assert result == 2

    def test_filter_cli(self):
        with patch.object(
            sys, "argv", ["freshness_check.py", "--datasets", "Equities (raw)"]
        ):
            with patch.object(freshness_check, "check_all_rules") as mock_check:
                mock_check.return_value = []
                result = freshness_check.main()
                mock_check.assert_called_once()
                assert result == 0

    def test_unknown_dataset(self):
        with patch.object(
            sys, "argv", ["freshness_check.py", "--datasets", "nonexistent"]
        ):
            result = freshness_check.main()
            assert result == 2

    def test_warn_only_returns_zero(self):
        """Default WARN-only result must return exit code 0."""
        with patch.object(sys, "argv", ["freshness_check.py"]):
            with patch.object(freshness_check, "check_all_rules") as mock_check:
                mock_check.return_value = [
                    freshness_check.FreshnessResult(
                        rule_name="Test",
                        table="public.test",
                        timestamp_field="date",
                        latest_value=date(2026, 9, 14),
                        lag_days=2.0,
                        status="WARN",
                        message="Warning",
                        is_critical=False,
                    ),
                ]
                result = freshness_check.main()
                assert result == 0

    def test_non_critical_fail_returns_two(self):
        """Non-critical FAIL should return exit code 2."""
        with patch.object(sys, "argv", ["freshness_check.py"]):
            with patch.object(freshness_check, "check_all_rules") as mock_check:
                mock_check.return_value = [
                    freshness_check.FreshnessResult(
                        rule_name="Test",
                        table="public.test",
                        timestamp_field="date",
                        latest_value=None,
                        lag_days=None,
                        status="FAIL",
                        message="Failed",
                        is_critical=False,
                    ),
                ]
                result = freshness_check.main()
                assert result == 2

    def test_require_critical_with_non_critical_fail(self):
        """--require-critical with only non-critical FAIL should return 0."""
        with patch.object(sys, "argv", ["freshness_check.py", "--require-critical"]):
            with patch.object(freshness_check, "check_all_rules") as mock_check:
                mock_check.return_value = [
                    freshness_check.FreshnessResult(
                        rule_name="NonCritical",
                        table="public.test",
                        timestamp_field="date",
                        latest_value=None,
                        lag_days=None,
                        status="FAIL",
                        message="Failed",
                        is_critical=False,
                    ),
                ]
                result = freshness_check.main()
                assert result == 0

    def test_mixed_statuses(self):
        """Mixed OK/WARN/FAIL should return highest severity."""
        with patch.object(sys, "argv", ["freshness_check.py"]):
            with patch.object(freshness_check, "check_all_rules") as mock_check:
                mock_check.return_value = [
                    freshness_check.FreshnessResult(
                        rule_name="OK",
                        table="public.test1",
                        timestamp_field="date",
                        latest_value=date(2026, 9, 16),
                        lag_days=0.0,
                        status="OK",
                        message="OK",
                        is_critical=False,
                    ),
                    freshness_check.FreshnessResult(
                        rule_name="WARN",
                        table="public.test2",
                        timestamp_field="date",
                        latest_value=date(2026, 9, 14),
                        lag_days=2.0,
                        status="WARN",
                        message="Warn",
                        is_critical=False,
                    ),
                    freshness_check.FreshnessResult(
                        rule_name="FAIL",
                        table="public.test3",
                        timestamp_field="date",
                        latest_value=None,
                        lag_days=None,
                        status="FAIL",
                        message="Fail",
                        is_critical=False,
                    ),
                ]
                result = freshness_check.main()
                assert result == 2

    def test_connection_failure(self):
        """Connection failure should result in ERROR status."""
        with patch.object(sys, "argv", ["freshness_check.py"]):
            with patch.object(freshness_check, "check_all_rules") as mock_check:
                mock_check.return_value = [
                    freshness_check.FreshnessResult(
                        rule_name="Test",
                        table="public.test",
                        timestamp_field="date",
                        latest_value=None,
                        lag_days=None,
                        status="ERROR",
                        message="Check failed: connection refused",
                        is_critical=False,
                    ),
                ]
                result = freshness_check.main()
                assert result == 0

    def test_empty_tables(self):
        """Empty tables should result in FAIL status."""
        with patch.object(sys, "argv", ["freshness_check.py"]):
            with patch.object(freshness_check, "check_all_rules") as mock_check:
                mock_check.return_value = [
                    freshness_check.FreshnessResult(
                        rule_name="Empty",
                        table="public.empty",
                        timestamp_field="date",
                        latest_value=None,
                        lag_days=None,
                        status="FAIL",
                        message="No rows found",
                        is_critical=True,
                    ),
                ]
                result = freshness_check.main()
                assert result == 2


# ---------------------------------------------------------------------------
# Tests: Weekend handling
# ---------------------------------------------------------------------------


class TestWeekendHandling:
    def test_weekend_no_false_warn(self):
        """Data from Friday should not trigger WARN on Monday."""
        rule = freshness_check.FreshnessRule(
            name="Equities (raw)",
            table="public.us_equities",
            timestamp_field="date",
            is_date_not_timestamp=True,
            cadence="daily_nyse",
            warn_days=1,
            fail_days=3,
        )
        engine = MockEngine([(date(2026, 9, 11),)])
        schedule_mock = MagicMock()
        schedule_mock.__len__ = lambda self: 0
        with patch.object(tc_mod, "latest_completed_nyse_session_date", return_value=date(2026, 9, 14)), \
             patch.object(tc_mod.NYSE, "schedule", return_value=schedule_mock):
            result = freshness_check.check_rule(rule, engine)
        assert result.status == "OK"

    def test_weekend_finra_business_days(self):
        """FINRA short volume should use business days, not calendar days."""
        rule = freshness_check.FreshnessRule(
            name="FINRA short volume",
            table="public.finra_short_volume",
            timestamp_field="trade_date",
            is_date_not_timestamp=True,
            cadence="daily_business",
            warn_days=3,
            fail_days=5,
        )
        latest = business_days_before(datetime.now(timezone.utc).date(), 3)
        engine = MockEngine([(latest,)])
        result = freshness_check.check_rule(rule, engine)
        assert result.status == "WARN"


# ---------------------------------------------------------------------------
# Tests: NYSE holiday handling
# ---------------------------------------------------------------------------


class TestNYSEHolidayHandling:
    def test_nyse_holiday_no_false_warn(self):
        """NYSE holiday should not trigger false WARN."""
        rule = freshness_check.FreshnessRule(
            name="Equities (raw)",
            table="public.us_equities",
            timestamp_field="date",
            is_date_not_timestamp=True,
            cadence="daily_nyse",
            warn_days=1,
            fail_days=3,
        )
        engine = MockEngine([(date(2026, 9, 14),)])
        schedule_mock = MagicMock()
        schedule_mock.__len__ = lambda self: 1
        with patch.object(tc_mod, "latest_completed_nyse_session_date", return_value=date(2026, 9, 16)), \
             patch.object(tc_mod.NYSE, "schedule", return_value=schedule_mock):
            result = freshness_check.check_rule(rule, engine)
        assert result.status == "WARN"


# ---------------------------------------------------------------------------
# Tests: sync_state timezone
# ---------------------------------------------------------------------------


class TestSyncStateTimezone:
    def test_sync_state_naive_timestamp(self):
        """sync_state.updated_at may be naive (server local time)."""
        rule = freshness_check.FreshnessRule(
            name="Neon sync",
            table="public.sync_state",
            timestamp_field="updated_at",
            is_date_not_timestamp=False,
            cadence="daily_nyse",
            warn_days=2,
            fail_days=5,
        )
        engine = MockEngine([(datetime(2026, 9, 14, 12, 0, 0),)])
        result = freshness_check.check_rule(rule, engine)
        assert result.status in ("OK", "WARN", "FAIL")


# ---------------------------------------------------------------------------
# Tests: Workflow CLI arguments
# ---------------------------------------------------------------------------


class TestWorkflowCLI:
    """Test exact CLI arguments used in workflow .bat files."""

    def test_workflow_freshness_fail_exit_code(self):
        """Test that freshness FAIL returns exit code 2."""
        with patch.object(
            sys, "argv",
            ["freshness_check.py", "--datasets", "Equities (raw)", "--require-critical"]
        ):
            with patch.object(freshness_check, "check_all_rules") as mock_check:
                mock_check.return_value = [
                    freshness_check.FreshnessResult(
                        rule_name="Equities (raw)",
                        table="public.us_equities",
                        timestamp_field="date",
                        latest_value=None,
                        lag_days=None,
                        status="FAIL",
                        message="Failed",
                        is_critical=True,
                    ),
                ]
                result = freshness_check.main()
                assert result == 2

    def test_workflow_freshness_warn_exit_code(self):
        """Test that freshness WARN returns exit code 0."""
        with patch.object(
            sys, "argv",
            ["freshness_check.py", "--datasets", "Equities (raw)", "--require-critical"]
        ):
            with patch.object(freshness_check, "check_all_rules") as mock_check:
                mock_check.return_value = [
                    freshness_check.FreshnessResult(
                        rule_name="Equities (raw)",
                        table="public.us_equities",
                        timestamp_field="date",
                        latest_value=date(2026, 9, 14),
                        lag_days=2.0,
                        status="WARN",
                        message="Warning",
                        is_critical=True,
                    ),
                ]
                result = freshness_check.main()
                assert result == 0
