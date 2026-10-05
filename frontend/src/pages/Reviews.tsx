import { useState } from "react";
import { Link } from "react-router-dom";
import { fmtDate, type Driver } from "../api";
import { Card, Empty, ErrorNote, Spinner, StatusBadge } from "../components/ui";
import { useFetch } from "../hooks";
import { useSession } from "../session";

interface Row { id: string; investigation_id: string; question: string; status: string; investigation_status: string; current_version: number; primary_driver: Driver | null; updated_at: string; owner_id: string }

export default function Reviews() {
  const { role, session } = useSession();
  const [status, setStatus] = useState("in_review");
  const { data, error, loading } = useFetch<{ items: Row[] }>(`/reports?limit=50${status ? `&status=${status}` : ""}`, [status]);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Report review</h1>
          <p className="text-sm text-muted">
            {role === "reviewer" || role === "admin" ? "Approve, request changes or reject reports you did not author." : "Reviewers and admins decide on publication; you can follow status here."}
          </p>
        </div>
        <select aria-label="Report status" value={status} onChange={(e) => setStatus(e.target.value)} className="rounded-lg border border-line px-2 py-1 text-sm">
          {["in_review", "changes_requested", "approved", "rejected", ""].map((s) => <option key={s} value={s}>{s ? s.replaceAll("_", " ") : "all"}</option>)}
        </select>
      </div>
      <ErrorNote error={error} />
      <Card>
        {loading && !data ? <Spinner /> : !data?.items.length ? <Empty title="Nothing here" /> : (
          <ul className="divide-y divide-line">
            {data.items.map((r) => (
              <li key={r.id} className="flex flex-wrap items-center justify-between gap-2 py-3">
                <div className="min-w-0">
                  <Link to={`/investigations/${r.investigation_id}`} className="text-sm font-medium text-brand hover:underline">{r.question}</Link>
                  <div className="text-xs text-muted">
                    version {r.current_version} · driver <code>{r.primary_driver?.label ?? "—"}</code> · updated {fmtDate(r.updated_at)}
                    {r.owner_id === session?.user.id && " · you are the author"}
                  </div>
                </div>
                <StatusBadge status={r.status} />
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
