"use client";

import { useEffect, useRef, useState } from "react";

import { api, ApiError } from "@/lib/api";
import type { ImportBatch } from "@/lib/types";
import { useReview } from "@/components/ReviewProvider";

const PROBLEM_LABEL: Record<string, string> = {
  missing_required: "Missing a required value",
  duplicate_in_sheet: "Duplicate row in this sheet",
  already_imported: "Already in the catalogue",
  bad_price: "Price could not be read",
  unknown_category: "Category not recognised — kept for review",
};

export default function ImportPage() {
  const { stores, storeId, notify, refreshCounts, liveTick } = useReview();
  const fileInput = useRef<HTMLInputElement>(null);

  const [target, setTarget] = useState<number | undefined>(storeId);
  const [contract, setContract] = useState<{ required: string[]; optional: string[] } | null>(null);
  const [batches, setBatches] = useState<ImportBatch[]>([]);
  const [latest, setLatest] = useState<ImportBatch | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.sheetTemplate().then(setContract).catch(() => undefined);
  }, []);

  useEffect(() => {
    api.importBatches(storeId).then((p) => setBatches(p.results)).catch(() => undefined);
  }, [storeId, latest, liveTick]);

  useEffect(() => setTarget(storeId), [storeId]);

  async function upload(event: React.FormEvent) {
    event.preventDefault();
    const file = fileInput.current?.files?.[0];
    if (!file || !target) return;

    setBusy(true);
    try {
      const batch = await api.uploadSheet(file, target);
      setLatest(batch);
      notify(`${batch.created_count} product(s) created, ${batch.skipped_count} skipped.`);
      refreshCounts();
      if (fileInput.current) fileInput.current.value = "";
    } catch (err) {
      notify(err instanceof ApiError ? err.text : "Upload failed.", "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <form className="report" onSubmit={upload}>
        <h3>Upload a product sheet</h3>
        <p style={{ color: "var(--muted)", marginTop: 0, fontSize: 13 }}>
          Creates products with every flag pending. Nothing is generated and no model is
          called — enrichment begins only once a category is approved.
        </p>

        <div className="toolbar">
          <select
            value={target ?? ""}
            onChange={(e) => setTarget(e.target.value ? Number(e.target.value) : undefined)}
            required
          >
            <option value="">Choose a store…</option>
            {stores.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name_english}
              </option>
            ))}
          </select>
          <input ref={fileInput} type="file" accept=".xlsx,.xls,.csv" required />
          <button className="primary" disabled={busy || !target}>
            {busy ? "Importing…" : "Import"}
          </button>
        </div>

        {contract && (
          <p style={{ fontSize: 12, color: "var(--muted)", margin: 0 }}>
            <strong>Required columns:</strong>{" "}
            {contract.required.map((c) => (
              <code key={c} style={{ marginRight: 4 }}>
                {c}
              </code>
            ))}
            <br />
            <strong style={{ display: "inline-block", marginTop: 6 }}>Optional:</strong>{" "}
            {contract.optional.map((c) => (
              <code key={c} style={{ marginRight: 4 }}>
                {c}
              </code>
            ))}
          </p>
        )}
      </form>

      {latest && <BatchReport batch={latest} />}

      {batches.length > 0 && (
        <div className="report">
          <h3>Recent imports</h3>
          <table>
            <thead>
              <tr>
                <th>File</th>
                <th>Store</th>
                <th>Created</th>
                <th>Skipped</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {batches.map((b) => (
                <tr key={b.id}>
                  <td>{b.filename}</td>
                  <td>{b.store_name}</td>
                  <td>{b.created_count}</td>
                  <td>{b.skipped_count}</td>
                  <td>{new Date(b.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function BatchReport({ batch }: { batch: ImportBatch }) {
  return (
    <div className="report">
      <h3>{batch.filename}</h3>
      <div className="stat-row">
        <div className="stat">
          <b>{batch.total_rows}</b>
          <span>rows read</span>
        </div>
        <div className="stat">
          <b style={{ color: "var(--ok)" }}>{batch.created_count}</b>
          <span>created</span>
        </div>
        <div className="stat">
          <b style={{ color: batch.skipped_count ? "var(--warn)" : undefined }}>
            {batch.skipped_count}
          </b>
          <span>skipped</span>
        </div>
      </div>

      {batch.issues.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Row</th>
              <th>Issue</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {batch.issues.map((issue, i) => (
              <tr key={i}>
                <td>{issue.row}</td>
                <td>{PROBLEM_LABEL[issue.problem] ?? issue.problem}</td>
                <td>
                  <code>{issue.detail}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
