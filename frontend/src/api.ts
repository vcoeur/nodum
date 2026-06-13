// Same-origin JSON client for the nodum API. Requests send the session cookie
// (credentials: same-origin); a 401 on any data call invokes the registered
// handler so the app can drop back to the sign-in view. The explicit auth calls
// below bypass that handler so a wrong password reads as a local error, not a
// session expiry.

import type { SessionInfo } from "./types";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

let unauthorizedHandler: (() => void) | null = null;

/** Register a callback fired whenever a data request returns 401. */
export function setUnauthorizedHandler(handler: () => void): void {
  unauthorizedHandler = handler;
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const options: RequestInit = { method, headers, credentials: "same-origin" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  if (response.status === 401) {
    if (unauthorizedHandler) unauthorizedHandler();
    throw new ApiError("Authentication required", 401);
  }
  const raw = await response.text();
  let payload: unknown = null;
  if (raw) {
    try {
      payload = JSON.parse(raw);
    } catch {
      payload = null;
    }
  }
  if (!response.ok) {
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? String((payload as { detail: unknown }).detail)
        : `${response.status} ${response.statusText}`;
    throw new ApiError(detail, response.status);
  }
  return payload as T;
}

export const apiGet = <T>(url: string): Promise<T> => request<T>("GET", url);
export const apiSend = <T>(method: string, url: string, body?: unknown): Promise<T> =>
  request<T>(method, url, body);

/** Read whether a password is configured and whether this caller is authenticated. */
export async function getSession(): Promise<SessionInfo> {
  return apiGet<SessionInfo>("/auth/session");
}

/** Sign in with the main password; sets the session cookie. Throws on a wrong password. */
export async function login(password: string): Promise<void> {
  const response = await fetch("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ password }),
  });
  if (response.ok) return;
  let detail = `Sign-in failed (${response.status}).`;
  try {
    const body = await response.json();
    if (body && body.detail) detail = String(body.detail);
  } catch {
    // keep the default message
  }
  throw new ApiError(detail, response.status);
}

/** Clear the session cookie. Best-effort. */
export async function logout(): Promise<void> {
  try {
    await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
  } catch {
    // ignore — we redirect to the login view regardless
  }
}
