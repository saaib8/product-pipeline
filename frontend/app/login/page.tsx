"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { api, ApiError } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.login(username, password);
      router.replace("/review/category");
    } catch (err) {
      setError(err instanceof ApiError ? err.text : "Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="centre">
      <form className="panel" onSubmit={submit}>
        <h1>Zory pipeline</h1>
        <p>Sign in to review products.</p>

        <label className="field">
          <span>Username</span>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </label>

        <label className="field">
          <span>Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>

        {error && <div className="error">{error}</div>}

        <button className="primary" style={{ width: "100%" }} disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
