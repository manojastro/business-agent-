"""Read-only, tenant-scoped access to the analytics (source) database.

Every query runs in its own transaction that:
  1. is READ ONLY,
  2. sets ``app.tenant_id`` with ``set_config(..., is_local => true)`` (transaction scoped, so it
     cannot leak to the next user of a pooled connection),
  3. sets a transaction-local statement timeout,
and then fetches at most ``max_rows + 1`` rows and enforces a result-byte limit.

The connecting role is a non-owner, non-superuser, NOBYPASSRLS role that can only SELECT the
approved ``semantic`` views. Row-level security in the database is the primary control; the
compiled queries also carry an explicit ``tenant_id = :tenant_id`` predicate (defense in depth).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.sql import Executable

from app.config import get_settings


class SourceAccessError(RuntimeError):
    code = "source_access_error"


class MissingTenantContext(SourceAccessError):
    code = "missing_tenant_context"


class ResultTooLarge(SourceAccessError):
    code = "result_too_large"


@dataclass
class SourceResult:
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    execution_ms: int
    result_bytes: int


@lru_cache
def analytics_engine() -> Engine:
    s = get_settings()
    return create_engine(
        s.analytics_database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={"application_name": "metric-investigator-source-reader"},
    )


def jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    return v


def run_source_query(
    tenant_id: uuid.UUID | str | None,
    statement: Executable | str,
    params: dict[str, Any] | None = None,
    *,
    max_rows: int | None = None,
    max_bytes: int | None = None,
    timeout_ms: int | None = None,
    engine: Engine | None = None,
) -> SourceResult:
    if tenant_id is None or str(tenant_id).strip() == "":
        raise MissingTenantContext("A tenant context is required for every source query")
    tenant = str(uuid.UUID(str(tenant_id)))  # validates format
    s = get_settings()
    max_rows = max_rows or s.source_max_rows
    max_bytes = max_bytes or s.source_max_result_bytes
    timeout_ms = timeout_ms or s.source_statement_timeout_ms
    stmt = text(statement) if isinstance(statement, str) else statement
    eng = engine or analytics_engine()
    started = time.perf_counter()
    with eng.connect() as conn:
        with conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            conn.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(int(timeout_ms))})
            result = conn.execute(stmt, params or {})
            raw = result.mappings().fetchmany(max_rows + 1)
    elapsed = int((time.perf_counter() - started) * 1000)
    truncated = len(raw) > max_rows
    rows = [{k: jsonable(v) for k, v in r.items()} for r in raw[:max_rows]]
    size = len(json.dumps(rows, separators=(",", ":")))
    if size > max_bytes:
        raise ResultTooLarge(f"Result of {size} bytes exceeds the {max_bytes}-byte limit")
    return SourceResult(rows=rows, row_count=len(rows), truncated=truncated, execution_ms=elapsed, result_bytes=size)
