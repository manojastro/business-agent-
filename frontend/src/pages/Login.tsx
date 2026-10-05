import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { Button, ErrorNote } from "../components/ui";
import { useSession } from "../session";

export default function Login() {
  const { session, login } = useSession();
  const nav = useNavigate();
  const loc = useLocation() as { state?: { from?: string } };
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  if (session) return <Navigate to={loc.state?.from ?? "/"} replace />;

  return (
    <div className="flex min-h-full items-center justify-center px-4">
      <form
        className="w-full max-w-sm rounded-2xl border border-line bg-white p-6 shadow-sm"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          try {
            await login(email, password);
            nav(loc.state?.from ?? "/", { replace: true });
          } catch (err) {
            setError(err);
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="mb-5 flex items-center gap-2">
          <img src="/favicon.svg" alt="" className="h-8 w-8" />
          <div>
            <h1 className="text-lg font-semibold">Metric Investigator</h1>
            <p className="text-xs text-muted">Sign in to investigate metric changes</p>
          </div>
        </div>
        <label className="block text-sm font-medium" htmlFor="email">Email</label>
        <input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)}
          className="mt-1 mb-3 w-full rounded-lg border border-line px-3 py-2 text-sm" />
        <label className="block text-sm font-medium" htmlFor="password">Password</label>
        <input id="password" type="password" autoComplete="current-password" required value={password}
          onChange={(e) => setPassword(e.target.value)} className="mt-1 mb-4 w-full rounded-lg border border-line px-3 py-2 text-sm" />
        <ErrorNote error={error} />
        <Button variant="primary" type="submit" disabled={busy} className="mt-3 w-full py-2">
          {busy ? "Signing in…" : "Sign in"}
        </Button>
        <p className="mt-4 text-xs text-muted">
          Demo accounts are created by <code>python -m app.cli seed-demo</code>, which prints their passwords once.
        </p>
      </form>
    </div>
  );
}
