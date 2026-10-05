import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Step } from "./api";

export function useFetch<T>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const load = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    try {
      setData(await api.get<T>(path));
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);
  useEffect(() => {
    void load();
  }, [load]);
  return { data, error, loading, reload: load, setData };
}

export const TERMINAL = new Set(["completed", "rejected", "cancelled", "failed", "insufficient_evidence"]);

/** Live investigation steps over SSE, replaying persisted history; falls back to polling. */
export function useLiveSteps(investigationId: string, onStatus: (s: string) => void) {
  const [steps, setSteps] = useState<Step[]>([]);
  const [mode, setMode] = useState<"sse" | "polling" | "idle">("idle");
  const cursor = useRef(0);
  const statusRef = useRef(onStatus);
  statusRef.current = onStatus;

  useEffect(() => {
    cursor.current = 0;
    setSteps([]);
    let es: EventSource | null = null;
    let timer: number | undefined;
    let stopped = false;

    const push = (s: Step) => {
      if (s.id <= cursor.current) return;
      cursor.current = s.id;
      setSteps((prev) => [...prev, s]);
    };

    const poll = async () => {
      setMode("polling");
      while (!stopped) {
        try {
          const r = await api.get<{ items: Step[]; next_after: number }>(`/investigations/${investigationId}/steps?after=${cursor.current}`);
          r.items.forEach(push);
          if (r.items.length) statusRef.current("refresh");
        } catch {
          /* keep polling */
        }
        await new Promise((res) => (timer = window.setTimeout(res, 2500)));
      }
    };

    if ("EventSource" in window) {
      es = new EventSource(`/api/v1/investigations/${investigationId}/events?after=0`, { withCredentials: true });
      setMode("sse");
      es.addEventListener("step", (ev) => push(JSON.parse((ev as MessageEvent).data)));
      es.addEventListener("status", (ev) => {
        const status = JSON.parse((ev as MessageEvent).data).status as string;
        statusRef.current(status);
        if (TERMINAL.has(status)) {
          // the server closes the stream for finished runs; do not reconnect
          window.setTimeout(() => es?.close(), 4000);
        }
      });
      let errors = 0;
      es.onerror = () => {
        errors += 1;
        if (errors > 3 && es) {
          es.close();
          es = null;
          void poll();
        }
      };
    } else {
      void poll();
    }
    return () => {
      stopped = true;
      es?.close();
      if (timer) window.clearTimeout(timer);
    };
  }, [investigationId]);

  return { steps, mode };
}
