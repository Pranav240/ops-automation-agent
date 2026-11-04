import { useState, type FormEvent } from "react";
import { api, ApiError, type Scope } from "./api";

export function SignIn({ onSignIn }: { onSignIn: (token: string, scope: Scope) => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const me = await api.me(token);
      onSignIn(token, me.scope);
    } catch (e) {
      setError(e instanceof ApiError && e.status === 401 ? "That token was not accepted." : "Could not reach the API.");
    }
  }

  return (
    <main className="signin">
      <h1>Ops Dashboard</h1>
      <form onSubmit={submit}>
        <label htmlFor="token">Access token</label>
        <input
          id="token"
          type="password"
          autoComplete="off"
          value={token}
          onChange={(event) => setToken(event.target.value)}
        />
        <button type="submit" disabled={!token}>
          Sign in
        </button>
        {error && <p role="alert">{error}</p>}
      </form>
    </main>
  );
}
