import pandas as pd

from db_builder.short_backtest import conditional_si_change_table, evaluate_interval_predictiveness


def test_backtest_is_temporally_split_by_publication_date():
    frame = pd.DataFrame(
        {
            "end_publication_date": pd.date_range("2024-01-01", periods=100, freq="14D"),
            "actual_si_change": [0.05 if index % 2 else -0.05 for index in range(100)],
            "expected_si_direction": ["INCREASE" if index % 2 else "DECREASE" for index in range(100)],
            "mean_svr": range(100),
            "mean_abnormal_svr": range(100),
            "interval_casv": range(100),
            "fraction_above_75p": [index / 100 for index in range(100)],
            "fraction_above_90p": [index / 200 for index in range(100)],
            "svr_acceleration": range(100),
        }
    )
    result = evaluate_interval_predictiveness(frame)

    assert result["train_size"] == 70
    assert result["test_size"] == 30
    assert result["direction_metrics_test"][1]["hit_rate"] == 1.0


def test_conditional_table_reports_sample_size():
    frame = pd.DataFrame({"fraction_above_75p": [0.1, 0.9], "actual_si_change": [-0.01, 0.03]})
    rows = conditional_si_change_table(frame)
    assert sum(row["sample_size"] for row in rows) == 2
