import { fmtMoney, fmtNum, unitOf, type Report, type ReportItem } from "../api";
import { DailyLine, SignedBars } from "./Charts";
import { Badge, SimulationBadge, StatusBadge } from "./ui";

function Items({ title, items, onOpen, empty }: { title: string; items: ReportItem[]; onOpen: (id: string) => void; empty: string }) {
  return (
    <section>
      <h3 className="mb-2 text-sm font-semibold">{title}</h3>
      {items.length === 0 ? (
        <p className="text-sm text-muted">{empty}</p>
      ) : (
        <ul className="space-y-2">
          {items.map((f, i) => (
            <li key={f.key + i} className="rounded-lg border border-line bg-white px-3 py-2 text-sm">
              <p>{f.wording}</p>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                {f.verification.length > 0 && <Badge tone="green" title="Every number recomputed from evidence">✓ {f.verification.length} numbers verified</Badge>}
                {f.claim_type === "critic_warning" && <Badge tone="amber">critic</Badge>}
                {f.evidence_ids.map((e) => (
                  <button key={e} onClick={() => onOpen(e)} className="font-mono text-[11px] text-brand hover:underline">evidence {e.slice(0, 8)}</button>
                ))}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function ReportView({ report, onOpenEvidence }: { report: Report; onOpenEvidence: (id: string) => void }) {
  const c = report.content;
  const h = c.headline;
  const u = unitOf(c.scope.metric.key);
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">{c.title}</h2>
        <StatusBadge status={report.status} />
        <Badge>version {report.version}</Badge>
        <SimulationBadge label={c.simulation_label} />
      </div>
      {c.revision_note && <p className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-900">{c.revision_note}</p>}
      <div className="grid gap-4 md:grid-cols-3">
        <div className="rounded-xl border border-line bg-white p-4 md:col-span-2">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Summary</h3>
          <p className="mt-1 text-sm">{c.summary}</p>
          <div className="mt-3 rounded-lg bg-brand-soft px-3 py-2 text-sm">
            <span className="font-semibold">Primary driver: </span><code>{c.primary_driver.label}</code>
            <span className="text-muted"> ({c.primary_driver.confidence} confidence)</span>
            <p className="text-xs text-muted">{c.primary_driver.explanation} Observed contribution — not a causal claim.</p>
          </div>
        </div>
        <div className="rounded-xl border border-line bg-white p-4 text-sm">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Headline</h3>
          {h && (
            <dl className="num mt-1 grid grid-cols-2 gap-y-1">
              <dt className="text-muted">Baseline</dt><dd>{fmtNum(h.baseline as string, u)}</dd>
              <dt className="text-muted">Current</dt><dd>{fmtNum(h.current as string, u)}</dd>
              <dt className="text-muted">Change</dt><dd>{fmtNum(h.abs_change as string, u)}</dd>
              <dt className="text-muted">% change</dt><dd>{h.pct_change === null ? "undefined" : `${h.pct_change}%`}</dd>
              {c.materiality && (c.materiality as { defined?: boolean }).defined && (
                <><dt className="text-muted">Typical variation</dt><dd>±{String((c.materiality as { band_pct: string }).band_pct)}%</dd></>
              )}
            </dl>
          )}
          <p className="mt-2 text-[11px] text-muted">
            {c.scope.metric.name} v{c.scope.metric.version} · [{c.scope.current_window.start}, {c.scope.current_window.end}) vs [{c.scope.baseline_window.start}, {c.scope.baseline_window.end}) · {c.scope.timezone} · {c.scope.currency}
          </p>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {c.charts.components && (
          <div className="rounded-xl border border-line bg-white p-4">
            <SignedBars title={`Component contributions (total ${fmtMoney(c.charts.components.total)})`} points={c.charts.components.points}
              evidenceId={c.charts.components.evidence_id} onOpen={onOpenEvidence} height={150} />
          </div>
        )}
        {c.charts.dimension && (
          <div className="rounded-xl border border-line bg-white p-4">
            <SignedBars title={`Contribution by ${c.charts.dimension.dimension.replace("_", " ")} (one dimension at a time)`}
              points={c.charts.dimension.points} evidenceId={c.charts.dimension.evidence_id} onOpen={onOpenEvidence} />
          </div>
        )}
        {c.charts.daily && (
          <div className="rounded-xl border border-line bg-white p-4 lg:col-span-2">
            <DailyLine points={c.charts.daily.points} evidenceId={c.charts.daily.evidence_id} onOpen={onOpenEvidence} currentStart={c.scope.current_window.start} />
          </div>
        )}
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Items title="Findings" items={c.findings} onOpen={onOpenEvidence} empty="No verified findings." />
        <Items title="Hypotheses (not established causes)" items={c.hypotheses} onOpen={onOpenEvidence} empty="No hypothesis statements." />
        <Items title="Limitations" items={c.limitations} onOpen={onOpenEvidence} empty="None recorded." />
        <Items title="Recommended next investigations" items={c.recommendations} onOpen={onOpenEvidence} empty="None." />
      </div>

      {c.rejected_claims.length > 0 && (
        <section className="rounded-xl border border-red-200 bg-red-50/50 p-4">
          <h3 className="text-sm font-semibold text-red-900">Rejected claims (excluded from the findings)</h3>
          <ul className="mt-2 space-y-2">
            {c.rejected_claims.map((r) => (
              <li key={r.key} className="rounded-lg bg-white px-3 py-2 text-sm ring-1 ring-red-200">
                <p className="line-through decoration-red-400">{r.wording}</p>
                <div className="mt-1 flex flex-wrap gap-1">
                  {r.origin === "planted" && <Badge tone="violet">seeded misleading recommendation</Badge>}
                  {r.reasons.map((x, i) => <Badge key={i} tone="red" title={x.detail}>{x.code.replaceAll("_", " ")}</Badge>)}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section>
        <h3 className="mb-2 text-sm font-semibold">Hypotheses tested</h3>
        <div className="overflow-x-auto rounded-xl border border-line bg-white">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs text-muted"><tr><th className="px-3 py-2">Hypothesis</th><th>Status</th><th className="px-3">Rationale</th></tr></thead>
            <tbody className="divide-y divide-line">
              {c.hypothesis_table.map((hy) => (
                <tr key={hy.key}><td className="px-3 py-2">{hy.statement}</td><td><StatusBadge status={hy.status} /></td><td className="px-3 text-xs text-muted">{hy.rationale}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
