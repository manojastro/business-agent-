import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, ApiError, setCsrf, type SessionInfo } from "./api";

interface SessionCtx {
  session: SessionInfo | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  switchTenant: (id: string) => Promise<void>;
  role: string;
}

const Ctx = createContext<SessionCtx | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [loading, setLoading] = useState(true);

  const apply = (s: SessionInfo | null) => {
    setSession(s);
    setCsrf(s?.csrf_token ?? "");
  };

  useEffect(() => {
    api
      .get<SessionInfo>("/auth/session")
      .then(apply)
      .catch((e) => {
        if (!(e instanceof ApiError && e.status === 401)) console.error(e);
        apply(null);
      })
      .finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    apply(await api.post<SessionInfo>("/auth/login", { email, password }));
  }, []);
  const logout = useCallback(async () => {
    try {
      await api.post("/auth/logout");
    } finally {
      apply(null);
    }
  }, []);
  const switchTenant = useCallback(async (id: string) => {
    apply(await api.post<SessionInfo>("/auth/switch-tenant", { tenant_id: id }));
  }, []);

  return (
    <Ctx.Provider value={{ session, loading, login, logout, switchTenant, role: session?.active_tenant?.role ?? "" }}>
      {children}
    </Ctx.Provider>
  );
}

export function useSession(): SessionCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("SessionProvider missing");
  return c;
}
