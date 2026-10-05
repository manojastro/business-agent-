import { useState } from "react";
import { Link } from "react-router-dom";
import { fmtDate, type EvidenceSummary } from "../api";
import EvidenceDrawer from "../components/EvidenceDrawer";
import { Badge, Button, Card, Empty, ErrorNote, Mono, Spinner } from "../components/ui";
import { useFetch } from "../hooks";

export default function EvidenceExplorer() {
  const [kind, setKind] = useState("");
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const { data, error, loading } = useFetch<{ items: EvidenceSummary[]; next_offset: number | null }>(
    `/evidence?limit=50&offset=${offset}${kind ? `&kind=${kind}` : ""}`, [kind, offset]);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Evidence explorer</h1>
          <p className="text-sm text-muted">Append-only, hash-addressed records of every source query and calculation in this tenant.</p>
        </div>
        <select aria-label="Evidence kind" value={kind} onChange={(e) => { setKind(e.target.value); setOffset(0); }} className="rounded-lg border border-line px-2 py-1 text-sm">
          {["", "query", "calculation", "freshness", "quality"].map((k) => <option key={k} value={k}>{k || "all kinds"}</option>)}
        </select>
      </div>
      <ErrorNote error={error} />
      <Card>
        {loading && !data ? <Spinner /> : !data?.items.length ? <Empty title="No evidence yet">Evidence appears when investigations run.</Empty> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-muted"><tr><th className="py-2">Label</th><th>Kind</th><th>Rows</th><th>Result hash</th><th>Investigation</th><th>Recorded</th></tr></thead>
              <tbody className="divide-y divide-line">
                {data.items.map((e) => (
                  <tr key={e.id}>
                    <td className="py-2"><button onClick={() => setOpen(e.id)} className="text-left text-brand hover:underline">{e.label}</button></td>
                    <td><Badge>{e.kind}</Badge></td>
                    <td className="num">{e.row_count}</td>
                    <td><Mono>{e.result_hash.slice(0, 12)}</Mono></td>
                    <td><Link to={`/investigations/${e.investigation_id}`} className="font-mono text-xs text-brand hover:underline">{e.investigation_id.slice(0, 8)}</Link></td>
                    <td className="whitespace-nowrap text-xs text-muted">{fmtDate(e.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="mt-3 flex justify-end gap-2">
          <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</Button>
          <Button disabled={!data?.next_offset} onClick={() => setOffset(data?.next_offset ?? 0)}>Next</Button>
        </div>
      </Card>
      <EvidenceDrawer id={open} onClose={() => setOpen(null)} />
    </div>
  );
}
