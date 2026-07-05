"""Global economic indicator fetchers using free official sources."""

from __future__ import annotations

from datetime import date, datetime, timezone
from io import StringIO
import json

import pandas as pd
import requests


WORLD_BANK_BASE_URL = "https://api.worldbank.org/v2/country/{country}/indicator/{indicator}"
ECB_BASE_URL = "https://data-api.ecb.europa.eu/service/data/{dataflow}/{key}"
ABS_BASE_URL = "https://data.api.abs.gov.au/rest/data/{dataflow}/all"


WORLD_BANK_COUNTRIES = {
    "CHN": ("China", "China"),
    "JPN": ("Japan", "Japan"),
    "DEU": ("Germany", "Germany"),
    "AUS": ("Australia", "Australia"),
    "EMU": ("Euro Area", "Eurozone"),
}


WORLD_BANK_INDICATORS = {
    "NY.GDP.MKTP.KD.ZG": ("GDP growth", "growth", "annual", "annual percent"),
    "FP.CPI.TOTL.ZG": ("Inflation, consumer prices", "inflation", "annual", "annual percent"),
    "SL.UEM.TOTL.ZS": ("Unemployment rate", "labor", "annual", "percent"),
    "BN.CAB.XOKA.GD.ZS": ("Current account balance", "external", "annual", "percent of GDP"),
    "NE.EXP.GNFS.ZS": ("Exports of goods and services", "trade", "annual", "percent of GDP"),
    "NE.IMP.GNFS.ZS": ("Imports of goods and services", "trade", "annual", "percent of GDP"),
    "FM.LBL.BMNY.GD.ZS": ("Broad money", "liquidity", "annual", "percent of GDP"),
}


ECB_EXR_KEYS = {
    "D.USD.EUR.SP00.A": ("US dollar/Euro ECB reference exchange rate", "US", "fx"),
    "D.JPY.EUR.SP00.A": ("Japanese yen/Euro ECB reference exchange rate", "Japan", "fx"),
    "D.CNY.EUR.SP00.A": ("Chinese yuan/Euro ECB reference exchange rate", "China", "fx"),
    "D.GBP.EUR.SP00.A": ("Pound sterling/Euro ECB reference exchange rate", "United Kingdom", "fx"),
    "D.AUD.EUR.SP00.A": ("Australian dollar/Euro ECB reference exchange rate", "Australia", "fx"),
}


ABS_DATAFLOWS = {
    "CPI": ("Consumer Price Index", "inflation"),
    "LF": ("Labour Force", "labor"),
    "ANA_AGG": ("Australian National Accounts Key Aggregates", "growth"),
    "RT": ("Retail Trade", "growth"),
    "ITGS": ("International Trade in Goods", "trade"),
    "BOP": ("Balance of Payments", "external"),
    "PPI_FD": ("Producer Price Indexes, Final Demand", "inflation"),
    "WPI": ("Wage Price Index", "labor"),
}


def _period_to_date(value: str) -> date | None:
    text = str(value)
    try:
        if "-Q" in text:
            year, quarter = text.split("-Q", 1)
            month = (int(quarter) - 1) * 3 + 1
            return date(int(year), month, 1)
        if len(text) == 7 and text[4] == "-":
            return datetime.strptime(text, "%Y-%m").date()
        if len(text) == 4 and text.isdigit():
            return date(int(text), 1, 1)
        return datetime.fromisoformat(text).date()
    except Exception:
        return None


def _row(
    *,
    date_value: date,
    series_id: str,
    series_name: str,
    source: str,
    country: str,
    region: str,
    category: str,
    frequency: str,
    value,
    unit: str,
    raw_payload: dict,
) -> dict:
    return {
        "date": date_value,
        "series_id": series_id,
        "series_name": series_name,
        "source": source,
        "country": country,
        "region": region,
        "category": category,
        "frequency": frequency,
        "value": float(value),
        "unit": unit,
        "seasonal_adjustment": "",
        "realtime_start": date.today(),
        "realtime_end": date(9999, 12, 31),
        "updated_at": datetime.now(timezone.utc),
        "raw_payload": raw_payload,
    }


def fetch_world_bank_rows(*, start_year: int = 2025, timeout: int = 12) -> list[dict]:
    rows: list[dict] = []
    for country_code, (country_name, region) in WORLD_BANK_COUNTRIES.items():
        for indicator, (name, category, frequency, unit) in WORLD_BANK_INDICATORS.items():
            url = WORLD_BANK_BASE_URL.format(country=country_code, indicator=indicator)
            try:
                response = requests.get(url, params={"format": "json", "per_page": 200}, timeout=timeout)
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                print(f"[WARN] World Bank skipped {country_code} {indicator}: {exc}", flush=True)
                continue
            observations = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
            for observation in observations:
                if observation.get("value") is None:
                    continue
                period = observation.get("date")
                if not period or int(period) < start_year:
                    continue
                rows.append(
                    _row(
                        date_value=date(int(period), 1, 1),
                        series_id=f"WB:{country_code}:{indicator}",
                        series_name=f"{country_name} {name}",
                        source="World Bank",
                        country=country_code,
                        region=region,
                        category=category,
                        frequency=frequency,
                        value=observation["value"],
                        unit=unit,
                        raw_payload=observation,
                    )
                )
    return rows


def fetch_ecb_rows(*, start_date: str = "2026-04-01", timeout: int = 30) -> list[dict]:
    rows: list[dict] = []
    for key, (name, country, category) in ECB_EXR_KEYS.items():
        url = ECB_BASE_URL.format(dataflow="EXR", key=key)
        response = requests.get(url, params={"startPeriod": start_date, "format": "csvdata"}, timeout=timeout)
        response.raise_for_status()
        df = pd.read_csv(StringIO(response.text))
        for item in df.to_dict(orient="records"):
            period = _period_to_date(item.get("TIME_PERIOD"))
            value = item.get("OBS_VALUE")
            if period is None or pd.isna(value):
                continue
            rows.append(
                _row(
                    date_value=period,
                    series_id=f"ECB:EXR:{key}",
                    series_name=item.get("TITLE") or name,
                    source="ECB",
                    country=country,
                    region="Eurozone",
                    category=category,
                    frequency="daily",
                    value=value,
                    unit=item.get("UNIT") or "exchange rate",
                    raw_payload=item,
                )
            )
    return rows


def fetch_abs_rows(*, last_n_observations: int = 2, timeout: int = 60) -> list[dict]:
    rows: list[dict] = []
    for dataflow, (flow_name, category) in ABS_DATAFLOWS.items():
        url = ABS_BASE_URL.format(dataflow=dataflow)
        try:
            response = requests.get(
                url,
                params={"lastNObservations": str(last_n_observations), "format": "csv"},
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
        except Exception as exc:
            print(f"[WARN] ABS skipped {dataflow}: {exc}", flush=True)
            continue
        df = pd.read_csv(StringIO(response.text))
        if "TIME_PERIOD" not in df.columns or "OBS_VALUE" not in df.columns:
            continue
        dimension_cols = [
            col for col in df.columns
            if col not in {
                "DATAFLOW",
                "TIME_PERIOD",
                "OBS_VALUE",
                "OBS_STATUS",
                "OBS_COMMENT",
                "DECIMALS",
                "BASE_PERIOD",
            }
            and not col.startswith("UNIT")
        ]
        for item in df.to_dict(orient="records"):
            period = _period_to_date(item.get("TIME_PERIOD"))
            value = item.get("OBS_VALUE")
            if period is None or pd.isna(value):
                continue
            dimensions = ".".join(str(item.get(col, "")) for col in dimension_cols)
            freq = str(item.get("FREQ") or "").lower() or "unknown"
            rows.append(
                _row(
                    date_value=period,
                    series_id=f"ABS:{dataflow}:{dimensions}",
                    series_name=f"{flow_name} {dimensions}".strip(),
                    source="ABS",
                    country="AUS",
                    region="Australia",
                    category=category,
                    frequency={"m": "monthly", "q": "quarterly", "a": "annual"}.get(freq, freq),
                    value=value,
                    unit=item.get("UNIT_MEASURE") or "",
                    raw_payload=item,
                )
            )
    return rows


def fetch_global_economic_rows(*, start_date: str = "2026-04-01", providers: set[str] | None = None) -> dict[str, list[dict]]:
    providers = providers or {"world_bank", "ecb", "abs"}
    start_year = int(start_date[:4]) - 1
    result: dict[str, list[dict]] = {}
    if "world_bank" in providers:
        result["world_bank"] = fetch_world_bank_rows(start_year=start_year)
    if "ecb" in providers:
        result["ecb"] = fetch_ecb_rows(start_date=start_date)
    if "abs" in providers:
        result["abs"] = fetch_abs_rows()
    return result


def rows_to_json_preview(rows: list[dict], limit: int = 5) -> str:
    return json.dumps(rows[:limit], indent=2, default=str)
