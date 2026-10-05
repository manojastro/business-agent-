import { Bar, BarChart, CartesianGrid, Cell, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fmtMoney } from "../api";

const POS = "#0f766e"; // teal-700
const NEG = "#b91c1c"; // red-700
const AXIS = { fontSize: 11, fill: "#5b6475" };

const compact = (v: number) =>
  Math.abs(v) >= 1e7 ? `${(v / 1e7).toFixed(1)}Cr` : Math.abs(v) >= 1e5 ? `${(v / 1e5).toFixed(1)}L` : Math.abs(v) >= 1e3 ? `${(v / 1e3).toFixed(0)}k` : `${v}`;

function EvidenceRef({ id, onOpen }: { id: string; onOpen?: (id: string) => void }) {
  return (
    <button type="button" onClick={() => onOpen?.(id)} className="font-mono text-[11px] text-brand underline-offset-2 hover:underline">
      evidence {id.slice(0, 8)}
    </button>
  );
}

/** Signed bars (contribution to a change). Data comes straight from verified evidence. */
export function SignedBars({ title, points, evidenceId, onOpen, height = 220 }: {
  title: string; points: { label: string; value: string; small?: boolean }[]; evidenceId: string; onOpen?: (id: string) => void; height?: number;
}) {
  const data = points.map((p) => ({ ...p, v: Number(p.value) }));
  return (
    <figure>
      <figcaption className="mb-1 flex items-center justify-between text-xs text-muted">
        <span className="font-medium text-ink">{title}</span>
        <EvidenceRef id={evidenceId} onOpen={onOpen} />
      </figcaption>
      <div style={{ height }} role="img" aria-label={`${title}: ${data.map((d) => `${d.label} ${fmtMoney(d.v)}`).join(", ")}`}>
        <ResponsiveContainer>
          <BarChart data={data} layout="vertical" margin={{ left: 8, right: 16, top: 4, bottom: 4 }}>
            <CartesianGrid horizontal={false} stroke="#eef1f5" />
            <XAxis type="number" tick={AXIS} tickFormatter={compact} />
            <YAxis type="category" dataKey="label" tick={AXIS} width={96} />
            <ReferenceLine x={0} stroke="#94a3b8" />
            <Tooltip formatter={(v) => fmtMoney(v as number)} cursor={{ fill: "#f1f5f9" }} />
            <Bar dataKey="v" name="Change" radius={[0, 3, 3, 0]}>
              {data.map((d) => <Cell key={d.label} fill={d.v < 0 ? NEG : POS} fillOpacity={d.small ? 0.45 : 1} />)}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      {data.some((d) => d.small) && <p className="text-[11px] text-muted">Faded bars are small segments (&lt;30 orders in a window).</p>}
    </figure>
  );
}

export function DailyLine({ points, evidenceId, onOpen, currentStart }: {
  points: { day: string; value: string }[]; evidenceId: string; onOpen?: (id: string) => void; currentStart?: string;
}) {
  const data = points.map((p) => ({ day: p.day.slice(5, 10), full: p.day, v: Number(p.value) }));
  return (
    <figure>
      <figcaption className="mb-1 flex items-center justify-between text-xs text-muted">
        <span className="font-medium text-ink">Daily value (baseline then current window)</span>
        <EvidenceRef id={evidenceId} onOpen={onOpen} />
      </figcaption>
      <div style={{ height: 200 }} role="img" aria-label="Daily metric values">
        <ResponsiveContainer>
          <LineChart data={data} margin={{ left: 8, right: 16, top: 4, bottom: 4 }}>
            <CartesianGrid stroke="#eef1f5" />
            <XAxis dataKey="day" tick={AXIS} />
            <YAxis tick={AXIS} tickFormatter={compact} width={52} />
            {currentStart && <ReferenceLine x={currentStart.slice(5, 10)} stroke="#64748b" strokeDasharray="4 3" label={{ value: "current", fontSize: 10, fill: "#64748b" }} />}
            <Tooltip formatter={(v) => fmtMoney(v as number)} labelFormatter={(_, p) => (p?.[0]?.payload?.full as string) ?? ""} />
            <Line type="monotone" dataKey="v" name="Value" stroke="#1e3a8a" strokeWidth={2} dot={{ r: 2 }} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}

export function CountBars({ data, height = 160, label }: { data: { x: string; y: number | null; missing?: boolean }[]; height?: number; label: string }) {
  return (
    <div style={{ height }} role="img" aria-label={label}>
      <ResponsiveContainer>
        <BarChart data={data} margin={{ left: 0, right: 8, top: 4, bottom: 4 }}>
          <CartesianGrid vertical={false} stroke="#eef1f5" />
          <XAxis dataKey="x" tick={AXIS} interval="preserveStartEnd" />
          <YAxis tick={AXIS} width={40} />
          <Tooltip />
          <Bar dataKey="y" name="Rows">
            {data.map((d) => <Cell key={d.x} fill={d.missing ? NEG : "#1e3a8a"} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
