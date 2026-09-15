import type { Flags } from "@/lib/types";

type Tone = "ok" | "warn" | "idle";

function tone(value: string): Tone {
  if (["APPROVED", "COMPLETED", "GRANDFATHERED"].includes(value)) return "ok";
  if (["REJECTED", "FAILED"].includes(value)) return "warn";
  return "idle";
}

/** Compact pipeline state. Deliberately shows where a row has NOT got to yet —
 *  "ingest ·" reads as not-started, which is different from a failure. */
export function FlagStrip({ flags }: { flags: Flags }) {
  const detection =
    flags.detection === null ? "·" : flags.detection ? "found" : "none";

  const pills: Array<[string, string, Tone]> = [
    ["cat", short(flags.category_status), tone(flags.category_status)],
    ["dim", short(flags.dimensions_status), tone(flags.dimensions_status)],
    ["ingest", short(flags.ingestion_status), tone(flags.ingestion_status)],
    ["detect", detection, flags.detection === false ? "warn" : flags.detection ? "ok" : "idle"],
  ];

  if (flags.wants_icon) {
    pills.push(["icon", short(flags.icon_2d_status), tone(flags.icon_2d_status)]);
  }
  if (flags.has_metadata) pills.push(["meta", "yes", "ok"]);

  return (
    <div className="flags">
      {pills.map(([label, value, t]) => (
        <span className="flag" data-tone={t} key={label} title={`${label}: ${value}`}>
          {label} {value}
        </span>
      ))}
    </div>
  );
}

/** PENDING is the common case, so it reads as a dot rather than shouting. */
function short(status: string): string {
  return (
    {
      PENDING: "·",
      IN_REVIEW: "review",
      IN_PROGRESS: "running",
      APPROVED: "ok",
      COMPLETED: "ok",
      GRANDFATHERED: "grandf.",
      REJECTED: "rejected",
      FAILED: "failed",
      // Reads as a deliberate absence, not a queue position — this artifact is never
      // coming for this product, and that is the correct outcome.
      NOT_APPLICABLE: "n/a",
    }[status] ?? status.toLowerCase()
  );
}
