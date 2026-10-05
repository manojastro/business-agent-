import { useState } from "react";
import { Link } from "react-router-dom";
import { fmtDate, type Investigation } from "../api";
import { Badge, Button, Card, Empty, ErrorNote, Spinner, StatusBadge } from "../components/ui";
import { useFetch } from "../hooks";

const STATUSES = ["", "queued", "running", "awaiting_clarification", "awaiting_review", "completed", "rejected", "cancelled", "failed", "insufficient_evidence"];

export default function Investigations() {
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const { data, error, loading } = useFetch<{ items: Investigation[]; next_offset: number | null }>(
    `/investigations?limit=20&offset=${offset}${status ? `&status=${status}` : ""}`, [status, offset]);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Investigations</h1>
        <div className="flex items-center gap-2">
          <label htmlFor="st" className="text-sm text-muted">Status</label>
          <select id="st" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }} className="rounded-lg border border-line px-2 py-1 text-sm">
            {STATUSES.map((s) => <option key={s} value={s}>{s ? s.replaceAll("_", " ") : "all"}</option>)}
          </select>
          <Link to="/investigations/new" className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white">New</Link>
        </div>
      </div>
      <ErrorNote error={error} />
      <Card>
        {loading && !data ? <Spinner /> : !data?.items.length ? <Empty title="No investigations match" /> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-muted"><tr><th className="py-2">Question</th><th>Metric</th><th>Status</th><th>Driver</th><th>Created</th></tr></thead>
              <tbody className="divide-y divide-line">
                {data.items.map((i) => (
                  <tr key={i.id}>
                    <td className="max-w-md py-2"><Link className="text-brand hover:underline" to={`/investigations/${i.id}`}>{i.question}</Link></td>
                    <td className="text-xs">{i.metric_key ?? "—"}</td>
                    <td><div className="flex gap-1"><StatusBadge status={i.status} />{i.simulation_label && <Badge tone="amber">sim</Badge>}</div></td>
                    <td className="text-xs"><code>{i.primary_driver?.label ?? "—"}</code></td>
                    <td className="whitespace-nowrap text-xs text-muted">{fmtDate(i.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="mt-3 flex justify-end gap-2">
          <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>Previous</Button>
          <Button disabled={!data?.next_offset} onClick={() => setOffset(data?.next_offset ?? 0)}>Next</Button>
        </div>
      </Card>
    </div>
  );
}
