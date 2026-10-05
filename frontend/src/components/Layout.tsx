import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useSession } from "../session";
import { Badge } from "./ui";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/investigations/new", label: "New investigation", roles: ["analyst", "admin"] },
  { to: "/investigations", label: "Investigations", end: true },
  { to: "/evidence", label: "Evidence explorer" },
  { to: "/reviews", label: "Report review" },
  { to: "/catalog", label: "Metric catalog" },
  { to: "/quality", label: "Data quality" },
  { to: "/admin", label: "Administration", roles: ["admin"] },
];

export default function Layout() {
  const { session, logout, switchTenant, role } = useSession();
  const nav = useNavigate();
  if (!session) return null;
  return (
    <div className="flex min-h-full">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2">
        Skip to content
      </a>
      <aside className="hidden w-60 shrink-0 flex-col border-r border-line bg-white md:flex">
        <div className="flex items-center gap-2 px-5 py-4">
          <img src="/favicon.svg" alt="" className="h-7 w-7" />
          <div>
            <div className="text-sm font-semibold leading-tight">Metric Investigator</div>
            <div className="text-[11px] text-muted">Evidence-first metric analysis</div>
          </div>
        </div>
        <nav aria-label="Main" className="flex-1 px-3 py-2">
          {NAV.filter((n) => !n.roles || n.roles.includes(role)).map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                `mb-0.5 block rounded-lg px-3 py-2 text-sm ${isActive ? "bg-brand-soft font-semibold text-brand" : "text-ink hover:bg-slate-50"}`
              }
            >
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-line px-5 py-3 text-[11px] text-muted">
          Category inspiration: Inconvo (not affiliated).
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line bg-white px-4 py-2.5 md:px-6">
          <div className="flex items-center gap-2">
            <label htmlFor="tenant" className="text-xs text-muted">Tenant</label>
            <select
              id="tenant"
              className="rounded-lg border border-line bg-white px-2 py-1 text-sm"
              value={session.active_tenant?.id}
              onChange={async (e) => {
                await switchTenant(e.target.value);
                nav("/");
              }}
            >
              {session.tenants.filter((t) => t.kind !== "eval").map((t) => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </select>
            <Badge tone="blue">{role}</Badge>
            <Badge tone={session.model_mode === "fixture" ? "amber" : "green"} title="Configured model mode">
              {session.model_mode === "fixture" ? "Demo simulation mode" : "Real model mode"}
            </Badge>
          </div>
          <div className="flex items-center gap-3 text-sm">
            <span className="text-muted">{session.user.display_name}</span>
            <button className="rounded-lg px-2 py-1 text-brand hover:bg-brand-soft" onClick={() => logout().then(() => nav("/login"))}>
              Sign out
            </button>
          </div>
          <nav aria-label="Mobile" className="flex w-full gap-1 overflow-x-auto md:hidden">
            {NAV.filter((n) => !n.roles || n.roles.includes(role)).map((n) => (
              <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => `whitespace-nowrap rounded px-2 py-1 text-xs ${isActive ? "bg-brand-soft text-brand" : "text-muted"}`}>
                {n.label}
              </NavLink>
            ))}
          </nav>
        </header>
        <main id="main" className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 md:px-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
