"""ABS SDMX metadata fetching and series decoding."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pandas as pd
import requests
from sqlalchemy import text


ABS_STRUCTURE_URL = "https://data.api.abs.gov.au/rest/datastructure/ABS/{dataflow}/{version}"
DEFAULT_ABS_DATAFLOWS = ("CPI", "LF", "ANA_AGG", "RT", "ITGS", "BOP", "PPI_FD", "WPI")


def create_abs_metadata_tables(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS public.abs_dataflow_dimensions (
                    dataflow text NOT NULL,
                    version text NOT NULL,
                    position integer NOT NULL,
                    dimension_id text NOT NULL,
                    codelist_id text,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (dataflow, version, dimension_id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS public.abs_codelists (
                    codelist_id text NOT NULL,
                    code text NOT NULL,
                    label text NOT NULL,
                    parent_code text,
                    raw_payload jsonb,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (codelist_id, code)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS public.abs_series_mappings (
                    series_id text PRIMARY KEY,
                    dataflow text NOT NULL,
                    decoded_name text NOT NULL,
                    decoded_dimensions jsonb NOT NULL,
                    obs_count integer NOT NULL,
                    min_date date,
                    max_date date,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
        )


def fetch_abs_structure(dataflow: str, *, version: str = "latest", timeout: int = 60) -> dict:
    response = requests.get(
        ABS_STRUCTURE_URL.format(dataflow=dataflow, version=version),
        params={"references": "all", "format": "json"},
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    return response.json()["data"]


def _codelist_id_from_urn(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"Codelist=ABS:([^(]+)", value)
    return match.group(1) if match else value


def parse_abs_structure(dataflow: str, payload: dict) -> tuple[list[dict], list[dict]]:
    structures = payload.get("dataStructures") or []
    if not structures:
        return [], []
    structure = structures[0]
    version = structure.get("version", "")
    dimensions = []
    for dim in (structure.get("dataStructureComponents") or {}).get("dimensionList", {}).get("dimensions", []):
        dimensions.append(
            {
                "dataflow": dataflow,
                "version": version,
                "position": dim.get("position"),
                "dimension_id": dim.get("id"),
                "codelist_id": _codelist_id_from_urn((dim.get("localRepresentation") or {}).get("enumeration")),
            }
        )
    codes = []
    for codelist in payload.get("codelists") or []:
        codelist_id = codelist.get("id")
        for code in codelist.get("codes") or []:
            codes.append(
                {
                    "codelist_id": codelist_id,
                    "code": str(code.get("id")),
                    "label": code.get("name") or (code.get("names") or {}).get("en") or str(code.get("id")),
                    "parent_code": code.get("parent"),
                    "raw_payload": json.dumps(code, default=str, allow_nan=False),
                }
            )
    return dimensions, codes


def upsert_abs_metadata(engine, dimensions: list[dict], codes: list[dict]) -> tuple[int, int]:
    create_abs_metadata_tables(engine)
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        if dimensions:
            conn.execute(
                text(
                    """
                    INSERT INTO public.abs_dataflow_dimensions (
                        dataflow, version, position, dimension_id, codelist_id, updated_at
                    )
                    VALUES (
                        :dataflow, :version, :position, :dimension_id, :codelist_id, :updated_at
                    )
                    ON CONFLICT (dataflow, version, dimension_id)
                    DO UPDATE SET
                        position = EXCLUDED.position,
                        codelist_id = EXCLUDED.codelist_id,
                        updated_at = now()
                    """
                ),
                [{**row, "updated_at": now} for row in dimensions],
            )
        if codes:
            conn.execute(
                text(
                    """
                    INSERT INTO public.abs_codelists (
                        codelist_id, code, label, parent_code, raw_payload, updated_at
                    )
                    VALUES (
                        :codelist_id, :code, :label, :parent_code, CAST(:raw_payload AS jsonb), :updated_at
                    )
                    ON CONFLICT (codelist_id, code)
                    DO UPDATE SET
                        label = EXCLUDED.label,
                        parent_code = EXCLUDED.parent_code,
                        raw_payload = EXCLUDED.raw_payload,
                        updated_at = now()
                    """
                ),
                [{**row, "updated_at": now} for row in codes],
            )
    return len(dimensions), len(codes)


def fetch_and_store_abs_metadata(engine, *, dataflows: list[str] | None = None) -> dict[str, dict]:
    results = {}
    for dataflow in dataflows or list(DEFAULT_ABS_DATAFLOWS):
        payload = fetch_abs_structure(dataflow)
        dimensions, codes = parse_abs_structure(dataflow, payload)
        dim_count, code_count = upsert_abs_metadata(engine, dimensions, codes)
        results[dataflow] = {"dimensions": dim_count, "codes": code_count}
    return results


def decode_abs_series_id(engine, series_id: str) -> dict:
    if not series_id.startswith("ABS:"):
        return {}
    parts = series_id.split(":")
    if len(parts) < 3:
        return {}
    dataflow = parts[1]
    codes = parts[2].split(".")
    stored_row = pd.read_sql(
        text(
            """
            SELECT raw_payload
            FROM public.economic_indicators
            WHERE series_id = :series_id
            ORDER BY date DESC
            LIMIT 1
            """
        ),
        engine,
        params={"series_id": series_id},
    )
    raw_payload = stored_row.iloc[0]["raw_payload"] if not stored_row.empty else None
    if isinstance(raw_payload, str):
        raw_payload = json.loads(raw_payload)
    if raw_payload is None or not isinstance(raw_payload, dict):
        raw_payload = {}
    dims = pd.read_sql(
        text(
            """
            SELECT DISTINCT ON (dimension_id)
                dimension_id, position, codelist_id
            FROM public.abs_dataflow_dimensions
            WHERE dataflow = :dataflow
            ORDER BY dimension_id, version DESC
            """
        ),
        engine,
        params={"dataflow": dataflow},
    )
    if dims.empty:
        return {"dataflow": dataflow, "decoded_dimensions": []}
    dims = dims.sort_values("position")
    labels = {}
    codelist_ids = [row["codelist_id"] for row in dims.to_dict(orient="records") if row.get("codelist_id")]
    if raw_payload.get("BASE_PERIOD") is not None:
        codelist_ids.append("CL_BASE_PERIOD")
    if codelist_ids:
        label_df = pd.read_sql(
            text(
                """
                SELECT codelist_id, code, label
                FROM public.abs_codelists
                WHERE codelist_id = ANY(:codelist_ids)
                """
            ),
            engine,
            params={"codelist_ids": codelist_ids},
        )
        labels = {(row["codelist_id"], str(row["code"])): row["label"] for row in label_df.to_dict(orient="records")}
    decoded = []
    for idx, dim in enumerate(dims.to_dict(orient="records")):
        raw_value = raw_payload.get(dim["dimension_id"])
        value = str(raw_value) if raw_value is not None else (codes[idx] if idx < len(codes) else "")
        codelist_id = dim.get("codelist_id")
        decoded.append(
            {
                "dimension": dim["dimension_id"],
                "code": value,
                "label": labels.get((codelist_id, value), value),
                "codelist_id": codelist_id,
            }
        )
    if raw_payload.get("BASE_PERIOD") is not None:
        value = str(raw_payload["BASE_PERIOD"])
        decoded.append(
            {
                "dimension": "BASE_PERIOD",
                "code": value,
                "label": labels.get(("CL_BASE_PERIOD", value), value),
                "codelist_id": "CL_BASE_PERIOD",
            }
        )
    for extra_idx, value in enumerate(codes[len(decoded):], start=1):
        decoded.append(
            {
                "dimension": f"extra_code_{extra_idx}",
                "code": value,
                "label": value,
                "codelist_id": None,
            }
        )
    return {"dataflow": dataflow, "decoded_dimensions": decoded}


def decoded_abs_name(engine, series_id: str) -> str:
    decoded = decode_abs_series_id(engine, series_id)
    parts = [item["label"] for item in decoded.get("decoded_dimensions", []) if item.get("label")]
    return " | ".join(parts)


def _format_abs_readable_name(dataflow: str, decoded_dimensions: list[dict]) -> str:
    labels_by_dimension = {
        item.get("dimension"): item.get("label")
        for item in decoded_dimensions
        if item.get("dimension") and item.get("label")
    }
    flow_prefixes = {
        "CPI": "ABS CPI",
        "LF": "ABS Labour Force",
        "ANA_AGG": "ABS National Accounts",
        "RT": "ABS Retail Trade",
        "ITGS": "ABS International Trade in Goods",
        "BOP": "ABS Balance of Payments",
        "PPI_FD": "ABS Producer Price Index",
        "WPI": "ABS Wage Price Index",
    }
    prefix = flow_prefixes.get(dataflow, f"ABS {dataflow}")
    ordered_labels = []
    for dimension in (
        "MEASURE",
        "DATA_ITEM",
        "INDEX",
        "SEX",
        "AGE",
        "SECTOR",
        "INDUSTRY",
        "SOURCE",
        "DESTINATION",
        "TSEST",
        "REGION",
        "FREQ",
    ):
        label = labels_by_dimension.get(dimension)
        if label and label not in ordered_labels:
            ordered_labels.append(label)
    if not ordered_labels:
        ordered_labels = [
            item["label"]
            for item in decoded_dimensions
            if item.get("label") and not str(item["dimension"]).startswith("extra_code_")
        ]
    return " - ".join([prefix, *ordered_labels])


def build_abs_series_mappings(engine) -> list[dict]:
    create_abs_metadata_tables(engine)
    series = pd.read_sql(
        text(
            """
            SELECT
                series_id,
                split_part(series_id, ':', 2) AS dataflow,
                count(*)::integer AS obs_count,
                min(date) AS min_date,
                max(date) AS max_date
            FROM public.economic_indicators
            WHERE series_id LIKE 'ABS:%'
            GROUP BY series_id
            ORDER BY dataflow, series_id
            """
        ),
        engine,
    )
    mappings = []
    for row in series.to_dict(orient="records"):
        decoded = decode_abs_series_id(engine, row["series_id"])
        decoded_dimensions = decoded.get("decoded_dimensions", [])
        mappings.append(
            {
                "series_id": row["series_id"],
                "dataflow": row["dataflow"],
                "decoded_name": _format_abs_readable_name(row["dataflow"], decoded_dimensions),
                "decoded_dimensions": json.dumps(decoded_dimensions, default=str, allow_nan=False),
                "obs_count": row["obs_count"],
                "min_date": row["min_date"],
                "max_date": row["max_date"],
            }
        )
    return mappings


def upsert_abs_series_mappings(engine, mappings: list[dict], *, update_series_names: bool = False) -> int:
    create_abs_metadata_tables(engine)
    if not mappings:
        return 0
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO public.abs_series_mappings (
                    series_id,
                    dataflow,
                    decoded_name,
                    decoded_dimensions,
                    obs_count,
                    min_date,
                    max_date,
                    updated_at
                )
                VALUES (
                    :series_id,
                    :dataflow,
                    :decoded_name,
                    CAST(:decoded_dimensions AS jsonb),
                    :obs_count,
                    :min_date,
                    :max_date,
                    now()
                )
                ON CONFLICT (series_id)
                DO UPDATE SET
                    dataflow = EXCLUDED.dataflow,
                    decoded_name = EXCLUDED.decoded_name,
                    decoded_dimensions = EXCLUDED.decoded_dimensions,
                    obs_count = EXCLUDED.obs_count,
                    min_date = EXCLUDED.min_date,
                    max_date = EXCLUDED.max_date,
                    updated_at = now()
                """
            ),
            mappings,
        )
        if update_series_names:
            conn.execute(
                text(
                    """
                    UPDATE public.economic_indicators AS indicator
                    SET series_name = mapping.decoded_name,
                        updated_at = now()
                    FROM public.abs_series_mappings AS mapping
                    WHERE indicator.series_id = mapping.series_id
                      AND indicator.series_id LIKE 'ABS:%'
                      AND indicator.series_name IS DISTINCT FROM mapping.decoded_name
                    """
                )
            )
    return len(mappings)
