"""Container health check: the database is reachable and some worker beat in the last 60s."""

import sys

from sqlalchemy import text

from app.db.session import app_engine

try:
    with app_engine().connect() as c:
        ok = c.execute(text("SELECT count(*) FROM worker_heartbeats WHERE last_seen_at > now() - interval '60 seconds'")).scalar()
    sys.exit(0 if ok else 1)
except Exception:  # noqa: BLE001
    sys.exit(1)
