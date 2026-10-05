"""Deployment smoke check (standard library only).

Checks: login, source freshness, create one investigation, wait for review, evidence access,
reviewer approval, HTML and PDF export. Exits non-zero on the first failure.

Environment: BASE_URL (default http://localhost:8080), SMOKE_EMAIL (analyst), SMOKE_REVIEWER,
SMOKE_PASSWORD (shared demo password or set SMOKE_REVIEWER_PASSWORD), SMOKE_TIMEOUT (seconds, default 180).
"""

import http.cookiejar
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/")
TIMEOUT = int(os.environ.get("SMOKE_TIMEOUT", "180"))


class Client:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def call(self, method: str, path: str, body: dict | None = None, raw: bool = False):  # noqa: ANN201
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + "/api/v1" + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if method != "GET":
            req.add_header("X-CSRF-Token", self.csrf)
        with self.opener.open(req, timeout=30) as r:
            payload = r.read()
            return payload if raw else json.loads(payload or b"{}")

    def login(self, email: str, password: str) -> None:
        self.csrf = self.call("POST", "/auth/login", {"email": email, "password": password})["csrf_token"]


def step(name: str) -> None:
    print(f"[smoke] {name}", flush=True)


def main() -> int:
    pw = os.environ["SMOKE_PASSWORD"]
    a, r = Client(), Client()
    step("health")
    a.call("GET", "/health/ready")
    step("login analyst")
    a.login(os.environ.get("SMOKE_EMAIL", "analyst@acme.demo"), pw)
    step("source freshness")
    fr = a.call("GET", "/source/freshness")
    assert fr["watermark"], "no watermark"
    step("create investigation")
    inv = a.call("POST", "/investigations", {"question": "Why did net sales fall last week? (smoke)", "metric_key": "net_sales"})
    iid = inv["id"]
    deadline = time.time() + TIMEOUT
    status = ""
    while time.time() < deadline:
        status = a.call("GET", f"/investigations/{iid}")["status"]
        if status in ("awaiting_review", "failed", "cancelled"):
            break
        time.sleep(2)
    assert status == "awaiting_review", f"unexpected status {status}"
    step("evidence access")
    ev = a.call("GET", f"/investigations/{iid}/evidence")["items"]
    assert ev, "no evidence"
    a.call("GET", f"/evidence/{ev[0]['id']}")
    step("reviewer approval")
    r.login(os.environ.get("SMOKE_REVIEWER", "reviewer@acme.demo"), os.environ.get("SMOKE_REVIEWER_PASSWORD", pw))
    version = r.call("GET", f"/investigations/{iid}/report")["current_version"]
    r.call("POST", f"/investigations/{iid}/report/decision", {"decision": "approve", "version": version})
    while time.time() < deadline:
        if a.call("GET", f"/investigations/{iid}")["status"] == "completed":
            break
        time.sleep(2)
    step("export")
    html = r.call("GET", f"/investigations/{iid}/report/export?format=html", raw=True)
    pdf = r.call("GET", f"/investigations/{iid}/report/export?format=pdf", raw=True)
    assert b"Evidence index" in html and pdf[:4] == b"%PDF"
    step(f"OK - investigation {iid}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
