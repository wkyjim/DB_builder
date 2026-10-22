"""Export canonical 68-symbol MacroDB production universe.

Uses runtime import of deployed production code as source of truth.
"""
import sys
import json
from datetime import date, datetime
from pathlib import Path

# Add production src to path
DB_BUILDER_ROOT = Path(r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder")
sys.path.insert(0, str(DB_BUILDER_ROOT / "src"))

# Runtime import of production modules
from db_builder.config import local_engine
from db_builder import session_calendar as sc
import importlib.util

# Load macro_data_fetch.py via importlib
spec = importlib.util.spec_from_file_location(
    "macro_data_fetch",
    DB_BUILDER_ROOT / "scripts" / "macro_data_fetch.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Extract canonical values from runtime
APPROVED = getattr(mod, 'APPROVED_MACRO_SYMBOLS', None)
REQUIRED = getattr(mod, 'REQUIRED_SYMBOLS', None)
SOURCE_DEF = getattr(mod, 'SOURCE_DEFINITION_BY_SYMBOL', sc.SOURCE_DEFINITION_BY_SYMBOL)
CLOSE_MAP = getattr(mod, 'SYMBOL_CLOSE_MAP', None)
INVESTINY_ASSETS = getattr(mod, 'INVESTINY_ASSETS', set())
TREASURY_SYMBOLS = getattr(sc, 'TREASURY_SYMBOLS', set())
CMT_SYMBOLS = getattr(mod, 'CMT_SYMBOLS', set())

# Define asset type and provider mappings
ASSET_TYPE_MAP = {
    '^GSPC': 'Equity/Index', '^IXIC': 'Equity/Index', '^NDX': 'Equity/Index',
    '^DJI': 'Equity/Index', '^RUT': 'Equity/Index', '^VIX': 'Equity/Index',
    '^SKEW': 'Equity/Index', '^HSI': 'Equity/Index', '^N225': 'Equity/Index',
    '^KS11': 'Equity/Index', '^GDAXI': 'Equity/Index', '^FCHI': 'Equity/Index',
    '^FTSE': 'Equity/Index', '^MOVE': 'Rates/Yields',
    'TLT': 'ETF', 'HYG': 'ETF', 'JNK': 'ETF', 'LQD': 'ETF',
    'IWD': 'ETF', 'IWF': 'ETF', 'RSP': 'ETF', 'IEF': 'ETF', 'SHY': 'ETF',
    'DX-Y.NYB': 'FX',
    '^FVX': 'Rates/Yields', '^TNX': 'Rates/Yields', '^TYX': 'Rates/Yields',
    '000001.SS': 'Equity/Index',
    'BTC-USD': 'Crypto', 'ETH-USD': 'Crypto',
    'AUDUSD=X': 'FX', 'CHFUSD=X': 'FX', 'CNY=X': 'FX', 'EURUSD=X': 'FX',
    'GBPUSD=X': 'FX', 'HKD=X': 'FX', 'JPY=X': 'FX', 'NZDUSD=X': 'FX', 'SGD=X': 'FX',
    'ES=F': 'Futures', 'NQ=F': 'Futures', 'YM=F': 'Futures', 'RTY=F': 'Futures',
    'NIY=F': 'Futures', 'GC=F': 'Futures', 'SI=F': 'Futures', 'HG=F': 'Futures',
    'NG=F': 'Futures', 'BZ=F': 'Futures', 'CL=F': 'Futures',
    'US2YT=X': 'Rates/Yields', 'US3YT=X': 'Rates/Yields', 'US5YT=X': 'Rates/Yields',
    'US7YT=X': 'Rates/Yields', 'US10YT=X': 'Rates/Yields', 'US20YT=X': 'Rates/Yields',
    'US30YT=X': 'Rates/Yields',
    'US2YT_CMT': 'Rates/Yields', 'US3YT_CMT': 'Rates/Yields', 'US5YT_CMT': 'Rates/Yields',
    'US7YT_CMT': 'Rates/Yields', 'US10YT_CMT': 'Rates/Yields', 'US20YT_CMT': 'Rates/Yields',
    'US30YT_CMT': 'Rates/Yields',
    'HK50': 'Futures', 'KOR200c1': 'Futures', 'CIHc1': 'Futures',
    '^KS200': 'Equity/Index',
}

# Provider ticker mapping
PROVIDER_TICKER = {
    '^GSPC': ('Yahoo', 'SPX'), '^IXIC': ('Yahoo', 'IXIC'), '^NDX': ('Yahoo', 'NDX'),
    '^DJI': ('Yahoo', 'DJI'), '^RUT': ('Yahoo', 'RUT'), '^VIX': ('Yahoo', 'VIX'),
    '^SKEW': ('Yahoo', 'SKEW'), '^HSI': ('Yahoo', 'HSI'), '^N225': ('Yahoo', 'N225'),
    '^KS11': ('Yahoo', 'KS11'), '^GDAXI': ('Yahoo', 'GDAXI'), '^FCHI': ('Yahoo', 'FCHI'),
    '^FTSE': ('Yahoo', 'FTSE'), '^MOVE': ('Yahoo', 'MOVE'),
    'TLT': ('Yahoo', 'TLT'), 'HYG': ('Yahoo', 'HYG'), 'JNK': ('Yahoo', 'JNK'),
    'LQD': ('Yahoo', 'LQD'), 'IWD': ('Yahoo', 'IWD'), 'IWF': ('Yahoo', 'IWF'),
    'RSP': ('Yahoo', 'RSP'), 'IEF': ('Yahoo', 'IEF'), 'SHY': ('Yahoo', 'SHY'),
    'DX-Y.NYB': ('Yahoo', 'DX-Y.NYB'),
    '^FVX': ('Yahoo', 'FVX'), '^TNX': ('Yahoo', 'TNX'), '^TYX': ('Yahoo', 'TYX'),
    '000001.SS': ('Yahoo', '000001.SS'),
    'BTC-USD': ('Yahoo', 'BTC-USD'), 'ETH-USD': ('Yahoo', 'ETH-USD'),
    'AUDUSD=X': ('Yahoo', 'AUDUSD=X'), 'CHFUSD=X': ('Yahoo', 'CHFUSD=X'),
    'CNY=X': ('Yahoo', 'CNY=X'), 'EURUSD=X': ('Yahoo', 'EURUSD=X'),
    'GBPUSD=X': ('Yahoo', 'GBPUSD=X'), 'HKD=X': ('Yahoo', 'HKD=X'),
    'JPY=X': ('Yahoo', 'JPY=X'), 'NZDUSD=X': ('Yahoo', 'NZDUSD=X'),
    'SGD=X': ('Yahoo', 'SGD=X'),
    'ES=F': ('Investing', 'ES=F'), 'NQ=F': ('Investing', 'NQ=F'),
    'YM=F': ('Investing', 'YM=F'), 'RTY=F': ('Investing', 'RTY=F'),
    'NIY=F': ('Investing', 'NIY=F'), 'GC=F': ('Investing', 'GC=F'),
    'SI=F': ('Investing', 'SI=F'), 'HG=F': ('Investing', 'HG=F'),
    'NG=F': ('Investing', 'NG=F'), 'BZ=F': ('Investing', 'BZ=F'),
    'CL=F': ('Investing', 'CL=F'),
    'US2YT=X': ('Investing', 'US2YT=X'), 'US3YT=X': ('Investing', 'US3YT=X'),
    'US5YT=X': ('Investing', 'US5YT=X'), 'US7YT=X': ('Investing', 'US7YT=X'),
    'US10YT=X': ('Investing', 'US10YT=X'), 'US20YT=X': ('Investing', 'US20YT=X'),
    'US30YT=X': ('Investing', 'US30YT=X'),
    'US2YT_CMT': ('Treasury.gov', 'US2YT_CMT'), 'US3YT_CMT': ('Treasury.gov', 'US3YT_CMT'),
    'US5YT_CMT': ('Treasury.gov', 'US5YT_CMT'), 'US7YT_CMT': ('Treasury.gov', 'US7YT_CMT'),
    'US10YT_CMT': ('Treasury.gov', 'US10YT_CMT'), 'US20YT_CMT': ('Treasury.gov', 'US20YT_CMT'),
    'US30YT_CMT': ('Treasury.gov', 'US30YT_CMT'),
    'HK50': ('Investing', 'HK50'), 'KOR200c1': ('Investing', 'KOR200c1'),
    'CIHc1': ('Investing', 'CIHc1'),
    '^KS200': ('Investing', '^KS200'),
}

# Source definition short names
SOURCE_DEF_SHORT = {
    'YFINANCE_CASH_INDEX_DAILY': 'YFINANCE_CASH_INDEX_DAILY',
    'YFINANCE_CASH_EQUITY_DAILY': 'YFINANCE_CASH_EQUITY_DAILY',
    'YFINANCE_CASH_ETF_DAILY': 'YFINANCE_CASH_ETF_DAILY',
    'YFINANCE_CASH_FUTURES_DAILY': 'YFINANCE_CASH_FUTURES_DAILY',
    'YFINANCE_CASH_FX_DAILY': 'YFINANCE_CASH_FX_DAILY',
    'YFINANCE_CASH_RATE_DAILY': 'YFINANCE_CASH_RATE_DAILY',
    'YFINANCE_CRYPTO_24X7_DAILY': 'YFINANCE_CRYPTO_24X7_DAILY',
    'INVESTING_CASH_INDEX_DAILY': 'INVESTING_CASH_INDEX_DAILY',
    'INVESTING_CASH_ETF_DAILY': 'INVESTING_CASH_ETF_DAILY',
    'INVESTING_CASH_FUTURES_DAILY': 'INVESTING_CASH_FUTURES_DAILY',
    'INVESTING_CASH_FX_DAILY': 'INVESTING_CASH_FX_DAILY',
    'INVESTING_CASH_RATE_DAILY': 'INVESTING_CASH_RATE_DAILY',
    'INVESTING_HK40_FUTURES_DAILY': 'INVESTING_HK40_FUTURES_DAILY',
    'INVESTING_KOSPI200_FUTURES_DAILY': 'INVESTING_KOSPI200_FUTURES_DAILY',
    'INVESTING_SSE50_FUTURES_DAILY': 'INVESTING_SSE50_FUTURES_DAILY',
    'INVESTING_US2Y_BOND_YIELD_DAILY': 'INVESTING_US2Y_BOND_YIELD_DAILY',
    'INVESTING_US3Y_BOND_YIELD_DAILY': 'INVESTING_US3Y_BOND_YIELD_DAILY',
    'INVESTING_US5Y_BOND_YIELD_DAILY': 'INVESTING_US5Y_BOND_YIELD_DAILY',
    'INVESTING_US7Y_BOND_YIELD_DAILY': 'INVESTING_US7Y_BOND_YIELD_DAILY',
    'INVESTING_US10Y_BOND_YIELD_DAILY': 'INVESTING_US10Y_BOND_YIELD_DAILY',
    'INVESTING_US20Y_BOND_YIELD_DAILY': 'INVESTING_US20Y_BOND_YIELD_DAILY',
    'INVESTING_US30Y_BOND_YIELD_DAILY': 'INVESTING_US30Y_BOND_YIELD_DAILY',
    'TREASURY_GOV_PAR_YIELD_DAILY': 'TREASURY_GOV_PAR_YIELD_DAILY',
}

# Build the canonical universe
if APPROVED is None:
    APPROVED = REQUIRED
if REQUIRED is None:
    REQUIRED = APPROVED

# Convert to sorted list
approved_list = sorted(APPROVED) if isinstance(APPROVED, (set, list, tuple)) else sorted(APPROVED)
required_list = sorted(REQUIRED) if isinstance(REQUIRED, (set, list, tuple)) else sorted(REQUIRED)

# Build table rows
rows = []
for i, symbol in enumerate(approved_list, 1):
    name = ASSET_TYPE_MAP.get(symbol, 'Unknown')
    asset_type = ASSET_TYPE_MAP.get(symbol, 'Unknown')
    
    # Get provider info
    provider_info = PROVIDER_TICKER.get(symbol, ('Unknown', symbol))
    provider_name = provider_info[0]
    provider_ticker = provider_info[1]
    
    # Get source definition
    source_def = SOURCE_DEF.get(symbol, 'MISSING')
    source_def_short = SOURCE_DEF_SHORT.get(source_def, source_def)
    
    # Check membership
    in_required = symbol in REQUIRED
    in_close_map = symbol in CLOSE_MAP if CLOSE_MAP else False
    in_source_def = symbol in SOURCE_DEF
    is_investing = symbol in INVESTINY_ASSETS
    is_yahoo = provider_name == 'Yahoo'
    is_treasury = provider_name == 'Treasury.gov'
    is_crypto = asset_type == 'Crypto'
    
    rows.append({
        'index': i,
        'symbol': symbol,
        'name': name,
        'asset_type': asset_type,
        'provider': provider_name,
        'provider_ticker': provider_ticker,
        'source_definition': source_def_short,
        'in_required': in_required,
        'in_close_map': in_close_map,
        'in_source_def': in_source_def,
        'is_investing': is_investing,
        'is_yahoo': is_yahoo,
        'is_treasury': is_treasury,
        'is_crypto': is_crypto,
    })

# Validation
print("=" * 60)
print("VALIDATION")
print("=" * 60)
print(f"APPROVED_UNIVERSE_COUNT = {len(APPROVED)}")
print(f"REQUIRED_SYMBOLS_COUNT = {len(REQUIRED)}")
print(f"SOURCE_DEFINITION_BY_SYMBOL_COUNT = {len(SOURCE_DEF)}")
print(f"SYMBOL_CLOSE_MAP_COUNT = {len(CLOSE_MAP) if CLOSE_MAP else 0}")

# Check equality
approved_set = set(APPROVED) if not isinstance(APPROVED, set) else APPROVED
required_set = set(REQUIRED) if not isinstance(REQUIRED, set) else REQUIRED
print(f"REQUIRED_SYMBOLS == APPROVED_UNIVERSE: {approved_set == required_set}")

if approved_set != required_set:
    only_in_approved = approved_set - required_set
    only_in_required = required_set - approved_set
    if only_in_approved:
        print(f"  Only in APPROVED: {sorted(only_in_approved)}")
    if only_in_required:
        print(f"  Only in REQUIRED: {sorted(only_in_required)}")

print()
print("=" * 60)
print("CANONICAL 68-SYMBOL UNIVERSE")
print("=" * 60)
print(f"{'#':<3} {'Symbol':<12} {'Name':<35} {'Asset Type':<15} {'Provider':<12} {'Provider Ticker':<15} {'Source Definition':<35} {'Req':<5} {'Close':<5} {'SrcDef':<5} {'Inv':<5} {'YF':<5} {'TR':<5} {'Crypto':<5} {'Massive Mapping'}")
print("-" * 200)

for row in rows:
    print(f"{row['index']:<3} {row['symbol']:<12} {row['name']:<35} {row['asset_type']:<15} {row['provider']:<12} {row['provider_ticker']:<15} {row['source_definition']:<35} {'Y' if row['in_required'] else 'N':<5} {'Y' if row['in_close_map'] else 'N':<5} {'Y' if row['in_source_def'] else 'N':<5} {'Y' if row['is_investing'] else 'N':<5} {'Y' if row['is_yahoo'] else 'N':<5} {'Y' if row['is_treasury'] else 'N':<5} {'Y' if row['is_crypto'] else 'N':<5}")

print()
print("=" * 60)
print("PROVIDER SUMMARY")
print("=" * 60)
provider_counts = {}
for row in rows:
    p = row['provider']
    provider_counts[p] = provider_counts.get(p, 0) + 1
for p, c in sorted(provider_counts.items()):
    print(f"  {p} = {c}")

print()
print("=" * 60)
print("ASSET CLASS SUMMARY")
print("=" * 60)
asset_counts = {}
for row in rows:
    a = row['asset_type']
    asset_counts[a] = asset_counts.get(a, 0) + 1
for a, c in sorted(asset_counts.items()):
    print(f"  {a} = {c}")

print()
print("CANONICAL 68-SYMBOL UNIVERSE EXPORTED")
