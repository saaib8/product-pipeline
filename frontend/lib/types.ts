export type ReviewStatus =
  | "PENDING"
  | "IN_REVIEW"
  | "APPROVED"
  | "REJECTED"
  | "FAILED"
  | "GRANDFATHERED"
  /** This artifact will never exist for this product, and that is correct — a
   *  treadmill has no floor-plan icon. Keeps PENDING meaning only "queued". */
  | "NOT_APPLICABLE";

export type JobStatus = "PENDING" | "IN_PROGRESS" | "COMPLETED" | "FAILED";

/** Every pipeline flag on a product — what the tabs surface so a reviewer can see
 *  where a row sits without leaving the queue. */
export interface Flags {
  category_status: ReviewStatus;
  dimensions_status: ReviewStatus;
  ingestion_status: JobStatus;
  /** null = ingestion has not run. false = the model looked and found nothing. */
  detection: boolean | null;
  icon_2d_status: ReviewStatus;
  model_3d_status: ReviewStatus;
  has_icon: boolean;
  has_metadata: boolean;
  /** Whether the 2D-icon stage covers this category at all. */
  wants_icon: boolean;
}

export interface ProductBase {
  id: number;
  uuid: string;
  store: number;
  store_name: string;
  name_english: string;
  name_arabic: string;
  image_url: string;
  product_url: string;
  category: string | null;
  is_active: boolean;
  flags: Flags;
  time_created: string;
  time_updated: string;
}

export interface CategoryProduct extends ProductBase {
  allowed_categories: string[];
}

export interface DimensionProduct extends ProductBase {
  length: string | null;
  width: string | null;
  height: string | null;
  dimension_unit: string | null;
}

export interface IconProduct extends ProductBase {
  /** The S3 key. Shown for support/debugging — not loadable by the browser. */
  two_d_icon: string | null;
  /** Browser-loadable URL for the key. Empty when it could not be built. */
  icon_url: string;
  /** Non-empty when automated checks were unhappy but deferred to the reviewer. */
  flagged: string;
}

export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

export interface Store {
  id: number;
  name_english: string;
  name_arabic: string | null;
  provider: string;
}

export interface QueueCounts {
  category: number;
  dimensions: number;
  icon_2d: number;
}

export interface ImportIssue {
  row: number;
  problem: string;
  detail: string;
}

export interface ImportBatch {
  id: number;
  filename: string;
  store: number;
  store_name: string;
  uploaded_by_name: string | null;
  total_rows: number;
  created_count: number;
  skipped_count: number;
  issues: ImportIssue[];
  created_at: string;
}

export interface User {
  id: number;
  username: string;
  is_staff: boolean;
}
