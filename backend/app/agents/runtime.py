"""Per-run dependencies, event log, budgets and model-call accounting for graph nodes."""

from __future__ import annotations

import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from app.config import Settings, get_settings
from app.db.models import Investigation, InvestigationStep
from app.db.session import session_scope
from app.providers.base import BudgetExceeded, ModelProvider, ModelRequest, ModelResult, ProviderError


@dataclass
class Runtime:
    settings: Settings
    provider: ModelProvider
    auto_continue_incomplete: bool = False  # evaluation policy: label and continue instead of asking
    worker_id: str = "inline"
    extra: dict[str, Any] = field(default_factory=dict)


_runtime: ContextVar[Runtime | None] = ContextVar("investigation_runtime", default=None)


def set_runtime(rt: Runtime) -> object:
    return _runtime.set(rt)


def reset_runtime(token: object) -> None:
    _runtime.reset(token)  # type: ignore[arg-type]


def runtime() -> Runtime:
    rt = _runtime.get()
    if rt is None:
        from app.providers.factory import get_provider

        rt = Runtime(settings=get_settings(), provider=get_provider())
        _runtime.set(rt)
    return rt


def emit(
    investigation_id: str | uuid.UUID,
    tenant_id: str | uuid.UUID,
    node: str,
    kind: str,
    title: str,
    rationale: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    """Append an event in its own short transaction so live viewers see it immediately."""
    with session_scope() as s:
        s.add(
            InvestigationStep(
                investigation_id=uuid.UUID(str(investigation_id)),
                tenant_id=uuid.UUID(str(tenant_id)),
                node=node,
                kind=kind,
                title=title[:300],
                rationale=rationale[:2000],
                detail=detail or {},
            )
        )


def is_cancelled(investigation_id: str) -> bool:
    with session_scope() as s:
        inv = s.get(Investigation, uuid.UUID(investigation_id))
        return bool(inv and inv.cancel_requested)


def call_model(
    state: dict[str, Any],
    node: str,
    task: str,
    role: str,
    system: str,
    instructions: str,
    context: dict[str, Any],
    response_model: type[BaseModel],
) -> ModelResult:
    """Call the provider with cost ceiling, bounded retry of transient errors, and usage accounting."""
    rt = runtime()
    inv_id = state["investigation_id"]
    with session_scope() as s:
        inv = s.get(Investigation, uuid.UUID(inv_id))
        assert inv is not None
        if inv.cost_used >= inv.max_cost and inv.max_cost > 0 and not rt.provider.simulated:
            raise BudgetExceeded(f"model cost ceiling {inv.max_cost} reached")
    req = ModelRequest(task=task, role=role, system=system, instructions=instructions, context=context,  # type: ignore[arg-type]
                       response_model=response_model)
    attempts = rt.settings.model_max_retries + 1
    last: ProviderError | None = None
    for i in range(attempts):
        try:
            result = rt.provider.complete(req)
            break
        except ProviderError as exc:
            last = exc
            if not exc.transient or i == attempts - 1:
                emit(inv_id, state["tenant_id"], node, "model_error", f"Model call failed: {exc.code}",
                     detail={"task": task, "attempt": i + 1, "error": str(exc)[:300]})
                raise
            time.sleep(min(4.0, 0.25 * 2**i))
    else:  # pragma: no cover - loop always breaks or raises
        raise last or ProviderError("unknown")
    with session_scope() as s:
        inv = s.get(Investigation, uuid.UUID(inv_id))
        assert inv is not None
        inv.tokens_used += result.usage.total
        inv.cost_used = (inv.cost_used or Decimal("0")) + result.cost
    emit(
        inv_id,
        state["tenant_id"],
        node,
        "model_call",
        f"{role.title()} model call: {task}",
        detail={
            "task": task,
            "role": role,
            "provider": result.provider,
            "model": result.model,
            "simulated": result.simulated,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "tokens_estimated": result.usage.estimated,
            "cost": str(result.cost),
            "latency_ms": result.latency_ms,
            "attempts": result.attempts,
        },
    )
    return result
