"use client";

import { useState } from "react";

/** Product image with an explicit broken state.
 *
 * A dead image URL is a real and common data problem — the reviewer needs to see that
 * it's the *data* that's broken, not the page, because that's grounds to reject. */
export function ProductThumb({ src, alt }: { src: string; alt: string }) {
  const [failed, setFailed] = useState(false);

  if (!src || failed) {
    return (
      <div className="thumb thumb-missing" role="img" aria-label={`${alt} — image unavailable`}>
        image unavailable
      </div>
    );
  }

  return (
    // Plain <img>: these are arbitrary merchant URLs on domains we don't control, so
    // Next's image optimiser has nothing to offer here.
    // eslint-disable-next-line @next/next/no-img-element
    <img className="thumb" src={src} alt={alt} loading="lazy" onError={() => setFailed(true)} />
  );
}
