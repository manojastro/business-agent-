import { CountBars } from "../components/Charts";
import { Badge, Card, ErrorNote, Spinner } from "../components/ui";
import { fmtDate } from "../api";
import { useFetch } from "../hooks";

interface QualityData {
  windows: { baseline: { start: string; end: string }; current: { start: string; end: string } };
  checks: { window: string; order_rows: number; duplicate_rows: number; orders_without_items: number; non_reporting_currency_rows: number; canceled_rows: number; null_ordered_at_rows: number }[];
  daily_orders: { day: string; completed_orders: number; canceled_orders: number }[];
  freshness: { watermark: string | null; required_watermark: string; fresh: boolean; missing_order_batch_days: string[]; days: { date: string; orders_rows: number | null; refunds_rows: number | null }[] };
}

export default function Quality() {
  const { data, error, loading } = useFetch<QualityData>("/source/quality");
  if (loading && !data) return <Spinner />;
  if (error) return <ErrorNote error={error} />;
  if (!data) return null;
  const f = data.freshness;
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">Data quality</h1>
        <p className="text-sm text-muted">Freshness and quality failures change investigation answers: incomplete windows stop the run or require an explicit incomplete-data label.</p>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Ingestion batches (orders rows per local day)" actions={<Badge tone={f.fresh ? "green" : "red"}>{f.fresh ? "watermark covers as-of" : "stale"}</Badge>}>
          <p className="text-xs text-muted">Watermark {fmtDate(f.watermark)} · required {fmtDate(f.required_watermark)}</p>
          <CountBars label="Orders rows per day" height={200} data={f.days.map((d) => ({ x: d.date.slice(5), y: d.orders_rows ?? 0, missing: d.orders_rows === null }))} />
          <p className="mt-1 text-xs">{f.missing_order_batch_days.length ? <span className="text-red-700">Missing: {f.missing_order_batch_days.join(", ")}</span> : <span className="text-muted">No missing order batches in the last 21 days.</span>}</p>
        </Card>
        <Card title="Completed orders per day (both comparison windows)">
          <CountBars label="Completed orders per day" height={200} data={data.daily_orders.map((d) => ({ x: d.day.slice(5), y: d.completed_orders }))} />
        </Card>
      </div>
      <Card title="Checks for the default comparison windows">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-muted"><tr><th className="py-1">Window</th><th>Order rows</th><th>Duplicate rows</th><th>Orders without items</th><th>Non-INR rows</th><th>Canceled</th><th>Missing order date</th></tr></thead>
            <tbody className="divide-y divide-line">
              {data.checks.map((c) => (
                <tr key={c.window} className="num">
                  <td className="py-2 font-medium">{c.window} [{data.windows[c.window as "baseline" | "current"].start}, {data.windows[c.window as "baseline" | "current"].end})</td>
                  <td>{c.order_rows}</td>
                  <td className={c.duplicate_rows ? "text-red-700" : ""}>{c.duplicate_rows}</td>
                  <td className={c.orders_without_items ? "text-amber-700" : ""}>{c.orders_without_items}</td>
                  <td className={c.non_reporting_currency_rows ? "text-red-700" : ""}>{c.non_reporting_currency_rows}</td>
                  <td>{c.canceled_rows}</td>
                  <td className={c.null_ordered_at_rows ? "text-amber-700" : ""}>{c.null_ordered_at_rows}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
