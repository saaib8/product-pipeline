"use client";

import { useEffect, useRef, useState } from "react";

import { api, ApiError } from "./api";

export type LiveStatus = "connecting" | "live" | "reconnecting";

// A live nudge triggers a full re-fetch, which replaces the whole list in one go —
// fast enough that it can cut off the brief "settled" fade a reviewer's own decision
// just started (see .card[data-settled] in globals.css), snapping a card back to full
// opacity for an instant before it vanishes. Waiting this long first lets that fade
// actually be seen before the swap happens.
const REFRESH_DELAY_MS = 500;

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

    let timer: ReturnType<typeof setTimeout> | undefined;
    source.onmessage = () => {
      clearTimeout(timer);
      timer = setTimeout(() => setTick((t) => t + 1), REFRESH_DELAY_MS);
    };

    source.onerror = () => {
      setStatus(everConnected.current ? "reconnecting" : "connecting");
      // Only a confirmed 401/403 means the session is actually gone — anything else
      // (a 500, a network blip) is a transient failure EventSource already retries on
      // its own, and must NOT be treated as "log the reviewer out".
      api.me().catch((err) => {
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          source.close();
          onAuthLost?.();
        }
      });
    };

    return () => {
      clearTimeout(timer);
      source.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);

  return { tick, status };
}
