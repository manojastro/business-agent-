"""Durable worker and graph behaviour: duplicate delivery, leases, timeouts, resume, cancellation,
clarification, freshness failure, the planted recommendation and the reviewer loop."""

import uuid
from datetime import date

from sqlalchemy import select, text

from app.agents.graph import advance, checkpointer, thread_config
from app.agents.runtime import Runtime, reset_runtime, set_runtime
from app.db.models import Claim, EvidenceItem, Investigation, InvestigationStep, Job, QueryRun
from app.db.session import new_session, session_scope
from app.providers.fixture import FixtureProvider
from app.workers import queue
from app.workers.main import Worker


def _run(inv_id: str, tenant_id: str, provider: FixtureProvider | None = None, resume: dict | None = None,
         settings=None, auto_continue: bool = False) -> dict:  # noqa: ANN001
    from app.config import get_settings

    tok = set_runtime(Runtime(settings=settings or get_settings(), provider=provider or FixtureProvider(),
                              auto_continue_incomplete=auto_continue))
    try:
        return advance(inv_id, tenant_id, resume)
    finally:
        reset_runtime(tok)


def _inv(inv_id: str) -> Investigation:
    with session_scope() as s:
        inv = s.get(Investigation, uuid.UUID(inv_id))
        s.expunge(inv)
        return inv


def _kinds(inv_id: str) -> list[str]:
    with session_scope() as s:
        return [r.kind for r in s.scalars(select(InvestigationStep).where(
            InvestigationStep.investigation_id == uuid.UUID(inv_id)).order_by(InvestigationStep.id))]


def test_duplicate_enqueue_creates_one_job_and_single_claim(new_investigation) -> None:  # noqa: ANN001
    inv_id, tid = new_investigation()
    op = f"run:{inv_id}"
    with session_scope() as s:
        assert queue.enqueue(s, tenant_id=uuid.UUID(tid), kind="investigate", investigation_id=uuid.UUID(inv_id), operation_id=op)
        assert not queue.enqueue(s, tenant_id=uuid.UUID(tid), kind="investigate", investigation_id=uuid.UUID(inv_id), operation_id=op)
        assert s.scalar(text("SELECT count(*) FROM jobs WHERE operation_id = :op"), {"op": op}) == 1
    s1, s2 = new_session(), new_session()
    try:
        j1 = queue.claim(s1, "w1", 30)
        j2 = queue.claim(s2, "w2", 30)
        claimed = [j for j in (j1, j2) if j and str(j.investigation_id) == inv_id]
        assert len(claimed) == 1  # a duplicate worker cannot take the same job
        job = claimed[0]
        # fencing: a stale token cannot complete the job
        assert not queue.complete(s1, job.id, uuid.uuid4())
        assert queue.complete(s1, job.id, job.lease_token)
    finally:
        s1.close()
        s2.close()


def test_expired_lease_is_reclaimed_by_another_worker(new_investigation) -> None:  # noqa: ANN001
    inv_id, tid = new_investigation()
    with session_scope() as s:
        queue.enqueue(s, tenant_id=uuid.UUID(tid), kind="investigate", investigation_id=uuid.UUID(inv_id), operation_id=f"run:{inv_id}")
    s = new_session()
    try:
        first = queue.claim(s, "crashed-worker", 30)
        while first and str(first.investigation_id) != inv_id:  # skip unrelated leftovers
            queue.complete(s, first.id, first.lease_token)
            first = queue.claim(s, "crashed-worker", 30)
        assert first is not None
        # simulate the crashed worker's lease running out
        s.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"), {"id": first.id})
        s.commit()
        second = queue.claim(s, "healthy-worker", 30)
        assert second is not None and second.id == first.id and second.lease_token != first.lease_token
        assert second.attempts == first.attempts + 1
        assert not queue.heartbeat(s, first.id, first.lease_token, 30)  # old owner has lost the lease
        queue.complete(s, second.id, second.lease_token)
    finally:
        s.close()


def test_model_timeout_is_retried_then_job_fails_cleanly(new_investigation, settings) -> None:  # noqa: ANN001
    inv_id, tid = new_investigation()
    with session_scope() as s:
        queue.enqueue(s, tenant_id=uuid.UUID(tid), kind="investigate", investigation_id=uuid.UUID(inv_id),
                      operation_id=f"run:{inv_id}", max_attempts=2)
    w = Worker(worker_id="timeout-test")

    import app.workers.main as wm

    original = wm.get_provider
    wm.get_provider = lambda *_a, **_k: FixtureProvider(fail_tasks={"propose"})
    try:
        for _ in range(2):
            with session_scope() as s:
                s.execute(text("UPDATE jobs SET run_after = now() WHERE operation_id = :op"), {"op": f"run:{inv_id}"})
            w.run_until_idle(max_jobs=5)
    finally:
        wm.get_provider = original
    with session_scope() as s:
        job = s.scalar(select(Job).where(Job.operation_id == f"run:{inv_id}"))
        assert job is not None and job.status == "failed" and job.attempts == 2
        assert "transient" in (job.last_error or "") or "timeout" in (job.last_error or "").lower()
    assert _inv(inv_id).status == "failed"
    assert "model_error" in _kinds(inv_id)


def test_resume_after_crash_continues_from_checkpoint_and_reuses_evidence(new_investigation) -> None:  # noqa: ANN001
    inv_id, tid = new_investigation()

    class Crash(FixtureProvider):
        def complete(self, request):  # noqa: ANN001, ANN201
            if request.task == "draft":
                raise KeyboardInterrupt("simulated process termination")
            return super().complete(request)

    try:
        _run(inv_id, tid, Crash())
        raise AssertionError("expected simulated crash")
    except KeyboardInterrupt:
        pass
    before = _inv(inv_id)
    with session_scope() as s:
        runs_before = s.scalar(select(text("count(*)")).select_from(QueryRun).where(QueryRun.investigation_id == uuid.UUID(inv_id)))
    assert before.queries_used >= 6
    with checkpointer() as saver:
        assert saver.get_tuple(thread_config(inv_id)) is not None
    out = _run(inv_id, tid)  # same investigation id, healthy provider
    after = _inv(inv_id)
    assert out["interrupts"] and out["interrupts"][0]["type"] == "review"
    assert after.queries_used == before.queries_used  # no query re-executed
    with session_scope() as s:
        runs_after = s.scalar(select(text("count(*)")).select_from(QueryRun).where(QueryRun.investigation_id == uuid.UUID(inv_id)))
    assert runs_after == runs_before


def test_planted_recommendation_rejected_and_review_loop(new_investigation) -> None:  # noqa: ANN001
    inv_id, tid = new_investigation("acme-retail")
    out = _run(inv_id, tid)
    assert out["interrupts"][0]["type"] == "review"
    with session_scope() as s:
        planted = s.scalar(select(Claim).where(Claim.investigation_id == uuid.UUID(inv_id), Claim.origin == "planted"))
        assert planted is not None and planted.verification_status == "rejected"
        codes = {r["code"] for r in planted.verification_detail["reasons"]}
        assert {"numeric_mismatch", "unsupported_causality", "critic_unsupported_causality"} <= codes
        verified = s.scalars(select(Claim).where(Claim.investigation_id == uuid.UUID(inv_id),
                                                 Claim.verification_status == "verified")).all()
        assert any(c.claim_key == "headline_change" for c in verified)
    assert "claim_rejected" in _kinds(inv_id)
    assert _inv(inv_id).primary_driver["label"] == "component:refunds"
    _run(inv_id, tid, resume={"decision": "request_changes", "comment": "Mention refund maturity in the summary."})
    _run(inv_id, tid, resume={"decision": "approve"})
    assert _inv(inv_id).status == "completed"


def test_cancellation_while_awaiting_review(new_investigation) -> None:  # noqa: ANN001
    inv_id, tid = new_investigation("bharat-bazaar")
    _run(inv_id, tid)
    assert _inv(inv_id).status == "awaiting_review"
    with session_scope() as s:
        s.get(Investigation, uuid.UUID(inv_id)).cancel_requested = True
    _run(inv_id, tid, resume={"decision": "cancel"})
    assert _inv(inv_id).status == "cancelled"


def test_ambiguous_metric_requires_clarification(new_investigation) -> None:  # noqa: ANN001
    inv_id, tid = new_investigation(metric_key=None, question="Why did revenue drop last week?")
    out = _run(inv_id, tid)
    assert out["interrupts"][0]["type"] == "metric"
    inv = _inv(inv_id)
    assert inv.status == "awaiting_clarification" and inv.metric_key is None
    out = _run(inv_id, tid, resume={"metric_key": "net_sales"})
    assert out["interrupts"][0]["type"] == "review"
    assert _inv(inv_id).metric_key == "net_sales"


def test_source_freshness_failure_changes_the_answer(new_investigation) -> None:  # noqa: ANN001
    inv_id, tid = new_investigation(as_of=date(2026, 9, 4))  # data ends 2026-08-31 local
    out = _run(inv_id, tid)
    assert out["interrupts"][0]["type"] == "incomplete_data"
    assert _inv(inv_id).status == "awaiting_clarification"
    out = _run(inv_id, tid, resume={"choice": "continue"})
    assert out["interrupts"][0]["type"] == "review"
    with session_scope() as s:
        from app.db.models import Report, ReportVersion

        rep = s.scalar(select(Report).where(Report.investigation_id == uuid.UUID(inv_id)))
        rv = s.scalar(select(ReportVersion).where(ReportVersion.report_id == rep.id))
        assert rv.content["limitations"][0]["key"] == "incomplete_data_label"
        assert rv.content["primary_driver"]["label"] == "data_quality:missing_batch"


def test_freshness_stop_choice_ends_without_report(new_investigation) -> None:  # noqa: ANN001
    inv_id, tid = new_investigation(as_of=date(2026, 9, 4))
    _run(inv_id, tid)
    _run(inv_id, tid, resume={"choice": "stop"})
    assert _inv(inv_id).status == "insufficient_evidence"


def test_evidence_items_are_append_only(new_investigation) -> None:  # noqa: ANN001
    import pytest
    from sqlalchemy.exc import DBAPIError

    inv_id, tid = new_investigation("bharat-bazaar")
    _run(inv_id, tid)
    with pytest.raises(DBAPIError):
        with session_scope() as s:
            ev = s.scalar(select(EvidenceItem).where(EvidenceItem.investigation_id == uuid.UUID(inv_id)))
            ev.label = "tampered"
