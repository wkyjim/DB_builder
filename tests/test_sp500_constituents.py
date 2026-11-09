from datetime import date

from db_builder.sp500_constituents import Member, build_intervals


def _member(members, ticker, start=None, end=None):
    return next(
        m for m in members
        if m.ticker == ticker
        and (start is None or m.start_date == start)
        and (end is None or m.end_date == end)
    )


def test_current_member_gets_open_interval():
    current = [{"ticker": "AAPL", "company_name": "Apple", "date_added": date(1982, 11, 30)}]
    members = build_intervals(current, [], today=date(2026, 11, 9))
    row = _member(members, "AAPL")
    assert row.start_date == date(1982, 11, 30)
    assert row.end_date is None


def test_replacement_closes_removed_and_opens_added():
    current = [
        {"ticker": "OKTA", "company_name": "Okta", "date_added": date(2026, 10, 30)},
        {"ticker": "AAPL", "company_name": "Apple", "date_added": date(1982, 11, 30)},
    ]
    changes = [
        {"date": date(2026, 10, 30), "action": "remove", "ticker": "KVUE", "company_name": "Kenvue", "reason": "acquisition"},
        {"date": date(2026, 10, 30), "action": "add", "ticker": "OKTA", "company_name": "Okta", "reason": "acquisition"},
    ]
    members = build_intervals(current, changes, today=date(2026, 11, 9))
    removed = _member(members, "KVUE")
    assert removed.end_date == date(2026, 10, 30)
    assert _member(members, "OKTA").start_date == date(2026, 10, 30)
    assert _member(members, "OKTA").end_date is None


def test_unknown_start_removals_do_not_duplicate_primary_key():
    changes = [
        {"date": date(1998, 12, 11), "action": "remove", "ticker": "AN", "company_name": None, "reason": None},
        {"date": date(2017, 8, 8), "action": "remove", "ticker": "AN", "company_name": None, "reason": None},
    ]
    members = build_intervals([], changes, today=date(2026, 11, 9))
    rows = [m for m in members if m.ticker == "AN"]
    assert len(rows) == 1
    assert rows[0].start_date == date(1900, 1, 1)
    assert rows[0].end_date == date(2017, 8, 8)
