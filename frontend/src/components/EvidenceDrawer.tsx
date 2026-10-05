import { useEffect, useState } from "react";
import { api, fmtDate, type EvidenceDetail } from "../api";
import { Badge, Drawer, ErrorNote, Mono, Spinner } from "./ui";

function ResultTable({ rows }: { rows: Record<string, unknown>[] }) {
  if (!rows.length) return <p className="text-sm text-muted">No rows.</p>;
  const cols = Object.keys(rows[0]);
  return (
    <div className="max-h-80 overflow-auto rounded border border-line">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-slate-50 text-left">
          <tr>{cols.map((c) => <th key={c} className="px-2 py-1 font-medium">{c}</th>)}</tr>
        </thead>
        <tbody className="divide-y divide-line">
          {rows.map((r, i) => (
            <tr key={i}>{cols.map((c) => <td key={c} className="num px-2 py-1">{String(r[c] ?? "")}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function EvidenceDrawer({ id, onClose }: { id: string | null; onClose: () => void }) {
  const [ev, setEv] = useState<EvidenceDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    setEv(null);
    setError(null);
    if (id) api.get<EvidenceDetail>(`/evidence/${id}`).then(setEv).catch(setError);
  }, [id]);

  const result = ev?.result as { rows?: Record<string, unknown>[]; function?: string; inputs?: string[]; output?: Record<string, unknown>; shape?: string; truncated?: boolean } | undefined;
  const params = ev?.params as Record<string, unknown> | undefined;
  return (
    <Drawer open={!!id} onClose={onClose} title="Evidence">
      {!ev && !error && <Spinner />}
      <ErrorNote error={error} />
      {ev && (
        <div className="space-y-4 text-sm">
          <div>
            <div className="font-medium">{ev.label}</div>
            <div className="mt-1 flex flex-wrap gap-1.5">
              <Badge tone="blue">{ev.kind}</Badge>
              {result?.shape && <Badge>{result.shape}</Badge>}
              <Badge>{ev.row_count} rows</Badge>
              {ev.metric_key && <Badge>{ev.metric_key} v{ev.metric_version}</Badge>}
              {result?.truncated && <Badge tone="amber">truncated</Badge>}
            </div>
          </div>
          <dl className="grid grid-cols-[9rem_1fr] gap-x-3 gap-y-1 text-xs">
            <dt className="text-muted">Evidence ID</dt><dd><Mono>{ev.id}</Mono></dd>
            <dt className="text-muted">Result hash</dt><dd><Mono>{ev.result_hash}</Mono></dd>
            {ev.plan_hash && (<><dt className="text-muted">Query-plan hash</dt><dd><Mono>{ev.plan_hash}</Mono></dd></>)}
            <dt className="text-muted">Source watermark</dt><dd>{fmtDate(ev.source_watermark)}</dd>
            <dt className="text-muted">Execution</dt><dd>{ev.execution_ms} ms</dd>
            <dt className="text-muted">Recorded</dt><dd>{fmtDate(ev.created_at)}</dd>
          </dl>
          {params && "b_start" in params && (
            <div>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">Date scope (UTC bounds of local days)</h3>
              <p className="num text-xs">baseline [{String(params.b_start)}, {String(params.b_end)}) · current [{String(params.c_start)}, {String(params.c_end)})</p>
            </div>
          )}
          {result?.function && (
            <div>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">Calculation provenance</h3>
              <p className="text-xs">Function <Mono>{result.function}</Mono> applied to evidence {result.inputs?.map((i) => <Mono key={i}>{i.slice(0, 8)}</Mono>)}</p>
              <pre className="mt-2 max-h-80 overflow-auto rounded bg-slate-50 p-2 text-[11px]">{JSON.stringify(result.output, null, 2)}</pre>
            </div>
          )}
          {result?.rows && (
            <div>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">Result</h3>
              <ResultTable rows={result.rows} />
            </div>
          )}
          <div>
            <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">Compiled SQL and parameters</h3>
            {ev.compiled_sql ? (
              <>
                <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-slate-900 p-3 text-[11px] text-slate-100">{ev.compiled_sql}</pre>
                <pre className="mt-2 max-h-48 overflow-auto rounded bg-slate-50 p-2 text-[11px]">{JSON.stringify(ev.params, null, 2)}</pre>
                <p className="mt-1 text-[11px] text-muted">Compiled from a validated QueryPlan; values are bound parameters, never inlined.</p>
              </>
            ) : (
              <p className="text-xs text-muted">{ev.kind === "query" || ev.kind === "freshness" ? "Hidden for your role (analysts and admins can view SQL)." : "Not applicable: this is a deterministic calculation over other evidence."}</p>
            )}
          </div>
        </div>
      )}
    </Drawer>
  );
}
