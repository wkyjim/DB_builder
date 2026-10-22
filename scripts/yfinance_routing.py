"""Which of the 21 Investing symbols does yfinance support?
Rule: prefer yfinance; use Investing HTML only where yfinance cannot serve it."""
import sys
import warnings

warnings.filterwarnings("ignore")
import yfinance as yf

INVESTING_TICKERS = [
    "US2YT=X", "US3YT=X", "US5YT=X", "US7YT=X", "US10YT=X", "US20YT=X", "US30YT=X",
    "BZ=F", "CL=F", "GC=F", "SI=F", "HG=F", "NG=F",
    "ES=F", "NQ=F", "YM=F", "RTY=F", "NIY=F",
    "HK50", "KOR200c1", "CIHc1",
]

# Yahoo's own UST yield tickers, for reference
YAHOO_YIELD_TICKERS = ["^IRX", "^FVX", "^TNX", "^TYX", "^UST2YR", "^US5YR", "^US10YR",
                       "^US30YR", "^US2YR", "^UST30YR", "^UST10YR"]

print("=" * 100)
print("A) yfinance support for the 21 Investing tickers")
print("=" * 100)
print()
print("  {:<12} {:>6} {:>12} {:>12}  {}".format("ticker", "rows", "first", "last", "verdict"))
print("  " + "-" * 88)

results = {}
for t in INVESTING_TICKERS:
    try:
        df = yf.Ticker(t).history(period="2y", auto_adjust=False)
        if df is None or df.empty:
            print("  {:<12} {:>6} {:>12} {:>12}  {}".format(t, 0, "-", "-", "NO DATA"))
            results[t] = (0, None, None)
        else:
            f, l = df.index[0].date(), df.index[-1].date()
            v = "yfinance OK" if len(df) > 100 else "yfinance (thin)"
            print("  {:<12} {:>6} {:>12} {:>12}  {}".format(t, len(df), f, l, v))
            results[t] = (len(df), f, l)
    except Exception as exc:
        print("  {:<12} {:>6} {:>12} {:>12}  ERROR {}".format(t, "-", "-", "-",
                                                              type(exc).__name__))
        results[t] = (0, None, None)

print()
print("=" * 100)
print("B) Yahoo's own UST yield tickers (possible mapping for the 7 yields)")
print("=" * 100)
print()
print("  {:<12} {:>6} {:>12} {:>12}".format("ticker", "rows", "first", "last"))
print("  " + "-" * 50)
for t in YAHOO_YIELD_TICKERS:
    try:
        df = yf.Ticker(t).history(period="2y", auto_adjust=False)
        if df is None or df.empty:
            print("  {:<12} {:>6} {:>12} {:>12}".format(t, 0, "-", "-"))
        else:
            print("  {:<12} {:>6} {:>12} {:>12}".format(t, len(df), df.index[0].date(),
                                                        df.index[-1].date()))
    except Exception as exc:
        print("  {:<12} {:>6} {:>12} {:>12}".format(t, "-", "-", "-"))

print()
print("=" * 100)
print("C) routing decision")
print("=" * 100)
print()
yf_ok = [t for t, (n, _, _) in results.items() if n > 100]
yf_no = [t for t in INVESTING_TICKERS if t not in yf_ok]
print("  yfinance can serve  : {}".format(len(yf_ok)))
print("    {}".format(yf_ok))
print()
print("  Investing HTML needed: {}".format(len(yf_no)))
print("    {}".format(yf_no))
