"""Database-level security: the actual read-only role, RLS, pooled-connection tenant isolation."""

import uuid

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.db.analytics import MissingTenantContext, analytics_engine, run_source_query
from app.db.session import psycopg_conninfo
from app.seed.synthetic import tenant_uuid

ACME = tenant_uuid("acme-retail")
BHARAT = tenant_uuid("bharat-bazaar")
COUNT = "SELECT count(*) AS n, count(DISTINCT tenant_id) AS tenants FROM semantic.order_facts"


def reader_conn(settings) -> psycopg.Connection:  # noqa: ANN001
    return psycopg.connect(psycopg_conninfo(settings.analytics_database_url), autocommit=True)


def test_reader_role_attributes(settings, demo_tenants) -> None:  # noqa: ANN001
    with reader_conn(settings) as c:
        row = c.execute("SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user").fetchone()
        assert row == (False, False, False, False)
        owner = c.execute("SELECT tableowner FROM pg_tables WHERE schemaname='source' AND tablename='orders'").fetchone()[0]
        assert owner != make_url(settings.analytics_database_url).username


def test_reader_cannot_read_raw_source_tables(settings, demo_tenants) -> None:  # noqa: ANN001
    with reader_conn(settings) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("SELECT * FROM source.orders LIMIT 1")


@pytest.mark.parametrize("stmt", [
    "INSERT INTO source.orders (tenant_id, order_id, source_id, customer_id, status, channel, region, currency, batch_id, ingested_at) "
    f"VALUES ('{ACME}', 99999999, 'x', 1, 'completed', 'web', 'North', 'INR', 1, now())",
    "UPDATE source.orders SET discount_amount = 0",
    "DELETE FROM source.refunds",
    "DELETE FROM semantic.order_facts",
    "CREATE TABLE semantic.evil (x int)",
    "CREATE FUNCTION semantic.f() RETURNS int AS 'select 1' LANGUAGE sql",
])
def test_source_writes_are_denied_even_outside_read_only_transactions(settings, demo_tenants, stmt: str) -> None:  # noqa: ANN001
    with reader_conn(settings) as c:
        c.execute("SET default_transaction_read_only = off")  # prove permissions, not just read-only mode, deny it
        with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.ObjectNotInPrerequisiteState,
                            psycopg.errors.WrongObjectType, psycopg.errors.ReadOnlySqlTransaction)):
            c.execute(stmt)


def test_missing_tenant_context_denies_rows(settings, demo_tenants) -> None:  # noqa: ANN001
    with reader_conn(settings) as c:
        assert c.execute(COUNT).fetchone()[0] == 0
        c.execute("SELECT set_config('app.tenant_id', '', false)")
        assert c.execute(COUNT).fetchone()[0] == 0


def test_application_refuses_queries_without_tenant() -> None:
    with pytest.raises(MissingTenantContext):
        run_source_query(None, COUNT)
    with pytest.raises(MissingTenantContext):
        run_source_query("", COUNT)


def test_each_tenant_sees_only_its_rows(demo_tenants) -> None:  # noqa: ANN001
    a = run_source_query(ACME, COUNT).rows[0]
    b = run_source_query(BHARAT, COUNT).rows[0]
    assert a["tenants"] == 1 and b["tenants"] == 1 and a["n"] != b["n"]
    leak = run_source_query(ACME, "SELECT count(*) AS n FROM semantic.order_facts WHERE tenant_id = :t", {"t": str(BHARAT)})
    assert leak.rows[0]["n"] == 0  # even an explicit predicate for another tenant returns nothing


def test_tenant_context_does_not_leak_through_pooled_connections(settings, demo_tenants) -> None:  # noqa: ANN001
    eng = create_engine(settings.analytics_database_url, pool_size=1, max_overflow=0)
    try:
        run_source_query(ACME, COUNT, engine=eng)
        with eng.connect() as conn:  # same physical connection, new transaction, no tenant set
            pid1 = conn.execute(text("SELECT pg_backend_pid()")).scalar()
            assert conn.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar() in (None, "")
            assert conn.execute(text(COUNT)).first()[0] == 0
        res = run_source_query(BHARAT, "SELECT count(DISTINCT tenant_id) AS t, min(tenant_id::text) AS id, pg_backend_pid() AS pid "
                                       "FROM semantic.order_facts", engine=eng)
        assert res.rows[0]["id"] == str(BHARAT) and res.rows[0]["pid"] == pid1
    finally:
        eng.dispose()


def test_statement_timeout_and_row_limit(demo_tenants) -> None:  # noqa: ANN001
    res = run_source_query(ACME, "SELECT order_id FROM semantic.order_facts", max_rows=5)
    assert res.row_count == 5 and res.truncated
    with pytest.raises(Exception, match="(?i)cancel|timeout"):
        run_source_query(ACME, "SELECT pg_sleep(2)", timeout_ms=200)


def test_invalid_tenant_id_format_rejected() -> None:
    with pytest.raises(ValueError):
        run_source_query("acme' OR '1'='1", COUNT)


def test_analytics_engine_uses_reader_role(settings) -> None:  # noqa: ANN001
    assert analytics_engine().url.username == make_url(settings.analytics_database_url).username
    assert uuid.UUID(str(ACME))
