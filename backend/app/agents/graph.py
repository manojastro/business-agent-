"""Bounded investigation graph with durable Postgres checkpoints.

Routing: every node sets ``next_action``; a single router maps it to the next node. Interrupts
(clarification, reviewer decision) pause the graph; the worker resumes it with
``Command(resume=...)``. After a crash the worker re-invokes the same thread (investigation id)
and LangGraph continues from the last persisted checkpoint.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TypedDict

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from psycopg.rows import dict_row

from app.agents import nodes as N
from app.config import get_settings
from app.db.session import psycopg_conninfo


class InvestigationState(TypedDict, total=False):
    investigation_id: str
    tenant_id: str
    next_action: str
    pending: dict[str, Any] | None
    metric: dict[str, Any]
    catalog: dict[str, Any]
    windows: dict[str, Any]
    timezone: str
    as_of: str
    refund_watermark: str
    freshness: dict[str, Any]
    quality: dict[str, Any]
    incomplete_data: bool
    headline: dict[str, Any]
    components: dict[str, Any] | None
    price_volume: dict[str, Any] | None
    noise_band: dict[str, Any]
    totals_evidence_id: str
    results: dict[str, Any]
    campaign_spend: dict[str, Any] | None
    pending_plans: list[dict[str, Any]]
    round: int
    budget_exhausted: str | None
    summary: str
    driver: dict[str, Any]
    next_investigations: list[str]
    critic_findings: list[dict[str, Any]]
    report_version: int
    review_comment: str
    error: dict[str, Any]


NODES = {
    "intake": N.intake,
    "clarify": N.clarify,
    "freshness": N.freshness,
    "baseline": N.baseline,
    "plan": N.plan,
    "query": N.query,
    "revise": N.revise,
    "draft": N.draft,
    "critic": N.critic,
    "assemble": N.assemble,
    "review": N.review,
    "revise_report": N.revise_report,
    "published": N.published,
    "rejected": N.rejected,
    "cancelled": N.cancelled,
    "failed": N.failed,
    "stopped": N.stopped,
}
TERMINAL_NODES = ("published", "rejected", "cancelled", "failed", "stopped")


def _route(state: InvestigationState) -> str:
    nxt = state.get("next_action", "failed")
    if nxt == "end":
        return END
    return nxt if nxt in NODES else "failed"


def build_graph() -> StateGraph:
    g = StateGraph(InvestigationState)
    for name, fn in NODES.items():
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    targets = {name: name for name in NODES} | {END: END}
    for name in NODES:
        if name in TERMINAL_NODES:
            g.add_edge(name, END)
        else:
            g.add_conditional_edges(name, _route, targets)
    return g


@contextmanager
def checkpointer() -> Iterator[PostgresSaver]:
    conninfo = psycopg_conninfo(get_settings().app_database_url)
    with psycopg.connect(conninfo, autocommit=True, prepare_threshold=0, row_factory=dict_row) as conn:
        yield PostgresSaver(conn)


def setup_checkpointer() -> None:
    with checkpointer() as saver:
        saver.setup()


def thread_config(investigation_id: str | uuid.UUID) -> dict[str, Any]:
    return {"configurable": {"thread_id": str(investigation_id)}, "recursion_limit": 80}


def advance(investigation_id: str, tenant_id: str, resume: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start, continue or resume an investigation. Safe to call again after a crash."""
    with checkpointer() as saver:
        graph = build_graph().compile(checkpointer=saver)
        config = thread_config(investigation_id)
        snap = graph.get_state(config)
        if not snap.values:
            graph.invoke({"investigation_id": investigation_id, "tenant_id": tenant_id}, config, durability="sync")
        elif snap.values.get("tenant_id") != tenant_id:
            raise PermissionError("checkpoint belongs to a different tenant")
        elif snap.interrupts and resume is not None:
            graph.invoke(Command(resume=resume), config, durability="sync")
        elif snap.next and not snap.interrupts:
            graph.invoke(None, config, durability="sync")  # resume after interruption of the process
        snap = graph.get_state(config)
        return {
            "next": list(snap.next),
            "interrupts": [i.value for i in snap.interrupts],
            "values": {k: snap.values.get(k) for k in ("next_action", "report_version", "driver")},
        }
