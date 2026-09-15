"use client";

import { useCallback, useState } from "react";

import { FlagStrip } from "@/components/Flags";
import { ProductThumb } from "@/components/ProductThumb";
import { api, ApiError, type Decision } from "@/lib/api";
import type { CategoryProduct } from "@/lib/types";
import { useQueue } from "@/lib/useQueue";
import { useReview } from "@/components/ReviewProvider";

export default function CategoryReviewPage() {
  const { storeId, categories, notify, bumpCount, refreshCounts } = useReview();

  const fetchPage = useCallback(
    (page: number) => api.categoryQueue(storeId, page),
    [storeId],
  );
  const queue = useQueue<CategoryProduct>(fetchPage, [storeId], (m) => notify(m, "error"));

  if (!queue.loading && queue.items.length === 0) {
    return (
      <div className="empty">
        <h2>Nothing to review</h2>
        <p>Every category in this scope has been decided. Import a sheet to add more.</p>
      </div>
    );
  }

  return (
    <>
      <div className="toolbar">
        <strong>{queue.total}</strong>
        <span className="toolbar-note">
          awaiting a category decision. Approving one makes the product eligible for
          ingestion, icon generation and metadata.
        </span>
      </div>

      <div className="grid">
        {queue.items.map((product) => (
          <CategoryCard
            key={product.id}
            product={product}
            categories={categories}
            settled={queue.settled.has(product.id)}
            onSettled={() => {
              queue.settle(product.id);
              bumpCount("category", -1);
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

function CategoryCard({
  product,
  categories,
  settled,
  onSettled,
  onError,
  onStale,
  onDone,
}: {
  product: CategoryProduct;
  categories: string[];
  settled: boolean;
  onSettled: () => void;
  onError: (message: string) => void;
  onStale: (message: string) => void;
  onDone: () => void;
}) {
  // The scraped category may not be in the vocabulary at all — import keeps those
  // rather than dropping them, so the select must not silently coerce it to something
  // valid-looking. An empty value forces a deliberate choice.
  const known = product.category ? categories.includes(product.category) : false;
  const [choice, setChoice] = useState(known ? product.category! : "");
  const [busy, setBusy] = useState(false);
  // Rejecting deactivates the product everywhere, so it takes two clicks. The icon
  // queue uses the same guard, but for a narrower reason: a rejected icon only bars
  // the product from layouts, it does not retire it.
  const [confirming, setConfirming] = useState(false);

  const changed = choice !== product.category;

  async function decide(decision: Decision) {
    setBusy(true);
    try {
      await api.decideCategory(product.id, {
        decision,
        // Only send a category when correcting; approving as-is keeps the audit clean.
        ...(decision === "APPROVED" && changed && choice ? { category: choice } : {}),
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
        {product.product_url && (
          <a href={product.product_url} target="_blank" rel="noreferrer" style={{ color: "var(--accent)" }}>
            source ↗
          </a>
        )}
        {!product.is_active && <span className="inactive-pill">inactive</span>}
      </div>

      {!known && product.category && (
        <div className="card-meta" style={{ color: "var(--warn)" }}>
          scraped “{product.category}” is not a known category
        </div>
      )}

      <select
        value={choice}
        onChange={(e) => setChoice(e.target.value)}
        aria-label={`Category for ${product.name_english}`}
      >
        <option value="">— choose a category —</option>
        {categories.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>

      <FlagStrip flags={product.flags} />

      {confirming ? (
        <>
          <div className="card-meta" style={{ color: "var(--warn)" }}>
            Rejecting deactivates this product everywhere — it leaves every queue and no
            stage will process it.
          </div>
          <div className="card-actions">
            <button onClick={() => setConfirming(false)} disabled={busy}>
              Cancel
            </button>
            <button className="danger" onClick={() => decide("REJECTED")} disabled={busy}>
              Confirm reject
            </button>
          </div>
        </>
      ) : (
        <div className="card-actions">
          <button className="primary" onClick={() => decide("APPROVED")} disabled={busy || !choice}>
            {changed && choice ? "Fix & approve" : "Approve"}
          </button>
          <button className="danger" onClick={() => setConfirming(true)} disabled={busy}>
            Reject
          </button>
        </div>
      )}
    </article>
  );
}
