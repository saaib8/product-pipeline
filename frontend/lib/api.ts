/**
 * API client.
 *
 * Requests go to /api/*, which next.config.mjs proxies to Django. Because the browser
 * sees a single origin, the session cookie needs no special handling — but Django still
 * wants a CSRF token on writes, so `post` reads it from the cookie and sends the header.
 */

import type {
  CategoryProduct,
  DimensionProduct,
  IconProduct,
  ImportBatch,
  Paginated,
  QueueCounts,
  Store,
  User,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    /** Field-keyed messages from DRF, so a form can show them in place. */
    public detail: Record<string, unknown> | string,
  ) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
  }

  /** Another reviewer decided this row while the page was open. Not a failure —
   *  the row is simply gone, and the UI should retire it rather than shout. */
  get isStale(): boolean {
    return this.status === 409;
  }

  /** A readable one-liner for a toast. */
  get text(): string {
    if (typeof this.detail === "string") return this.detail;
    const parts = Object.entries(this.detail).map(([k, v]) =>
      k === "detail" ? String(v) : `${k}: ${Array.isArray(v) ? v.join(" ") : v}`,
    );
    return parts.join(" · ") || `Request failed (${this.status})`;
  }
}

function cookie(name: string): string {
  const match = document.cookie.match(new RegExp(`(^|;\\s*)${name}=([^;]*)`));
  return match ? decodeURIComponent(match[2]) : "";
}

async function parse(res: Response) {
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, body);
  return body;
}

async function get(path: string) {
  return parse(await fetch(`/api${path}`, { credentials: "include" }));
}

async function post(path: string, body?: unknown, isForm = false) {
  // Django sets csrftoken on any GET; ensure we have one before the first write.
  if (!cookie("csrftoken")) await fetch("/api/auth/csrf/", { credentials: "include" });

  return parse(
    await fetch(`/api${path}`, {
      method: "POST",
      credentials: "include",
      headers: {
        "X-CSRFToken": cookie("csrftoken"),
        ...(isForm ? {} : { "Content-Type": "application/json" }),
      },
      body: isForm ? (body as FormData) : JSON.stringify(body ?? {}),
    }),
  );
}

function query(params: Record<string, string | number | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

export type Decision = "APPROVED" | "REJECTED";

export const api = {
  // auth
  me: (): Promise<User> => get("/auth/me/"),
  login: (username: string, password: string): Promise<User> =>
    post("/auth/login/", { username, password }),
  logout: (): Promise<null> => post("/auth/logout/"),

  // reference data
  vocabulary: (): Promise<{ categories: string[]; stores: Store[] }> => get("/vocabulary/"),
  counts: (store?: number): Promise<QueueCounts> => get(`/review/counts/${query({ store })}`),

  // queues
  categoryQueue: (store?: number, page = 1): Promise<Paginated<CategoryProduct>> =>
    get(`/review/category/${query({ store, page })}`),
  dimensionQueue: (store?: number, page = 1): Promise<Paginated<DimensionProduct>> =>
    get(`/review/dimensions/${query({ store, page })}`),
  iconQueue: (store?: number, page = 1): Promise<Paginated<IconProduct>> =>
    get(`/review/icons/${query({ store, page })}`),

  // decisions
  decideCategory: (
    id: number,
    payload: { decision: Decision; category?: string; note?: string },
  ): Promise<CategoryProduct> => post(`/review/category/${id}/decide/`, payload),

  decideDimensions: (
    id: number,
    payload: {
      decision: Decision;
      length?: string;
      width?: string;
      height?: string;
      dimension_unit?: string;
      note?: string;
    },
  ): Promise<DimensionProduct> => post(`/review/dimensions/${id}/decide/`, payload),

  /** Rejecting marks only the icon: the product stays active, but loses layout
   *  readiness. There is no regeneration, and the decision cannot be reopened. */
  decideIcon: (
    id: number,
    payload: { decision: Decision; note?: string },
  ): Promise<IconProduct> => post(`/review/icons/${id}/decide/`, payload),

  // import
  uploadSheet: (file: File, store: number): Promise<ImportBatch> => {
    const form = new FormData();
    form.append("file", file);
    form.append("store", String(store));
    return post("/import/", form, true);
  },
  importBatches: (store?: number): Promise<Paginated<ImportBatch>> =>
    get(`/import/batches/${query({ store })}`),
  sheetTemplate: (): Promise<{ required: string[]; optional: string[] }> =>
    get("/import/template/"),
};
