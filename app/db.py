from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

DB_PATH = os.getenv("CRAWLER_DB_PATH", "/data/jobs.db")
LOG_DIR = os.getenv("CRAWLER_LOG_DIR", "/data/logs")
logger = logging.getLogger(__name__)

DEFAULT_DISCOVERY_CONFIG_JSON = (
    '{"enabled":true,"max_rounds":3,"max_actions":100,"max_llm_pages":25}'
)


def ensure_data_dirs() -> None:
    logger.debug("Ensuring data directories exist db_path=%s log_dir=%s", DB_PATH, LOG_DIR)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


@asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    logger.debug("Opening SQLite connection to %s", DB_PATH)
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()
        logger.debug("Closed SQLite connection to %s", DB_PATH)


async def init_db() -> None:
    ensure_data_dirs()
    logger.info("Initializing database schema")
    async with connect() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                target_url TEXT NOT NULL,
                scope_config TEXT,
                auth_config TEXT,
                discovery_config TEXT NOT NULL DEFAULT
                    '{"enabled":true,"max_rounds":3,"max_actions":100,"max_llm_pages":25}',
                error TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                generated_exclusions TEXT,
                baseline_sitemap TEXT,
                discovery_checkpoint_sitemap TEXT,
                discovery_checkpoint_progress TEXT,
                crawl_evidence TEXT,
                discovery_result TEXT,
                sitemap TEXT,
                result_entry_count INTEGER,
                result_size_bytes INTEGER
            );
            """
        )
        cursor = await conn.execute("PRAGMA table_info(jobs)")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        required_columns = {
            "job_id",
            "status",
            "target_url",
            "scope_config",
            "auth_config",
            "discovery_config",
            "error",
            "created_at",
            "finished_at",
            "generated_exclusions",
            "baseline_sitemap",
            "discovery_checkpoint_sitemap",
            "discovery_checkpoint_progress",
            "crawl_evidence",
            "discovery_result",
            "sitemap",
            "result_entry_count",
            "result_size_bytes",
        }
        missing_columns = sorted(required_columns - columns)
        if missing_columns:
            raise RuntimeError(
                "Incompatible jobs database schema; recreate the database. Missing columns: "
                + ", ".join(missing_columns)
            )
        await conn.commit()
    logger.info("Database schema initialization complete")


def dumps_json(data: Any | None) -> str | None:
    if data is None:
        return None
    return json.dumps(data)


def loads_json(data: str | None) -> dict[str, Any] | None:
    if data is None:
        return None
    return json.loads(data)


async def fetch_one(query: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
    logger.debug("Executing fetch_one query")
    async with connect() as conn:
        cursor = await conn.execute(query, params)
        row = await cursor.fetchone()
        await cursor.close()
        return row


async def fetch_all(query: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
    logger.debug("Executing fetch_all query")
    async with connect() as conn:
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)


async def execute(query: str, params: Iterable[Any] = ()) -> None:
    logger.debug("Executing write query")
    async with connect() as conn:
        await conn.execute(query, params)
        await conn.commit()


async def execute_rowcount(query: str, params: Iterable[Any] = ()) -> int:
    logger.debug("Executing write query with row count")
    async with connect() as conn:
        cursor = await conn.execute(query, params)
        await conn.commit()
        rowcount = cursor.rowcount
        await cursor.close()
        return rowcount
