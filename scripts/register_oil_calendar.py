"""Register OILPRICEAPI_SPOT as a session-date-labelled definition on the NYSE calendar.

Evidence: 261 daily observations, 0 weekend rows, Mon-Fri evenly distributed.
The API publishes on US market holidays (Thanksgiving, Christmas, ...) but never
on weekends, so the previous *observation* for any row is the previous NYSE
session -- which is exactly what SESSION_DATE_LABELLED_DEFINITIONS + an
ExchangeCalendarAdapter("XNYS") gives us.
"""
import datetime
import shutil

ROOT = r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder"
SC = ROOT + r"\src\db_builder\session_calendar.py"
stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

src = open(SC, encoding="utf-8").read()

# ---- 1. SESSION_DATE_LABELLED_DEFINITIONS
OLD = """SESSION_DATE_LABELLED_DEFINITIONS: Set[str] = {
    SOURCE_DEF_TREASURY_GOV_PAR_YIELD,
    SOURCE_DEF_CRYPTO_24_7_DAILY,
    SOURCE_DEF_YFINANCE_FX_DAILY,
    SOURCE_DEF_YFINANCE_CASH_EQUITY_DAILY,
    SOURCE_DEF_YFINANCE_CASH_INDEX_DAILY,
}"""
NEW = """SESSION_DATE_LABELLED_DEFINITIONS: Set[str] = {
    SOURCE_DEF_TREASURY_GOV_PAR_YIELD,
    SOURCE_DEF_CRYPTO_24_7_DAILY,
    SOURCE_DEF_YFINANCE_FX_DAILY,
    SOURCE_DEF_YFINANCE_CASH_EQUITY_DAILY,
    SOURCE_DEF_YFINANCE_CASH_INDEX_DAILY,
    # OilPriceAPI daily_average bars carry a US-business-day date label (0 weekend
    # rows across 261 observations; it does publish on US market holidays, so the
    # previous observation for any row is the previous NYSE session).
    SOURCE_DEF_OILPRICEAPI_SPOT,
}"""
assert src.count(OLD) == 1
src = src.replace(OLD, NEW)
print("  [1] SESSION_DATE_LABELLED_DEFINITIONS += SOURCE_DEF_OILPRICEAPI_SPOT")

# ---- 2. adapter_for branch
OLD = """        elif symbol in CBOE_ICE_SYMBOLS:
            adapter = ExchangeCalendarAdapter("XNYS")"""
NEW = """        elif symbol in CBOE_ICE_SYMBOLS:
            adapter = ExchangeCalendarAdapter("XNYS")
        elif symbol in OILPRICEAPI_SYMBOLS:
            adapter = ExchangeCalendarAdapter("XNYS")"""
assert src.count(OLD) == 1
src = src.replace(OLD, NEW)
print("  [2] adapter_for: OILPRICEAPI_SYMBOLS -> XNYS")

# ---- 3. the symbol set (next to the other symbol sets)
OLD = """CRYPTO_SYMBOLS: Set[str] = {"BTC-USD", "ETH-USD"}"""
NEW = """CRYPTO_SYMBOLS: Set[str] = {"BTC-USD", "ETH-USD"}
OILPRICEAPI_SYMBOLS: Set[str] = {"BRENT_OILPRICE", "WTI_OILPRICE"}"""
assert src.count(OLD) == 1
src = src.replace(OLD, NEW)
print("  [3] OILPRICEAPI_SYMBOLS defined")

shutil.copy2(SC, "{}.backup_{}".format(SC, stamp))
open(SC, "w", encoding="utf-8").write(src)
print()
print("  backup -> {}.backup_{}".format(SC.split("\\")[-1], stamp))

import py_compile
try:
    py_compile.compile(SC, doraise=True)
    print("  compiles OK")
except Exception as exc:
    print("  COMPILE FAIL:", exc)
