# S&P 500 Point-in-Time Breadth

Standalone Phase 2 research dataset. It does not modify or feed the production regime model.

## Sources

- Membership: `public.sp500_membership_history` (Wikipedia historical changes + current list).
- Prices: `public.us_equities` (split-adjusted close/volume).
- Indicators: `public.us_equities_indicators` (MA, RSI, 52-week high/low, returns).

## Coverage

- Window: **2025-01-02 → 2026-11-06**
- Trading dates: **464**
- Constituents per date: **503–525**
- Average price coverage: **94.92%**
- Minimum price coverage: **93.12%**
- Dates below 90% coverage: **0**
- Missing constituent observations: **12331**
- Missing at first date (2025-01-02): **36**; at last date (2026-11-06): **0**
- First valid 50D breadth: **2025-03-17**
- First valid 200D breadth: **2025-10-20**
- First valid 20D positive breadth: **2025-02-03**

## Limitations

- Membership history is sourced from Wikipedia's historical changes table and current constituents. The table is not an official S&P DJI point-in-time feed.
- Removed/delisted names with no price history inherited from the local database are counted as active constituents but excluded from each breadth denominator; see coverage columns.
- Tickers with a trailing Wikipedia footnote marker (for example `ALLE |`) are normalized by stripping the marker before the price join.
- Historical membership before the first reliable change record may contain unknown start dates (`1900-01-01`).
- Breadth is computed on split-adjusted close and stored indicator values; no separate survivorship-bias-free price database exists.
- MA50/MA200/52-week-high-low windows require warm-up history. Early 2025 rows can have `indicator_rows_50 = 0` and NULL breadth ratios; these are missing observations, not zero breadth.
- Broad-market breadth (all tracked equities) is intentionally separate and not part of this table.

## Metric Definitions

- `pct_above_50d` / `pct_above_200d`: valid constituents with close ≥ MA50/MA200.
- `pct_positive_20d`: valid constituents with positive 20-day return.
- `advance_decline_ratio`: advancers / decliners using same-day pct_chg.
- `new_highs` / `new_lows`: close ≥ 52-week high / ≤ 52-week low.
- `breadth_50d_change_5d/20d`: change in `pct_above_50d` over 5/20 trading rows.
- `price_breadth_divergence`: `spx_return_20d_pct` − `breadth_50d_change_20d`.
- `negative_divergence`: SPX 20-day return positive while 50D breadth change is negative.