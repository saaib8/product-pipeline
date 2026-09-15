"use client";

import { useEffect, useState } from "react";

export interface ToastState {
  message: string;
  tone?: "info" | "error";
}

/** Transient feedback. Errors linger longer — they usually need reading. */
export function Toast({ toast }: { toast: ToastState | null }) {
  const [shown, setShown] = useState<ToastState | null>(null);

  useEffect(() => {
    if (!toast) return;
    setShown(toast);
    const ms = toast.tone === "error" ? 6000 : 2200;
    const timer = setTimeout(() => setShown(null), ms);
    return () => clearTimeout(timer);
  }, [toast]);

  if (!shown) return null;
  return (
    <div className="toast" data-tone={shown.tone ?? "info"} role="status">
      {shown.message}
    </div>
  );
}
