import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, newIdempotencyKey } from "../api";
import { Button, Card, ErrorNote } from "../components/ui";
import { useSession } from "../session";

const METRICS = [
  { value: "", label: "Infer from the question (asks if ambiguous)" },
  { value: "net_sales", label: "Net sales" },
  { value: "order_count", label: "Order count" },
  { value: "average_order_value", label: "Average order value" },
  { value: "refund_rate", label: "Refund rate" },
];

const addDays = (iso: string, n: number) => {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
};

export default function NewInvestigation() {
  const { session } = useSession();
  const nav = useNavigate();
  const [question, setQuestion] = useState("Why did net sales fall in the last complete seven days compared with the preceding seven days?");
  const [metric, setMetric] = useState("net_sales");
  const [asOf, setAsOf] = useState(session?.demo_as_of ?? "2026-09-01");
  const [custom, setCustom] = useState(false);
  const [days, setDays] = useState(7);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const idem = useMemo(() => newIdempotencyKey(), []);

  const current = { start: addDays(asOf, -days), end: asOf };
  const baseline = { start: addDays(asOf, -2 * days), end: addDays(asOf, -days) };

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <h1 className="text-xl font-semibold">New investigation</h1>
      <Card>
        <form
          className="space-y-4"
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError(null);
            try {
              const body: Record<string, unknown> = { question, as_of: asOf };
              if (metric) body.metric_key = metric;
              if (custom) Object.assign(body, { baseline_window: baseline, current_window: current });
              const r = await api.post<{ id: string }>("/investigations", body, { "Idempotency-Key": idem });
              nav(`/investigations/${r.id}`);
            } catch (err) {
              setError(err);
            } finally {
              setBusy(false);
            }
          }}
        >
          <div>
            <label htmlFor="q" className="text-sm font-medium">Question</label>
            <textarea id="q" rows={3} required minLength={5} maxLength={2000} value={question} onChange={(e) => setQuestion(e.target.value)}
              className="mt-1 w-full rounded-lg border border-line px-3 py-2 text-sm" />
            <p className="mt-1 text-xs text-muted">Ambiguous words such as “revenue” trigger a clarification instead of a silent choice.</p>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label htmlFor="m" className="text-sm font-medium">Metric</label>
              <select id="m" value={metric} onChange={(e) => setMetric(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-2 py-2 text-sm">
                {METRICS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
              </select>
            </div>
            <div>
              <label htmlFor="asof" className="text-sm font-medium">As-of date (excluded, treated as incomplete)</label>
              <input id="asof" type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} className="mt-1 w-full rounded-lg border border-line px-2 py-1.5 text-sm" />
            </div>
          </div>
          <fieldset className="rounded-lg border border-line p-3">
            <legend className="px-1 text-sm font-medium">Comparison windows</legend>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={custom} onChange={(e) => setCustom(e.target.checked)} /> Choose window length (default: last 7 complete days vs prior 7)
            </label>
            {custom && (
              <div className="mt-2 flex items-center gap-2 text-sm">
                <label htmlFor="days">Days per window</label>
                <select id="days" value={days} onChange={(e) => setDays(Number(e.target.value))} className="rounded border border-line px-2 py-1">
                  {[7, 14, 28].map((d) => <option key={d} value={d}>{d}</option>)}
                </select>
              </div>
            )}
            <p className="num mt-2 text-xs text-muted">
              Current [{custom ? current.start : addDays(asOf, -7)}, {asOf}) vs baseline [{custom ? baseline.start : addDays(asOf, -14)}, {custom ? baseline.end : addDays(asOf, -7)}) — half-open, equal length, same weekdays.
            </p>
          </fieldset>
          <ErrorNote error={error} />
          <div className="flex justify-end gap-2">
            <Button type="submit" variant="primary" disabled={busy}>{busy ? "Starting…" : "Start investigation"}</Button>
          </div>
        </form>
      </Card>
      <p className="text-xs text-muted">
        The run is asynchronous: it is queued for the worker and you can watch progress live. Source data is read through a read-only, tenant-scoped role;
        every number in the report is recomputed from stored evidence before a reviewer can publish it.
      </p>
    </div>
  );
}
