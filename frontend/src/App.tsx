// Top-level auth gate. On load it asks the open `GET /auth/session` whether a
// password is configured and whether this caller is authenticated (the HttpOnly
// cookie is unreadable by JS, so the server must tell us). It then shows the
// loading note, the sign-in view, or the authenticated workspace. A 401 from any
// data call (registered handler) drops back to sign-in.

import { useCallback, useEffect, useState } from "react";

import { apiGet, getSession, setUnauthorizedHandler } from "./api";
import { Login } from "./components/Login";
import { Workspace } from "./components/Workspace";
import { indexSchema, SchemaContext } from "./schema";
import type { Schema } from "./types";

type Phase =
  | { state: "loading" }
  | { state: "login"; configured: boolean }
  | { state: "ready"; schema: Schema }
  | { state: "error"; message: string };

export function App() {
  const [phase, setPhase] = useState<Phase>({ state: "loading" });

  const checkSession = useCallback(async () => {
    setPhase({ state: "loading" });
    try {
      const session = await getSession();
      if (!session.authenticated) {
        setPhase({ state: "login", configured: session.configured });
        return;
      }
      const schema = await apiGet<Schema>("/schema");
      setPhase({ state: "ready", schema });
    } catch (error) {
      setPhase({
        state: "error",
        message: error instanceof Error ? error.message : "Failed to load.",
      });
    }
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => setPhase({ state: "login", configured: true }));
    checkSession();
  }, [checkSession]);

  if (phase.state === "loading") {
    return (
      <main className="center-note">
        <p className="status">Loading…</p>
      </main>
    );
  }
  if (phase.state === "error") {
    return (
      <main className="center-note">
        <p className="status">{phase.message}</p>
      </main>
    );
  }
  if (phase.state === "login") {
    return <Login configured={phase.configured} onSignedIn={checkSession} />;
  }

  return (
    <SchemaContext.Provider value={indexSchema(phase.schema)}>
      <Workspace onSignedOut={checkSession} />
    </SchemaContext.Provider>
  );
}
