import { useEffect, useState } from "react";
import { api, ApiError, saveToken, savedToken, type Scope } from "./api";
import { Queue } from "./Queue";
import { RunView } from "./RunView";
import { SignIn } from "./SignIn";

function currentRun(): string | null {
  const match = window.location.hash.match(/^#\/runs\/([0-9a-zA-Z_-]+)$/);
  return match ? match[1] : null;
}

export function App() {
  const [token, setToken] = useState(savedToken);
  const [scope, setScope] = useState<Scope | null>(null);
  const [runId, setRunId] = useState(currentRun);

  useEffect(() => {
    const onHashChange = () => setRunId(currentRun());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    if (!token) return;
    api.me(token).then(
      (me) => setScope(me.scope),
      (error) => {
        if (error instanceof ApiError && error.status === 401) signOut();
      },
    );
  }, [token]);

  function signIn(newToken: string, newScope: Scope) {
    saveToken(newToken);
    setToken(newToken);
    setScope(newScope);
  }

  function signOut() {
    saveToken("");
    setToken("");
    setScope(null);
  }

  if (!token) return <SignIn onSignIn={signIn} />;
  if (!scope) return <main className="page">Loading…</main>;

  return (
    <>
      <header className="bar">
        <a className="brand" href="#/">
          Ops Dashboard
        </a>
        <span className="who">{scope === "approver" ? "Approver" : "Viewer"}</span>
        <button className="quiet" onClick={signOut}>
          Sign out
        </button>
      </header>
      <main className="page">
        {runId ? (
          <RunView token={token} scope={scope} runId={runId} key={runId} />
        ) : (
          <Queue token={token} />
        )}
      </main>
    </>
  );
}
