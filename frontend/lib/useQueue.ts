"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError } from "./api";
import type { Paginated } from "./types";

interface QueueState<T> {
  items: T[];
  total: number;
  loading: boolean;
  hasMore: boolean;
  loadMore: () => void;
  reload: () => void;
  /** Mark a row decided. It fades out in place rather than disappearing, so the grid
   *  doesn't reflow under the reviewer's cursor mid-pass. */
  settle: (id: number) => void;
  settled: Set<number>;
}

export function useQueue<T extends { id: number }>(
  fetchPage: (page: number) => Promise<Paginated<T>>,
  deps: unknown[],
  onError: (message: string) => void,
): QueueState<T> {
  const [items, setItems] = useState<T[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [settled, setSettled] = useState<Set<number>>(new Set());
  const [nonce, setNonce] = useState(0);

  const load = useCallback(
    async (targetPage: number, append: boolean) => {
      setLoading(true);
      try {
        const data = await fetchPage(targetPage);
        setItems((prev) => (append ? [...prev, ...data.results] : data.results));
        setTotal(data.count);
        setHasMore(Boolean(data.next));
      } catch (err) {
        onError(err instanceof ApiError ? err.text : "Could not load the queue.");
      } finally {
        setLoading(false);
      }
    },
    // fetchPage is recreated per render; deps are the real inputs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [...deps, onError],
  );

  useEffect(() => {
    setPage(1);
    setSettled(new Set());
    load(1, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  return {
    items,
    total,
    loading,
    hasMore,
    settled,
    loadMore: () => {
      const next = page + 1;
      setPage(next);
      load(next, true);
    },
    reload: () => setNonce((n) => n + 1),
    settle: (id: number) => setSettled((prev) => new Set(prev).add(id)),
  };
}
