// The signed-in session, in memory only. The access token (a short-lived JWT) is never written to
// localStorage or a readable cookie, so a script injected into the page can't carry it away; the refresh
// token lives in an httpOnly cookie the server sets, and restore() trades it for a new access token when
// the page loads. authFetch() adds the token to API calls and refreshes it once on a 401.

export interface User { id: string; email: string; plan: string; createdAt: number }
export interface Allowance { plan: string; generationsUsed: number; generationsLimit: number; usdUsed: number; usdLimit: number; resetsAt: number }
interface Session { accessToken: string; expiresAt: number; user: User; allowance: Allowance }

/** An API error: the server's message, plus its code and per-field messages when it sent them. */
export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string, readonly fields?: Record<string, string>) {
    super(message);
  }
}

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
const UNREACHABLE = "Couldn't reach the server. Check your connection and try again.";

let session: Session | null = null;
let refreshing: Promise<Session | null> | null = null;
const listeners = new Set<(u: User | null) => void>();
// Signing out (or in) in one tab tells the others.
const channel = typeof BroadcastChannel === "function" ? new BroadcastChannel("explainer-studio-auth") : null;
channel?.addEventListener("message", (e) => {
  if (e.data === "signed-out" && session) set(null, false);
  if (e.data === "signed-in" && !session) restore();
});

function set(s: Session | null, broadcast = true) {
  const changed = (s?.user.id ?? null) !== (session?.user.id ?? null);
  session = s;
  if (!changed) return;
  if (broadcast) channel?.postMessage(s ? "signed-in" : "signed-out");
  for (const fn of listeners) fn(s?.user ?? null);
}

export const currentUser = (): User | null => session?.user ?? null;
export const allowance = (): Allowance | null => session?.allowance ?? null;

/** Called with the new user (or null) whenever someone signs in or out, in this tab or another. */
export function onAuthChange(fn: (u: User | null) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

async function request(path: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(`${API_BASE}${path}`, { credentials: "include", ...init });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError(UNREACHABLE, 0, "network");
  }
}

async function errorOf(res: Response): Promise<ApiError> {
  const body = await res.json().catch(() => ({}));
  return new ApiError(body.error ?? `Server error (${res.status}).`, res.status, body.code, body.fields);
}

async function sessionCall(path: string, body: unknown, method = "POST"): Promise<Session> {
  const res = await request(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) throw await errorOf(res);
  const s = (await res.json()) as Session;
  set(s);
  return s;
}

export const register = (email: string, password: string) => sessionCall("/api/auth/register", { email, password });
export const login = (email: string, password: string) => sessionCall("/api/auth/login", { email, password });

/** A new access token from the refresh cookie (one request at a time, however many calls ask). Null = signed out. */
export function refresh(): Promise<Session | null> {
  refreshing ??= (async () => {
    try {
      return await sessionCall("/api/auth/refresh", {});
    } catch (e) {
      // A network failure keeps the current session (the next call retries); a refusal ends it.
      if (e instanceof ApiError && e.status === 0) throw e;
      set(null);
      return null;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

/** On page load: resume the session the refresh cookie holds, if any. */
export async function restore(): Promise<User | null> {
  try {
    return (await refresh())?.user ?? null;
  } catch {
    return null;
  }
}

async function token(): Promise<string> {
  // Refresh a little before expiry so a request never arrives with a token that just ran out.
  if (session && session.expiresAt * 1000 - Date.now() > 30_000) return session.accessToken;
  const s = await refresh();
  if (!s) throw new ApiError("Please sign in.", 401, "auth_required");
  return s.accessToken;
}

/** fetch() for the API with the access token; on a 401 the token is refreshed and the call retried once. */
export async function authFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const send = async (t: string) => request(path, { ...init, headers: { ...(init.headers as Record<string, string>), Authorization: `Bearer ${t}` } });
  let res = await send(await token());
  if (res.status === 401) {
    const s = await refresh();
    if (!s) throw new ApiError("Your session has ended. Please sign in again.", 401, "no_session");
    res = await send(s.accessToken);
    if (res.status === 401) {
      set(null);
      throw await errorOf(res);
    }
  }
  return res;
}

/** authFetch a JSON body and read a JSON reply (errors become ApiError with the server's message). */
export async function apiJSON<T>(path: string, method: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const res = await authFetch(path, {
    method, signal,
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (!res.ok) throw await errorOf(res);
  return (await res.json()) as T;
}

/** Today's use of the free plan (after a generation or chat message). */
export async function refreshAllowance(): Promise<Allowance | null> {
  if (!session) return null;
  try {
    const me = await apiJSON<{ user: User; allowance: Allowance }>("/api/auth/me", "GET");
    if (session) session.allowance = me.allowance;
    return me.allowance;
  } catch {
    return session?.allowance ?? null;
  }
}

export async function logout(): Promise<void> {
  try {
    await request("/api/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  } finally {
    set(null);
  }
}

export async function logoutEverywhere(): Promise<void> {
  try {
    await apiJSON("/api/auth/logout-all", "POST", {});
  } finally {
    set(null);
  }
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  const res = await authFetch("/api/auth/password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ currentPassword, newPassword }) });
  if (!res.ok) throw await errorOf(res);
  set((await res.json()) as Session);
}

export async function deleteAccount(password: string): Promise<void> {
  await apiJSON("/api/auth/me", "DELETE", { password });
  set(null);
}

export { errorOf };
