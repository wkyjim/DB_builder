"""Configuration helpers for DB Builder scripts.

All database credentials are loaded from environment variables. The scripts
intentionally fail fast when a required secret is missing instead of falling
back to hardcoded passwords.
"""

from __future__ import annotations

import os
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from db_builder.env_loader import load_external_env


load_external_env()


LOCAL_DB_NAME = "us_equities_historical"
LOCAL_DB_USER = "postgres"
LOCAL_DB_HOST = "127.0.0.1"
LOCAL_DB_PORT = "5432"
NEON_HOST = "ep-aged-moon-ao3o4z0j-pooler.c-2.ap-southeast-1.aws.neon.tech"
NEON_DB_NAME = "neondb"
NEON_DB_USER = "neondb_owner"

RAW_TABLE = "public.us_equities"
INDICATOR_TABLE = "public.us_equities_indicators"
MACRO_TABLE = "public.macro"


def _required_env(name: str, aliases: tuple[str, ...] = ()) -> str:
    value = os.getenv(name)
    if not value:
        for alias in aliases:
            value = os.getenv(alias)
            if value:
                break
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def local_database_url(driver: str = "postgresql") -> str:
    if os.getenv("LOCAL_DATABASE_URL"):
        return os.environ["LOCAL_DATABASE_URL"]

    password = quote_plus(_required_env("LOCAL_DB_PASSWORD", ("local_password",)))
    user = os.getenv("LOCAL_DB_USER", LOCAL_DB_USER)
    host = os.getenv("LOCAL_DB_HOST", LOCAL_DB_HOST)
    port = os.getenv("LOCAL_DB_PORT", LOCAL_DB_PORT)
    db_name = os.getenv("LOCAL_DB_NAME", LOCAL_DB_NAME)

    return f"{driver}://{user}:{password}@{host}:{port}/{db_name}"


def neon_database_url(driver: str = "postgresql") -> str:
    if os.getenv("NEON_DATABASE_URL"):
        return os.environ["NEON_DATABASE_URL"]

    password = quote_plus(_required_env("NEON_DB_PASSWORD", ("neon_password",)))
    user = os.getenv("NEON_DB_USER", NEON_DB_USER)
    host = os.getenv("NEON_DB_HOST", NEON_HOST)
    db_name = os.getenv("NEON_DB_NAME", NEON_DB_NAME)

    return (
        f"{driver}://{user}:{password}@{host}/{db_name}"
        "?sslmode=require&channel_binding=require"
    )


def local_engine(**kwargs):
    options = {"pool_pre_ping": True, "pool_recycle": 3600}
    options.update(kwargs)
    return create_engine(local_database_url(), **options)


def neon_engine(**kwargs):
    options = {"pool_pre_ping": True, "pool_recycle": 3600}
    options.update(kwargs)
    return create_engine(neon_database_url(), **options)
