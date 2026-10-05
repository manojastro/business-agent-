import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, fmtDate, type EvidenceSummary, type Investigation, type Report, type Step } from "../api";
import EvidenceDrawer from "../components/EvidenceDrawer";
import ReportView from "../components/ReportView";
import { Badge, Button, Card, Empty, ErrorNote, Meter, SimulationBadge, Spinner, StatusBadge } from "../components/ui";
import { TERMINAL, useFetch, useLiveSteps } from "../hooks";

const KIND_TONE: Record<string, "gray" | "blue" | "green" | "amber" | "red" | "violet"> = {
  scope: "blue", freshness: "blue", baseline: "blue", hypotheses_proposed: "violet", hypothesis_revision: "violet",
  evidence: "green", plan_invalid: "amber", plan_repaired: "amber", plan_rejected: "red", claim_rejected: "red",
  planted_recommendation: "violet", critic_review: "violet", clarification_requested: "amber", awaiting_review: "violet",
  review_decision: "green", report: "green", error: "red", model_error: "red", budget: "amber", status: "gray", policy: "amber",
};

const REFRESH_ON = new Set(["scope", "freshness", "baseline", "hypotheses_proposed", "evidence", "hypothesis_revision", "report",
  "awaiting_review", "clarification_requested", "review_decision", "status", "error", "budget"]);

function Timeline({ steps, onOpen, showTools }: { steps: Step[]; onOpen: (id: string) => void; showTools: boolean }) {
  const visible = steps.filter((s) => showTools || (s.kind !== "tool_call" && s.kind !== "model_call"));
  if (!visible.length) return <Empty title="Waiting for the worker">The run is queued; events appear here as they happen.</Empty>;
  return (
    <ol className="relative space-y-3 border-l border-line pl-4" aria-live="polite">
      {visible.map((s) => {
        const ids = (s.detail?.evidence_ids as string[] | undefined) ?? [];
        const changes = (s.detail?.changes as { key: string; from: string; to: string; rationale: string }[] | undefined) ?? [];
        const reasons = (s.detail?.reasons as { code: string; detail: string }[] | undefined) ?? [];
        return (
          <li key={s.id} className="relative">
            <span className={`absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full ring-2 ring-white ${s.kind === "claim_rejected" ? "bg-red-600" : s.kind === "model_call" || s.kind === "tool_call" ? "bg-slate-300" : "bg-brand"}`} />
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-medium">{s.title}</span>
              <Badge tone={KIND_TONE[s.kind] ?? "gray"}>{s.node}</Badge>
              <span className="text-[11px] text-muted">{new Date(s.created_at).toLocaleTimeString()}</span>
            </div>
            {s.rationale && <p className="mt-0.5 text-xs text-muted">{s.rationale}</p>}
            {s.kind === "model_call" && (
              <p className="mt-0.5 text-[11px] text-muted">
                {String(s.detail.provider)} · {String(s.detail.model)} · {String(s.detail.input_tokens)}+{String(s.detail.output_tokens)} tokens
                {s.detail.tokens_estimated ? " (estimated)" : ""} · cost {String(s.detail.cost)} · {String(s.detail.latency_ms)} ms
              </p>
            )}
            {s.kind === "tool_call" && (
              <p className="mt-0.5 text-[11px] text-muted">{String(s.detail.tool)} · {String(s.detail.duration_ms)} ms · {s.detail.ok ? "ok" : "error"}</p>
            )}
            {changes.length > 0 && (
              <ul className="mt-1 space-y-0.5 text-xs">
                {changes.map((c) => <li key={c.key}><code>{c.key}</code>: {c.from} → <b>{c.to}</b> — <span className="text-muted">{c.rationale}</span></li>)}
              </ul>
            )}
            {s.kind === "claim_rejected" && (
              <div className="mt-1 rounded-lg bg-red-50 px-2 py-1.5 text-xs text-red-900">
                <p className="line-through">{String(s.detail.wording ?? "")}</p>
                <div className="mt-1 flex flex-wrap gap-1">{reasons.map((r, i) => <Badge key={i} tone="red" title={r.detail}>{r.code}</Badge>)}</div>
              </div>
            )}
            {ids.length > 0 && (
              <div className="mt-1 flex flex-wrap gap-2">
                {ids.map((e) => <button key={e} onClick={() => onOpen(e)} className="font-mono text-[11px] text-brand hover:underline">evidence {e.slice(0, 8)}</button>)}
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function Clarify({ inv, onDone }: { inv: Investigation; onDone: () => void }) {
  const q = inv.pending_question!;
  const [value, setValue] = useState(q.options[0]?.value ?? "");
  const [error, setError] = useState<unknown>(null);
  return (
    <Card title="Clarification needed" className="border-amber-300">
      <p className="text-sm">{q.question}</p>
      <fieldset className="mt-3 space-y-1">
        <legend className="sr-only">Options</legend>
        {q.options.map((o) => (
          <label key={o.value} className="flex items-center gap-2 text-sm">
            <input type="radio" name="clarify" value={o.value} checked={value === o.value} onChange={() => setValue(o.value)} /> {o.label}
          </label>
        ))}
      </fieldset>
      <ErrorNote error={error} />
      <Button className="mt-3" variant="primary" onClick={async () => {
        try {
          await api.post(`/investigations/${inv.id}/clarification`, q.type === "metric" ? { metric_key: value } : { choice: value });
          onDone();
        } catch (e) { setError(e); }
      }}>Submit</Button>
    </Card>
  );
}

function ReviewControls({ inv, report, onDone }: { inv: Investigation; report: Report; onDone: () => void }) {
  const [comment, setComment] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const decide = async (decision: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/investigations/${inv.id}/report/decision`, { decision, comment, version: report.current_version });
      setComment("");
      onDone();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return (
    <Card title={`Review version ${report.current_version}`} className="border-violet-300">
      <label htmlFor="cmt" className="text-sm font-medium">Comment (required for “request changes”)</label>
      <textarea id="cmt" rows={2} value={comment} onChange={(e) => setComment(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-3 py-2 text-sm" />
      <ErrorNote error={error} />
      <div className="mt-2 flex flex-wrap gap-2">
        <Button variant="primary" disabled={busy} onClick={() => decide("approve")}>Approve for publication</Button>
        <Button disabled={busy || !comment.trim()} onClick={() => decide("request_changes")}>Request changes</Button>
        <Button variant="danger" disabled={busy} onClick={() => decide("reject")}>Reject</Button>
      </div>
      <p className="mt-2 text-[11px] text-muted">Approval publishes exactly this version. The investigation owner cannot approve their own report.</p>
    </Card>
  );
}

export default function InvestigationDetail() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const inv = useFetch<Investigation>(`/investigations/${id}`, [id]);
  const [report, setReport] = useState<Report | null>(null);
  const [evidence, setEvidence] = useState<EvidenceSummary[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [showTools, setShowTools] = useState(false);
  const [tab, setTab] = useState<"progress" | "report" | "evidence">("progress");
  const [actionError, setActionError] = useState<unknown>(null);

  const refreshAll = useCallback(async () => {
    await inv.reload();
    api.get<{ items: EvidenceSummary[] }>(`/investigations/${id}/evidence`).then((r) => setEvidence(r.items)).catch(() => undefined);
    api.get<Report>(`/investigations/${id}/report`).then(setReport).catch(() => setReport(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const { steps, mode } = useLiveSteps(id, () => void refreshAll());
  useEffect(() => { void refreshAll(); }, [refreshAll]);
  useEffect(() => {
    const last = steps[steps.length - 1];
    if (last && REFRESH_ON.has(last.kind)) void refreshAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [steps.length]);
  useEffect(() => {
    if (report && inv.data && ["awaiting_review", "completed", "rejected"].includes(inv.data.status) && tab === "progress") setTab("report");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [report?.current_version, inv.data?.status]);

  if (inv.error) return <ErrorNote error={inv.error} />;
  const d = inv.data;
  if (!d) return <Spinner />;
  const running = !TERMINAL.has(d.status);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link to="/investigations" className="text-xs text-brand hover:underline">← Investigations</Link>
          <h1 className="mt-1 text-lg font-semibold">{d.question}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
            <StatusBadge status={d.status} />
            <SimulationBadge label={d.simulation_label} />
            {d.incomplete_data && <Badge tone="red">INCOMPLETE DATA</Badge>}
            <span>{d.metric_key ?? "metric pending"}{d.metric_version ? ` v${d.metric_version}` : ""}</span>
            {d.current_window && <span className="num">[{d.current_window.start}, {d.current_window.end}) vs [{d.baseline_window?.start}, {d.baseline_window?.end})</span>}
            <span>{d.timezone}</span>
            <span>watermark {fmtDate(d.source_watermark)}</span>
            <span>by {d.owner.display_name}</span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {d.permissions?.can_cancel && running && (
            <Button variant="danger" onClick={async () => {
              try { await api.post(`/investigations/${id}/cancel`); await refreshAll(); } catch (e) { setActionError(e); }
            }}>Cancel</Button>
          )}
          {d.permissions?.can_rerun && !running && (
            <Button onClick={async () => {
              try { const r = await api.post<{ id: string }>(`/investigations/${id}/rerun`); nav(`/investigations/${r.id}`); } catch (e) { setActionError(e); }
            }}>Rerun</Button>
          )}
          {report?.status === "approved" && (
            <>
              <a className="rounded-lg px-3 py-1.5 text-sm font-medium text-brand ring-1 ring-line hover:bg-slate-50" href={`/api/v1/investigations/${id}/report/export?format=html`}>Export HTML</a>
              <a className="rounded-lg px-3 py-1.5 text-sm font-medium text-brand ring-1 ring-line hover:bg-slate-50" href={`/api/v1/investigations/${id}/report/export?format=pdf`}>Export PDF</a>
            </>
          )}
        </div>
      </div>
      <ErrorNote error={actionError} />
      {d.error && <ErrorNote error={{ code: d.error.code, message: `Run stopped in ${d.error.node ?? "worker"}${d.error.detail ? `: ${d.error.detail}` : ""}` }} />}

      <div className="grid gap-4 lg:grid-cols-4">
        <div className="space-y-4 lg:col-span-3">
          {d.status === "awaiting_clarification" && d.pending_question && <Clarify inv={d} onDone={refreshAll} />}
          {d.status === "awaiting_review" && report && d.permissions?.can_review && <ReviewControls inv={d} report={report} onDone={refreshAll} />}
          {d.status === "awaiting_review" && !d.permissions?.can_review && (
            <p className="rounded-lg bg-violet-50 px-3 py-2 text-sm text-violet-900">Waiting for a reviewer (the owner cannot approve their own report).</p>
          )}

          <div role="tablist" aria-label="Investigation views" className="flex gap-1 border-b border-line">
            {(["progress", "report", "evidence"] as const).map((t) => (
              <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}
                className={`-mb-px border-b-2 px-3 py-2 text-sm capitalize ${tab === t ? "border-brand font-semibold text-brand" : "border-transparent text-muted hover:text-ink"}`}>
                {t}{t === "evidence" ? ` (${evidence.length})` : ""}{t === "report" && report ? ` v${report.current_version}` : ""}
              </button>
            ))}
          </div>

          {tab === "progress" && (
            <Card title="Live progress" actions={
              <>
                <Badge tone={mode === "sse" ? "green" : "amber"}>{mode === "sse" ? "live (SSE)" : mode === "polling" ? "polling fallback" : "idle"}</Badge>
                <label className="flex items-center gap-1 text-xs text-muted"><input type="checkbox" checked={showTools} onChange={(e) => setShowTools(e.target.checked)} /> tool and model calls</label>
              </>
            }>
              <Timeline steps={steps} onOpen={setOpen} showTools={showTools} />
            </Card>
          )}
          {tab === "report" && (report ? <ReportView report={report} onOpenEvidence={setOpen} /> : <Empty title="No report yet">The report is assembled after verification and the critic review.</Empty>)}
          {tab === "evidence" && (
            <Card title="Evidence ledger">
              {evidence.length === 0 ? <Empty title="No evidence yet" /> : (
                <ul className="divide-y divide-line">
                  {evidence.map((e) => (
                    <li key={e.id} className="flex items-center justify-between gap-2 py-2">
                      <button className="text-left text-sm text-brand hover:underline" onClick={() => setOpen(e.id)}>{e.label}</button>
                      <div className="flex gap-1.5"><Badge>{e.kind}</Badge><Badge>{e.row_count} rows</Badge></div>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          )}
        </div>

        <aside className="space-y-4">
          <Card title="Budgets">
            <div className="space-y-3">
              <Meter label="Source queries" used={d.budgets.queries_used} max={d.budgets.queries_max} />
              <Meter label="Graph steps" used={d.budgets.steps_used} max={d.budgets.steps_max} />
              <Meter label="Hypotheses" used={d.hypotheses?.length ?? 0} max={d.budgets.hypotheses_max} />
              <div className="text-xs text-muted">
                Tokens {d.budgets.tokens_used.toLocaleString()} · cost {d.budgets.cost_used} / {d.budgets.cost_max}
                {d.budgets.cost_note && <div>{d.budgets.cost_note}</div>}
              </div>
              <div className="text-xs text-muted">Model: {d.model.provider} · {d.model.model}</div>
            </div>
          </Card>
          <Card title="Hypotheses">
            {(d.hypotheses ?? []).length === 0 ? <p className="text-sm text-muted">None yet.</p> : (
              <ul className="space-y-2">
                {d.hypotheses!.map((h) => (
                  <li key={h.key} className="text-sm">
                    <div className="flex items-start justify-between gap-2"><span>{h.statement}</span><StatusBadge status={h.status} /></div>
                    {h.rationale && <p className="text-xs text-muted">{h.rationale}</p>}
                  </li>
                ))}
              </ul>
            )}
          </Card>
          {d.primary_driver && (
            <Card title="Primary driver">
              <code className="text-sm">{d.primary_driver.label}</code>
              <p className="mt-1 text-xs text-muted">{d.primary_driver.explanation}</p>
            </Card>
          )}
        </aside>
      </div>
      <EvidenceDrawer id={open} onClose={() => setOpen(null)} />
    </div>
  );
}
