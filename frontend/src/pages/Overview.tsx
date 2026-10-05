import { Link } from "react-router-dom";
import { fmtDate, fmtNum, type Driver } from "../api";
import { CountBars } from "../components/Charts";
import { Badge, Card, Empty, ErrorNote, Spinner, StatusBadge } from "../components/ui";
import { useFetch } from "../hooks";

interface OverviewData {
  as_of: string;
  freshness: { watermark: string | null; fresh: boolean; missing_order_batch_days: string[]; days: { date: string; orders_rows: number | null }[] };
  metric_changes: { metric: string; name: string; unit: string; baseline: string | null; current: string | null; abs_change: string | null; pct_change: string | null; version: number; current_window: { start: string; end: string } }[];
  recent_runs: { id: string; question: string; status: string; created_at: string; primary_driver: Driver | null; model_mode: string }[];
  evaluations: { id: string; model_mode: string; summary: { suite?: string; systems?: Record<string, { driver_accuracy: number; numeric_accuracy: number; unsupported_causal_claims: number; scenarios: number }> }; finished_at: string | null }[];
}

export default function Overview() {
  const { data, error, loading } = useFetch<OverviewData>("/overview");
  if (loading && !data) return <Spinner />;
  if (error) return <ErrorNote error={error} />;
  if (!data) return null;
  const cw = data.metric_changes[0]?.current_window;
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Overview</h1>
          <p className="text-sm text-muted">
            Last complete 7 days [{cw?.start}, {cw?.end}) vs the 7 days before · as of {data.as_of} · Asia/Kolkata · INR
          </p>
        </div>
        <Link to="/investigations/new" className="rounded-lg bg-brand px-3 py-2 text-sm font-medium text-white hover:bg-blue-900">New investigation</Link>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {data.metric_changes.map((m) => {
          const pct = m.pct_change === null ? null : Number(m.pct_change);
          return (
            <div key={m.metric} className="rounded-xl border border-line bg-white p-4 shadow-sm">
              <div className="flex items-center justify-between text-xs text-muted"><span>{m.name}</span><span>v{m.version}</span></div>
              <div className="num mt-1 text-xl font-semibold">{fmtNum(m.current, m.unit)}</div>
              <div className="num mt-1 text-xs text-muted">baseline {fmtNum(m.baseline, m.unit)}</div>
              <div className={`num mt-2 text-sm font-medium ${pct === null ? "text-muted" : "text-ink"}`}>
                {pct === null ? "change undefined (zero baseline)" : `${pct > 0 ? "▲" : pct < 0 ? "▼" : "■"} ${Math.abs(pct).toFixed(2)}%`}
              </div>
            </div>
          );
        })}
      </div>
      <p className="-mt-3 text-[11px] text-muted">Dashboard values are computed live through the same read-only, tenant-scoped query path. They are observations, not explanations.</p>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Recent investigations" className="lg:col-span-2">
          {data.recent_runs.length === 0 ? (
            <Empty title="No investigations yet">Start one from “New investigation”.</Empty>
          ) : (
            <ul className="divide-y divide-line">
              {data.recent_runs.map((r) => (
                <li key={r.id} className="flex flex-wrap items-center justify-between gap-2 py-2.5">
                  <div className="min-w-0">
                    <Link to={`/investigations/${r.id}`} className="block truncate text-sm font-medium text-brand hover:underline">{r.question}</Link>
                    <div className="text-xs text-muted">{fmtDate(r.created_at)} {r.primary_driver && <>· driver <code>{r.primary_driver.label}</code></>}</div>
                  </div>
                  <div className="flex gap-1.5">{r.model_mode === "fixture" && <Badge tone="amber">Demo simulation</Badge>}<StatusBadge status={r.status} /></div>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Source freshness" actions={<Badge tone={data.freshness.fresh ? "green" : "red"}>{data.freshness.fresh ? "fresh" : "stale"}</Badge>}>
          <p className="text-xs text-muted">Orders watermark: {fmtDate(data.freshness.watermark)}</p>
          <CountBars label="Orders loaded per day" data={data.freshness.days.map((d) => ({ x: d.date.slice(5), y: d.orders_rows ?? 0, missing: d.orders_rows === null }))} />
          {data.freshness.missing_order_batch_days.length > 0 && (
            <p className="mt-1 text-xs text-red-700">Missing batches: {data.freshness.missing_order_batch_days.join(", ")}</p>
          )}
        </Card>
      </div>

      <Card title="Measured evaluation results">
        {data.evaluations.length === 0 ? (
          <Empty title="No evaluation runs yet">Admins can start one under Administration → Evaluation.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-muted"><tr><th className="py-1">Run</th><th>System</th><th>Driver identification</th><th>Numeric accuracy</th><th>Unsupported causal claims</th></tr></thead>
              <tbody className="divide-y divide-line">
                {data.evaluations.flatMap((e) =>
                  Object.entries(e.summary.systems ?? {}).map(([sys, m]) => (
                    <tr key={e.id + sys}>
                      <td className="py-1.5 text-xs">{e.summary.suite} <Badge tone={e.model_mode === "fixture" ? "amber" : "green"}>{e.model_mode}</Badge></td>
                      <td>{sys.replaceAll("_", " ")}</td>
                      <td className="num">{(m.driver_accuracy * 100).toFixed(0)}% <span className="text-xs text-muted">(n={m.scenarios})</span></td>
                      <td className="num">{(m.numeric_accuracy * 100).toFixed(0)}%</td>
                      <td className="num">{m.unsupported_causal_claims}</td>
                    </tr>
                  )),
                )}
              </tbody>
            </table>
            <p className="mt-2 text-[11px] text-muted">Fixture-mode results reflect scripted, deterministic model behaviour and validate the pipeline, not language-model reasoning.</p>
          </div>
        )}
      </Card>
    </div>
  );
}
