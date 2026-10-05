import { useState } from "react";
import { api, fmtDate } from "../api";
import { Badge, Button, Card, ErrorNote, Spinner, StatusBadge } from "../components/ui";
import { useFetch } from "../hooks";
import { useSession } from "../session";

interface MetricRow {
  id: string; metric_key: string; version: number; status: string; change_reason: string; created_at: string;
  definition: { name: string; description: string; formula: string; unit: string; additive: boolean; allowed_dimensions: string[]; rules: Record<string, string>; calculation: string };
}
interface CatalogData { items: MetricRow[]; dimensions: Record<string, { label: string; grain: string; values?: string[]; null_handling: string; allocation?: string }> }

function NewVersion({ m, onDone, dims }: { m: MetricRow; onDone: () => void; dims: string[] }) {
  const [desc, setDesc] = useState(m.definition.description);
  const [allowed, setAllowed] = useState<string[]>(m.definition.allowed_dimensions);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(null);
  return (
    <div className="mt-3 rounded-lg border border-amber-300 bg-amber-50/40 p-3">
      <p className="text-xs text-amber-900">Changing a business metric definition creates a new audited version. Running investigations keep the version they started with.</p>
      <label className="mt-2 block text-xs font-medium" htmlFor={`d-${m.id}`}>Description</label>
      <input id={`d-${m.id}`} value={desc} onChange={(e) => setDesc(e.target.value)} className="mt-1 w-full rounded border border-line px-2 py-1 text-sm" />
      <fieldset className="mt-2"><legend className="text-xs font-medium">Allowed dimensions</legend>
        <div className="mt-1 flex flex-wrap gap-3">
          {dims.map((d) => (
            <label key={d} className="flex items-center gap-1 text-sm">
              <input type="checkbox" checked={allowed.includes(d)} onChange={(e) => setAllowed(e.target.checked ? [...allowed, d] : allowed.filter((x) => x !== d))} /> {d}
            </label>
          ))}
        </div>
      </fieldset>
      <label className="mt-2 block text-xs font-medium" htmlFor={`r-${m.id}`}>Reason for change (audited)</label>
      <input id={`r-${m.id}`} value={reason} onChange={(e) => setReason(e.target.value)} className="mt-1 w-full rounded border border-line px-2 py-1 text-sm" />
      <ErrorNote error={error} />
      <Button className="mt-2" variant="primary" disabled={reason.trim().length < 5} onClick={async () => {
        try {
          await api.post(`/metrics/${m.metric_key}/versions`, { description: desc, allowed_dimensions: allowed, change_reason: reason });
          onDone();
        } catch (e) { setError(e); }
      }}>Create version {m.version + 1}</Button>
    </div>
  );
}

export default function Catalog() {
  const { role } = useSession();
  const { data, error, loading, reload } = useFetch<CatalogData>("/metrics");
  const [editing, setEditing] = useState<string | null>(null);
  if (loading && !data) return <Spinner />;
  if (error) return <ErrorNote error={error} />;
  if (!data) return null;
  const active = data.items.filter((m) => m.status === "active");
  const history = data.items.filter((m) => m.status !== "active");
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">Metric catalog</h1>
        <p className="text-sm text-muted">Versioned semantic definitions. Queries can only use these metrics, dimensions and join rules.</p>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        {active.map((m) => (
          <Card key={m.id} title={<span>{m.definition.name} <span className="text-xs font-normal text-muted">({m.metric_key})</span></span>}
            actions={<><Badge tone="blue">v{m.version}</Badge><StatusBadge status={m.status} /></>}>
            <p className="text-sm">{m.definition.description}</p>
            <p className="mt-2 rounded bg-slate-50 px-2 py-1 font-mono text-[11px]">{m.definition.formula}</p>
            <div className="mt-2 flex flex-wrap gap-1">
              <Badge>{m.definition.unit}</Badge>
              <Badge>{m.definition.additive ? "additive" : "ratio (non-additive)"}</Badge>
              {m.definition.allowed_dimensions.map((d) => <Badge key={d} tone="violet">{d}</Badge>)}
            </div>
            <details className="mt-3 text-sm">
              <summary className="cursor-pointer text-brand">Rules: windows, refunds, nulls, zero denominators</summary>
              <dl className="mt-2 space-y-1.5 text-xs">
                {Object.entries(m.definition.rules).map(([k, v]) => (<div key={k}><dt className="font-semibold">{k.replaceAll("_", " ")}</dt><dd className="text-muted">{v}</dd></div>))}
              </dl>
            </details>
            <p className="mt-2 text-[11px] text-muted">Version reason: {m.change_reason} · {fmtDate(m.created_at)}</p>
            {role === "admin" && (editing === m.id
              ? <NewVersion m={m} dims={Object.keys(data.dimensions)} onDone={() => { setEditing(null); void reload(); }} />
              : <Button className="mt-2" variant="ghost" onClick={() => setEditing(m.id)}>Propose new version…</Button>)}
          </Card>
        ))}
      </div>
      <Card title="Dimensions">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-muted"><tr><th className="py-1">Dimension</th><th>Grain</th><th>Values</th><th>Null handling / allocation</th></tr></thead>
            <tbody className="divide-y divide-line">
              {Object.entries(data.dimensions).map(([k, d]) => (
                <tr key={k}><td className="py-2 font-medium">{d.label} <code className="text-xs">{k}</code></td><td className="text-xs">{d.grain}</td>
                  <td className="text-xs">{d.values?.join(", ") ?? "campaign ids or 'none'"}</td><td className="text-xs text-muted">{d.null_handling} {d.allocation}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      {history.length > 0 && (
        <Card title="Retired versions">
          <ul className="space-y-1 text-sm">{history.map((m) => <li key={m.id}><code>{m.metric_key}</code> v{m.version} — {m.change_reason}</li>)}</ul>
        </Card>
      )}
    </div>
  );
}
