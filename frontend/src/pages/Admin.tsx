import { useState } from "react";
import { api, fmtDate } from "../api";
import { Badge, Button, Card, Empty, ErrorNote, Spinner } from "../components/ui";
import { useFetch } from "../hooks";
import { useSession } from "../session";

interface Member { membership_id: string; user_id: string; email: string; display_name: string; role: string; active: boolean }
interface Audit { id: number; action: string; object_type: string; object_id: string | null; detail: Record<string, unknown>; actor: string | null; created_at: string }
interface Suite { id: string; name: string; split: string; description: string; scenario_count: number; families: string[] }
interface Run { id: string; suite: string; systems: string[]; model_mode: string; status: string; summary: { systems?: Record<string, Record<string, number | string>>; error?: string }; started_at: string; finished_at: string | null }

function Members() {
  const { session } = useSession();
  const { data, error, reload } = useFetch<{ items: Member[] }>("/admin/members");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState("analyst");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);
  return (
    <Card title="Members">
      <ErrorNote error={error || err} />
      <table className="w-full text-sm">
        <thead className="text-left text-xs text-muted"><tr><th className="py-1">User</th><th>Role</th><th /></tr></thead>
        <tbody className="divide-y divide-line">
          {data?.items.map((m) => (
            <tr key={m.membership_id}>
              <td className="py-2">{m.display_name}<div className="text-xs text-muted">{m.email}</div></td>
              <td>
                <select aria-label={`Role for ${m.email}`} value={m.role} disabled={m.user_id === session?.user.id} className="rounded border border-line px-1 py-0.5 text-sm"
                  onChange={async (e) => { try { await api.patch(`/admin/members/${m.membership_id}`, { role: e.target.value }); void reload(); } catch (x) { setErr(x); } }}>
                  {["analyst", "reviewer", "admin"].map((r) => <option key={r}>{r}</option>)}
                </select>
              </td>
              <td className="text-right">
                {m.user_id !== session?.user.id && (
                  <Button variant="ghost" onClick={async () => {
                    if (!confirm(`Remove ${m.email} from this tenant?`)) return;
                    try { await api.del(`/admin/members/${m.membership_id}`); void reload(); } catch (x) { setErr(x); }
                  }}>Remove</Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <form className="mt-4 grid gap-2 sm:grid-cols-4" onSubmit={async (e) => {
        e.preventDefault();
        setErr(null);
        try {
          const r = await api.post<{ temporary_password: string | null }>("/admin/members", { email, display_name: name, role });
          setMsg(r.temporary_password ? `Created. Temporary password (shown once): ${r.temporary_password}` : "Existing user added.");
          setEmail(""); setName(""); void reload();
        } catch (x) { setErr(x); }
      }}>
        <input aria-label="Email" placeholder="email" required value={email} onChange={(e) => setEmail(e.target.value)} className="rounded border border-line px-2 py-1 text-sm" />
        <input aria-label="Display name" placeholder="display name" required value={name} onChange={(e) => setName(e.target.value)} className="rounded border border-line px-2 py-1 text-sm" />
        <select aria-label="Role" value={role} onChange={(e) => setRole(e.target.value)} className="rounded border border-line px-2 py-1 text-sm">
          {["analyst", "reviewer", "admin"].map((r) => <option key={r}>{r}</option>)}
        </select>
        <Button type="submit" variant="primary">Add member</Button>
      </form>
      {msg && <p className="mt-2 rounded bg-emerald-50 px-2 py-1 text-xs text-emerald-900">{msg}</p>}
    </Card>
  );
}

function Evaluation() {
  const suites = useFetch<{ items: Suite[] }>("/eval/suites");
  const runs = useFetch<{ items: Run[] }>("/eval/runs");
  const [suite, setSuite] = useState("synthetic-heldout");
  const [limit, setLimit] = useState(10);
  const [err, setErr] = useState<unknown>(null);
  return (
    <Card title="Evaluation" actions={<Button variant="ghost" onClick={() => void runs.reload()}>Refresh</Button>}>
      <ErrorNote error={err} />
      {!suites.data?.items.length ? <Empty title="No evaluation suites">Run <code>python -m app.cli seed-evals</code>.</Empty> : (
        <div className="flex flex-wrap items-end gap-2">
          <div>
            <label htmlFor="suite" className="block text-xs text-muted">Suite</label>
            <select id="suite" value={suite} onChange={(e) => setSuite(e.target.value)} className="rounded border border-line px-2 py-1 text-sm">
              {suites.data.items.map((s) => <option key={s.id} value={s.name}>{s.name} ({s.scenario_count})</option>)}
            </select>
          </div>
          <div>
            <label htmlFor="lim" className="block text-xs text-muted">Scenario limit</label>
            <input id="lim" type="number" min={1} max={200} value={limit} onChange={(e) => setLimit(Number(e.target.value))} className="w-24 rounded border border-line px-2 py-1 text-sm" />
          </div>
          <Button variant="primary" onClick={async () => {
            try { await api.post("/eval/runs", { suite, systems: ["fixed_dashboard", "single_pass", "full_graph"], limit }); void runs.reload(); } catch (x) { setErr(x); }
          }}>Run fixed dashboard vs single-pass vs full graph</Button>
        </div>
      )}
      <div className="mt-4 space-y-3">
        {runs.data?.items.map((r) => (
          <div key={r.id} className="rounded-lg border border-line p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{r.suite}</span><Badge tone={r.model_mode === "fixture" ? "amber" : "green"}>{r.model_mode}</Badge><Badge>{r.status}</Badge>
              <span className="text-xs text-muted">{fmtDate(r.started_at)}</span>
            </div>
            {r.summary.error && <p className="text-xs text-red-700">{r.summary.error}</p>}
            {r.summary.systems && (
              <table className="mt-2 w-full text-xs">
                <thead className="text-left text-muted"><tr><th>System</th><th>Driver id.</th><th>Numeric</th><th>Unsupported causal</th><th>Completion</th><th>Avg queries</th><th>Avg latency</th></tr></thead>
                <tbody>
                  {Object.entries(r.summary.systems).map(([k, m]) => (
                    <tr key={k} className="num"><td>{k}</td><td>{(Number(m.driver_accuracy) * 100).toFixed(0)}%</td><td>{(Number(m.numeric_accuracy) * 100).toFixed(0)}%</td>
                      <td>{m.unsupported_causal_claims}</td><td>{(Number(m.completion_rate) * 100).toFixed(0)}%</td><td>{m.avg_queries}</td><td>{m.avg_latency_ms} ms</td></tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ))}
      </div>
      <p className="mt-2 text-[11px] text-muted">Ground-truth labels stay in the application database and are not returned by the API or visible to agent tools.</p>
    </Card>
  );
}

function AuditLog() {
  const [offset, setOffset] = useState(0);
  const { data, error } = useFetch<{ items: Audit[]; next_offset: number | null }>(`/admin/audit?limit=25&offset=${offset}`, [offset]);
  return (
    <Card title="Audit log">
      <ErrorNote error={error} />
      {!data ? <Spinner /> : (
        <table className="w-full text-xs">
          <thead className="text-left text-muted"><tr><th className="py-1">When</th><th>Actor</th><th>Action</th><th>Object</th><th>Detail</th></tr></thead>
          <tbody className="divide-y divide-line">
            {data.items.map((a) => (
              <tr key={a.id}><td className="whitespace-nowrap py-1.5">{fmtDate(a.created_at)}</td><td>{a.actor ?? "system"}</td><td><code>{a.action}</code></td>
                <td>{a.object_type} {a.object_id?.slice(0, 8)}</td><td className="max-w-xs truncate text-muted">{JSON.stringify(a.detail)}</td></tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="mt-2 flex justify-end gap-2">
        <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 25))}>Newer</Button>
        <Button disabled={!data?.next_offset} onClick={() => setOffset(data?.next_offset ?? 0)}>Older</Button>
      </div>
    </Card>
  );
}

function Connections() {
  const { data } = useFetch<{ items: { id: string; name: string; kind: string; status: string; config: Record<string, unknown> }[] }>("/connections");
  return (
    <Card title="Data connections">
      {data?.items.map((c) => (
        <div key={c.id} className="text-sm"><span className="font-medium">{c.name}</span> <Badge>{c.kind}</Badge> <Badge tone={c.status === "active" ? "green" : "gray"}>{c.status}</Badge>
          <p className="text-xs text-muted">{Object.entries(c.config).map(([k, v]) => `${k}: ${String(v)}`).join(" · ")}</p></div>
      ))}
      <p className="mt-2 text-[11px] text-muted">One synthetic source connector in this release. Credentials live in server configuration, never in this table.</p>
    </Card>
  );
}

export default function Admin() {
  const { role } = useSession();
  if (role !== "admin") return <ErrorNote error={{ code: "forbidden", message: "Administration requires the admin role." }} />;
  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold">Administration</h1>
      <div className="grid gap-5 lg:grid-cols-2"><Members /><Connections /></div>
      <Evaluation />
      <AuditLog />
    </div>
  );
}
