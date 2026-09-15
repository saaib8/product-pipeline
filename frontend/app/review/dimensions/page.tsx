"use client";

import { useCallback, useState } from "react";

import { FlagStrip } from "@/components/Flags";
import { ProductThumb } from "@/components/ProductThumb";
import { api, ApiError, type Decision } from "@/lib/api";
import type { DimensionProduct } from "@/lib/types";
import { useQueue } from "@/lib/useQueue";
import { useReview } from "@/components/ReviewProvider";

export default function DimensionReviewPage() {
  const { storeId, notify, bumpCount, refreshCounts } = useReview();

  const fetchPage = useCallback(
    (page: number) => api.dimensionQueue(storeId, page),
    [storeId],
  );
  const queue = useQueue<DimensionProduct>(fetchPage, [storeId], (m) => notify(m, "error"));

  if (!queue.loading && queue.items.length === 0) {
    return (
      <div className="empty">
        <h2>Nothing to review</h2>
        <p>Every set of dimensions in this scope has been decided.</p>
      </div>
    );
  }

  return (
    <>
      <div className="toolbar">
        <strong>{queue.total}</strong>
        <span className="toolbar-note">
          awaiting a dimension decision. This starts no pipeline work — dimensions are
          checked when the layout feature reads the catalogue.
        </span>
      </div>

      <div className="grid">
        {queue.items.map((product) => (
          <DimensionCard
            key={product.id}
            product={product}
            settled={queue.settled.has(product.id)}
            onSettled={() => {
              queue.settle(product.id);
              bumpCount("dimensions", -1);
            }}
            onError={(m) => notify(m, "error")}
            onStale={(m) => notify(m, "info")}
            onDone={refreshCounts}
          />
        ))}
      </div>

      {queue.hasMore && (
        <div style={{ textAlign: "center", marginTop: 20 }}>
          <button onClick={queue.loadMore} disabled={queue.loading}>
            {queue.loading ? "Loading…" : "Load more"}
          </button>
        </div>
      )}
    </>
  );
}

const UNITS = ["cm", "in", "ft", "m"];

function DimensionCard({
  product,
  settled,
  onSettled,
  onError,
  onStale,
  onDone,
}: {
  product: DimensionProduct;
  settled: boolean;
  onSettled: () => void;
  onError: (message: string) => void;
  onStale: (message: string) => void;
  onDone: () => void;
}) {
  const [length, setLength] = useState(product.length ?? "");
  const [width, setWidth] = useState(product.width ?? "");
  const [height, setHeight] = useState(product.height ?? "");
  const [unit, setUnit] = useState(product.dimension_unit || "cm");
  const [busy, setBusy] = useState(false);

  // The layout engine needs positive length AND width; the API refuses an approval
  // without them, so disable rather than let the reviewer hit a server error.
  const approvable = Number(length) > 0 && Number(width) > 0;

  const edited =
    length !== (product.length ?? "") ||
    width !== (product.width ?? "") ||
    height !== (product.height ?? "") ||
    unit !== (product.dimension_unit || "cm");

  async function decide(decision: Decision) {
    setBusy(true);
    try {
      await api.decideDimensions(product.id, {
        decision,
        ...(decision === "APPROVED" && edited
          ? {
              length: String(length),
              width: String(width),
              ...(height !== "" ? { height: String(height) } : {}),
              dimension_unit: unit,
            }
          : {}),
      });
      onSettled();
      onDone();
    } catch (err) {
      // Another reviewer decided this row first — retire the card rather than leaving
      // one that can only fail again.
      if (err instanceof ApiError && err.isStale) {
        onStale(`“${product.name_english}” was already decided by someone else.`);
        onSettled();
        onDone();
        return;
      }
      onError(err instanceof ApiError ? err.text : "Could not save that decision.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="card" data-settled={settled} data-busy={busy}>
      <ProductThumb src={product.image_url} alt={product.name_english} />

      <div className="card-title" title={product.name_english}>
        {product.name_english}
      </div>

      <div className="card-meta">
        <span>{product.store_name}</span>
        {product.category && <code>{product.category}</code>}
        {product.product_url && (
          <a href={product.product_url} target="_blank" rel="noreferrer" style={{ color: "var(--accent)" }}>
            source ↗
          </a>
        )}
        {!product.is_active && <span className="inactive-pill">inactive</span>}
      </div>

      <div className="dims">
        <label>
          <span>Length</span>
          <input
            type="number"
            min="0"
            step="0.01"
            value={length}
            onChange={(e) => setLength(e.target.value)}
          />
        </label>
        <label>
          <span>Width</span>
          <input
            type="number"
            min="0"
            step="0.01"
            value={width}
            onChange={(e) => setWidth(e.target.value)}
          />
        </label>
        <label>
          <span>Height</span>
          <input
            type="number"
            min="0"
            step="0.01"
            value={height}
            onChange={(e) => setHeight(e.target.value)}
          />
        </label>
      </div>

      <label className="field" style={{ marginBottom: 0 }}>
        <span>Unit</span>
        <select value={unit} onChange={(e) => setUnit(e.target.value)}>
          {UNITS.map((u) => (
            <option key={u} value={u}>
              {u}
            </option>
          ))}
        </select>
      </label>

      <FlagStrip flags={product.flags} />

      <div className="card-actions">
        <button
          className="primary"
          onClick={() => decide("APPROVED")}
          disabled={busy || !approvable}
          title={approvable ? undefined : "Length and width must both be greater than zero"}
        >
          {edited ? "Fix & approve" : "Approve"}
        </button>
        <button className="danger" onClick={() => decide("REJECTED")} disabled={busy}>
          Reject
        </button>
      </div>
    </article>
  );
}
