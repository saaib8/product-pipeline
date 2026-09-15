"use client";

import { useCallback, useState } from "react";

import { FlagStrip } from "@/components/Flags";
import { ProductThumb } from "@/components/ProductThumb";
import { api, ApiError, type Decision } from "@/lib/api";
import type { IconProduct } from "@/lib/types";
import { useQueue } from "@/lib/useQueue";
import { useReview } from "@/components/ReviewProvider";

export default function IconReviewPage() {
  const { storeId, notify, bumpCount, refreshCounts } = useReview();

  const fetchPage = useCallback((page: number) => api.iconQueue(storeId, page), [storeId]);
  const queue = useQueue<IconProduct>(fetchPage, [storeId], (m) => notify(m, "error"));

  if (!queue.loading && queue.items.length === 0) {
    return (
      <div className="empty">
        <h2>No icons waiting</h2>
        <p>
          Icons appear here once the worker has drawn them. A product only reaches this
          queue after its category is approved and that category is one the icon stage
          covers.
        </p>
      </div>
    );
  }

  return (
    <>
      <div className="toolbar">
        <strong>{queue.total}</strong>
        <span className="toolbar-note">
          awaiting an icon decision. Compare the icon against the photo it was drawn
          from — rejecting keeps the product but bars it from layouts, and there is no
          regeneration.
        </span>
      </div>

      <div className="grid">
        {queue.items.map((product) => (
          <IconCard
            key={product.id}
            product={product}
            settled={queue.settled.has(product.id)}
            onSettled={() => {
              queue.settle(product.id);
              bumpCount("icon_2d", -1);
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

function IconCard({
  product,
  settled,
  onSettled,
  onError,
  onStale,
  onDone,
}: {
  product: IconProduct;
  settled: boolean;
  onSettled: () => void;
  onError: (message: string) => void;
  onStale: (message: string) => void;
  onDone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  // Rejecting is final — no regeneration, and the decision cannot be reopened — so it
  // takes two clicks. The second click is the one that sends.
  const [confirming, setConfirming] = useState(false);

  async function decide(decision: Decision) {
    setBusy(true);
    try {
      await api.decideIcon(product.id, { decision });
      onSettled();
      onDone();
    } catch (err) {
      // A colleague got there first. The row is decided either way, so retire it
      // instead of leaving a card that can only fail again.
      if (err instanceof ApiError && err.isStale) {
        onStale(`“${product.name_english}” was already decided by someone else.`);
        onSettled();
        onDone();
        return;
      }
      onError(err instanceof ApiError ? err.text : "Could not save that decision.");
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="card" data-settled={settled} data-busy={busy}>
      {/* The judgement is a comparison, so both images are always on screen together. */}
      <div className="compare">
        <figure>
          <ProductThumb src={product.image_url} alt={`${product.name_english} photo`} />
          <figcaption>photo</figcaption>
        </figure>
        <figure>
          <ProductThumb src={product.icon_url} alt={`${product.name_english} icon`} />
          <figcaption>generated icon</figcaption>
        </figure>
      </div>

      <div className="card-title" title={product.name_english}>
        {product.name_english}
      </div>

      <div className="card-meta">
        <span>{product.store_name}</span>
        {product.category && <span>{product.category}</span>}
        {product.product_url && (
          <a
            href={product.product_url}
            target="_blank"
            rel="noreferrer"
            style={{ color: "var(--accent)" }}
          >
            source ↗
          </a>
        )}
      </div>

      {product.two_d_icon && (
        <div className="card-meta" style={{ fontFamily: "monospace", fontSize: 11 }}>
          {product.two_d_icon}
        </div>
      )}

      {/* Automated checks rejected every attempt but deferred rather than discarding.
          Usually a pale product on white, which trips the same contrast check as a
          blank frame — the reviewer is the one who can tell those apart. */}
      {product.flagged && (
        <div className="card-meta" style={{ color: "var(--warn)" }}>
          ⚠ {product.flagged.replace(/^flagged:\s*/, "")} — check it looks right
        </div>
      )}

      {/* An icon that failed to resolve cannot be judged — say so instead of showing an
          empty box the reviewer might read as a blank icon and reject. */}
      {!product.icon_url && (
        <div className="card-meta" style={{ color: "var(--warn)" }}>
          the icon could not be loaded — check storage before deciding
        </div>
      )}

      <FlagStrip flags={product.flags} />

      {confirming ? (
        <>
          <div className="card-meta" style={{ color: "var(--warn)" }}>
            The product stays active and listed, but cannot be placed in a layout
            without an approved icon. This cannot be regenerated or undone.
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
          <button
            className="primary"
            onClick={() => decide("APPROVED")}
            disabled={busy || !product.two_d_icon}
          >
            Approve
          </button>
          <button className="danger" onClick={() => setConfirming(true)} disabled={busy}>
            Reject
          </button>
        </div>
      )}
    </article>
  );
}
