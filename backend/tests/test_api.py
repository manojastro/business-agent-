"""API: authentication, CSRF, roles, tenant isolation at every lookup, idempotency, export authorization."""

import uuid

from fastapi.testclient import TestClient

from app.workers.main import Worker

Q = "Why did net sales fall in the last complete seven days compared with the preceding seven days?"


def test_login_failure_and_unauthenticated_access(client, users) -> None:  # noqa: ANN001
    from app.main import create_app

    anon = TestClient(create_app())
    assert anon.get("/api/v1/investigations").status_code == 401
    r = anon.post("/api/v1/auth/login", json={"email": users["t-analyst-a"], "password": "wrong-password-123"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"


def test_csrf_required_for_mutations(client, users) -> None:  # noqa: ANN001
    a = client(users["t-analyst-a"])
    token = a.headers.pop("X-CSRF-Token")
    r = a.post("/api/v1/investigations", json={"question": Q, "metric_key": "net_sales"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    a.headers["X-CSRF-Token"] = token


def test_create_is_idempotent_and_async(client, users) -> None:  # noqa: ANN001
    a = client(users["t-analyst-a"])
    key = f"idem-{uuid.uuid4()}"
    r1 = a.post("/api/v1/investigations", json={"question": Q, "metric_key": "net_sales"}, headers={"Idempotency-Key": key})
    r2 = a.post("/api/v1/investigations", json={"question": Q, "metric_key": "net_sales"}, headers={"Idempotency-Key": key})
    assert r1.status_code == r2.status_code == 202
    assert r1.json()["id"] == r2.json()["id"] and r1.json()["created"] and not r2.json()["created"]
    assert r1.headers["Location"].endswith(r1.json()["id"])


def test_reviewer_cannot_create_and_owner_cannot_approve(client, users) -> None:  # noqa: ANN001
    rev = client(users["t-reviewer-a"])
    assert rev.post("/api/v1/investigations", json={"question": Q}).status_code == 403
    a = client(users["t-analyst-a"])
    iid = a.post("/api/v1/investigations", json={"question": Q, "metric_key": "net_sales"}).json()["id"]
    Worker(worker_id="api-test").run_until_idle()
    r = a.post(f"/api/v1/investigations/{iid}/report/decision", json={"decision": "approve", "version": 1})
    assert r.status_code == 403
    admin = client(users["t-admin-a"])
    assert admin.post(f"/api/v1/investigations/{iid}/report/decision",
                      json={"decision": "request_changes", "comment": "", "version": 1}).json()["error"]["code"] == "comment_required"
    assert admin.post(f"/api/v1/investigations/{iid}/report/decision",
                      json={"decision": "approve", "version": 9}).json()["error"]["code"] == "stale_version"


def test_full_review_flow_export_and_tenant_isolation(client, users) -> None:  # noqa: ANN001
    a = client(users["t-analyst-a"])
    rev = client(users["t-reviewer-a"])
    other = client(users["t-analyst-b"])
    iid = a.post("/api/v1/investigations", json={"question": Q, "metric_key": "net_sales"}).json()["id"]
    Worker(worker_id="api-test").run_until_idle()
    inv = a.get(f"/api/v1/investigations/{iid}").json()
    assert inv["status"] == "awaiting_review" and inv["simulation_label"] == "Demo simulation"

    # export is refused before approval
    assert rev.get(f"/api/v1/investigations/{iid}/report/export?format=html").status_code == 409

    # request changes -> new version -> approve
    assert rev.post(f"/api/v1/investigations/{iid}/report/decision",
                    json={"decision": "request_changes", "comment": "Add the refund maturity caveat.", "version": 1}).status_code == 200
    Worker(worker_id="api-test").run_until_idle()
    report = rev.get(f"/api/v1/investigations/{iid}/report").json()
    assert report["current_version"] == 2 and report["content"]["revision_note"]
    assert rev.post(f"/api/v1/investigations/{iid}/report/decision", json={"decision": "approve", "version": 2}).status_code == 200
    Worker(worker_id="api-test").run_until_idle()
    assert a.get(f"/api/v1/investigations/{iid}").json()["status"] == "completed"
    html = rev.get(f"/api/v1/investigations/{iid}/report/export?format=html")
    pdf = rev.get(f"/api/v1/investigations/{iid}/report/export?format=pdf")
    assert html.status_code == 200 and "Demo simulation" in html.text and "Evidence index" in html.text
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"

    # another tenant cannot see anything about it
    ev_id = a.get(f"/api/v1/investigations/{iid}/evidence").json()["items"][0]["id"]
    for path in (f"/api/v1/investigations/{iid}", f"/api/v1/investigations/{iid}/steps", f"/api/v1/investigations/{iid}/evidence",
                 f"/api/v1/investigations/{iid}/report", f"/api/v1/investigations/{iid}/report/export?format=pdf",
                 f"/api/v1/evidence/{ev_id}", f"/api/v1/investigations/{iid}/claims"):
        assert other.get(path).status_code == 404, path
    assert other.post(f"/api/v1/investigations/{iid}/cancel").status_code == 404
    assert other.post(f"/api/v1/investigations/{iid}/rerun").status_code == 404

    # SQL visibility by role
    assert a.get(f"/api/v1/evidence/{ev_id}").json()["compiled_sql"]
    assert rev.get(f"/api/v1/evidence/{ev_id}").json()["compiled_sql"] is None


def test_switching_to_a_tenant_without_membership_is_denied(client, users) -> None:  # noqa: ANN001
    from app.seed.synthetic import tenant_uuid

    b = client(users["t-analyst-b"])
    r = b.post("/api/v1/auth/switch-tenant", json={"tenant_id": str(tenant_uuid("acme-retail"))})
    assert r.status_code == 404


def test_metric_definition_change_requires_admin_and_is_audited(client, users) -> None:  # noqa: ANN001
    a = client(users["t-analyst-a"])
    body = {"description": "Order count (test)", "change_reason": "test version bump"}
    assert a.post("/api/v1/metrics/order_count/versions", json=body).status_code == 403
    from app.seed.synthetic import tenant_uuid

    admin = client(users["t-admin-a"])  # catalog changes are made on the isolated test tenant
    assert admin.post("/api/v1/auth/switch-tenant", json={"tenant_id": str(tenant_uuid("test-small"))}).status_code == 200
    bad = admin.post("/api/v1/metrics/order_count/versions", json={**body, "allowed_dimensions": ["category"]})
    assert bad.status_code == 422
    r = admin.post("/api/v1/metrics/order_count/versions", json=body)
    assert r.status_code == 201 and r.json()["version"] >= 2
    audit = admin.get("/api/v1/admin/audit?action=metric_definition_changed").json()["items"]
    assert audit and audit[0]["detail"]["to_version"] == r.json()["version"]


def test_events_polling_fallback_and_sse_replay(client, users) -> None:  # noqa: ANN001
    a = client(users["t-analyst-a"])
    iid = a.post("/api/v1/investigations", json={"question": Q, "metric_key": "net_sales"}).json()["id"]
    Worker(worker_id="api-test").run_until_idle()
    steps = a.get(f"/api/v1/investigations/{iid}/steps").json()["items"]
    assert len(steps) > 5
    mid = steps[len(steps) // 2]["id"]
    rest = a.get(f"/api/v1/investigations/{iid}/steps?after={mid}").json()["items"]
    assert rest and all(s["id"] > mid for s in rest)
    # TestClient buffers the whole stream, so finish the run first (cancel -> terminal status ends the stream).
    assert a.post(f"/api/v1/investigations/{iid}/cancel").status_code == 200
    Worker(worker_id="api-test").run_until_idle()
    assert a.get(f"/api/v1/investigations/{iid}").json()["status"] == "cancelled"
    with a.stream("GET", f"/api/v1/investigations/{iid}/events", headers={"Last-Event-ID": str(mid)}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        ids = []
        for line in r.iter_lines():
            if line.startswith("id: "):
                ids.append(int(line[4:]))
            if len(ids) >= 3:
                break
        assert ids and ids[0] > mid


def test_validation_errors_are_structured(client, users) -> None:  # noqa: ANN001
    a = client(users["t-analyst-a"])
    r = a.post("/api/v1/investigations", json={"question": Q, "metric_key": "revenue"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = a.post("/api/v1/investigations", json={"question": Q, "baseline_window": {"start": "2026-08-01", "end": "2026-08-08"},
                                               "current_window": {"start": "2026-08-08", "end": "2026-08-14"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_windows"
