"use client";

import { useEffect, useRef, useState } from "react";

import { api } from "./api";

export type LiveStatus = "connecting" | "live" | "reconnecting";

interface LiveEvents {
  /** Bumps on every message received. Drop it into a `useQueue` deps array (or any
   *  effect's deps) to have that data quietly refetch when something changed. */
  tick: number;
  status: LiveStatus;
}

/**
 * One SSE connection for the whole app shell, reusing the "pipeline_events" doorbell the
 * backend already rings on every decision (see pipeline/streaming.py). A message never
 * carries what changed — only that something did — so the only thing this hook does with
 * one is bump `tick`; callers respond by re-calling the same REST endpoints they already
 * call on load.
 *
 * `EventSource` retries forever on its own by default, including against a dead session,
 * which would otherwise hammer the endpoint indefinitely from a logged-out tab. `onerror`
 * confirms the session is still real before letting that continue; `onAuthLost` hands the
 * decision of what to do about a dead one back to the caller (ReviewProvider already owns
 * the redirect-to-login behavior for that).
 */
export function useLiveEvents(enabled: boolean, onAuthLost?: () => void): LiveEvents {
  const [tick, setTick] = useState(0);
  const [status, setStatus] = useState<LiveStatus>("connecting");
  const everConnected = useRef(false);

  useEffect(() => {
    if (!enabled) return;

    everConnected.current = false;
    setStatus("connecting");
    const source = new EventSource("/api/events/", { withCredentials: true });

    source.onopen = () => {
      everConnected.current = true;
      setStatus("live");
    };

    source.onmessage = () => setTick((t) => t + 1);

    source.onerror = () => {
      setStatus(everConnected.current ? "reconnecting" : "connecting");
      api.me().catch(() => {
        source.close();
        onAuthLost?.();
      });
    };

    return () => source.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);

  return { tick, status };
}
