// Explainer history: saved to the reader's account on the server, with a copy in this browser
// (IndexedDB, one database per account) so the sidebar opens instantly and only changed records are
// downloaded. One record per "conversation" message: the request, generation progress, the spec,
// reading progress and the chat with the explainer's tutor bot.
//
// Writes go to the local copy at once and to the server shortly after (debounced per record), so
// frequent saves (reading progress, chat) cost one request. flushRecord() waits for the server copy.
// Before accounts existed, history lived only in the browser ("explainer-studio" database);
// legacyRecords()/importLegacy() move it into the account once.

import type { ExplainerSpec, GenerateRequest, ProgressStep, SectionResponse, Usage } from "../../../shared/spec";
import { ApiError, apiJSON } from "../auth";
import type { ChatMsg } from "../render/chatbot";
import type { Progress } from "../render/explainer";

export interface ExplainerRecord {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  /** What the user typed or attached, for display. */
  prompt: { text: string; fileName?: string; pages?: number };
  request: GenerateRequest;
  /**
   * "asking": a Quality request waiting for the answer to "What do you want to add to each section?".
   * "deciding": a follow-up whose meaning wasn't clear (or is still being routed): revise the
   * explainer above, or a new one? (Old records may say "choosing"; app.ts turns those into errors.)
   */
  status: "deciding" | "asking" | "generating" | "done" | "error";
  /** Conversation: the id of its first record (absent = this record starts its own conversation). */
  thread?: string;
  /** A follow-up in a conversation: a new version of `baseId`, or a related new explainer after it. add: made with "Add section". */
  followUp?: { kind: "revise" | "new"; baseId: string; add?: true };
  /** Revisions: 2, 3… (the first explainer is version 1). */
  version?: number;
  steps: Partial<Record<ProgressStep, { status: "running" | "done"; detail?: string }>>;
  error?: string;
  spec?: ExplainerSpec;
  progress: Progress;
  chat: ChatMsg[];
  /** Edit chat: the page as generated, kept from the first edit so it can be restored. */
  originalSpec?: ExplainerSpec;
  /** Token usage and cost of the generation (absent for older records). */
  usage?: Usage;
  /** Section tool answers ("Another way", "Search the web"), keyed "<stage id>:<tool>", oldest first. */
  sectionNotes?: Record<string, (SectionResponse & { at: number })[]>;
}

const STORE = "explainers";
const LEGACY_DB = "explainer-studio";
const SAVE_DELAY = 800;
const BATCH = 50;

// ---------- the local copy (IndexedDB) ----------

function openDb(name: string, create: boolean): Promise<IDBDatabase | null> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(name, 1);
    req.onupgradeneeded = (ev) => {
      if (!create && ev.oldVersion === 0) {
        req.transaction?.abort(); // it didn't exist: don't create it just by looking
        return;
      }
      req.result.createObjectStore(STORE, { keyPath: "id" });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => (create ? reject(req.error) : resolve(null));
  });
}

function tx<T>(d: IDBDatabase, mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const r = fn(d.transaction(STORE, mode).objectStore(STORE));
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

let local: IDBDatabase | null = null;
let records = new Map<string, ExplainerRecord>();
const timers = new Map<string, ReturnType<typeof setTimeout>>();
const saving = new Map<string, Promise<void>>();
let onSaveError: (msg: string) => void = () => {};
let userKey = "";

// Records changed here but not yet saved to the account (offline, server down): kept across reloads, so
// a sync re-sends them instead of treating them as deleted elsewhere.
function unsynced(): Set<string> {
  try { return new Set(JSON.parse(localStorage.getItem(userKey) || "[]")); } catch { return new Set(); }
}
function markUnsynced(id: string, pending: boolean) {
  const ids = unsynced();
  if (pending === ids.has(id)) return;
  if (pending) ids.add(id); else ids.delete(id);
  try { localStorage.setItem(userKey, JSON.stringify([...ids])); } catch { /* storage blocked: best effort */ }
}

const localPut = (r: ExplainerRecord): Promise<void> => (local ? tx(local, "readwrite", (s) => s.put(r)).then(() => undefined, () => undefined) : Promise.resolve());

// ---------- opening and syncing ----------

/**
 * Open the signed-in reader's history: the local copy first, then the server's index; records that are
 * new or changed on the server are downloaded, and records deleted elsewhere are dropped.
 */
export async function openHistory(userId: string, onError: (msg: string) => void): Promise<ExplainerRecord[]> {
  closeHistory();
  onSaveError = onError;
  userKey = `explainer-studio:unsynced:${userId}`;
  local = await openDb(`explainer-studio:${userId}`, true).catch(() => null); // private mode: no local copy, still works
  const cached: ExplainerRecord[] = local ? await tx<ExplainerRecord[]>(local, "readonly", (s) => s.getAll()).catch(() => []) : [];
  records = new Map(cached.map((r) => [r.id, r]));
  const index = (await apiJSON<{ records: { id: string; updatedAt: number; status: string }[] }>("/api/explainers", "GET")).records;
  const want = index.filter((x) => { const r = records.get(x.id); return !r || r.updatedAt !== x.updatedAt || r.status !== x.status; }).map((x) => x.id);
  const onServer = new Set(index.map((x) => x.id));
  const pending = unsynced();
  for (const id of [...records.keys()]) {
    if (onServer.has(id)) continue;
    if (pending.has(id)) { flushRecord(id).catch(() => undefined); continue; } // never reached the server: send it now
    records.delete(id);
    if (local) tx(local, "readwrite", (s) => s.delete(id)).catch(() => undefined);
  }
  for (const id of pending) if (onServer.has(id) && records.has(id) && !want.includes(id)) flushRecord(id).catch(() => undefined);
  for (let i = 0; i < want.length; i += BATCH) {
    const got = (await apiJSON<{ records: ExplainerRecord[] }>("/api/explainers/batch", "POST", { ids: want.slice(i, i + BATCH) })).records;
    for (const r of got) { records.set(r.id, r); localPut(r); }
  }
  return listRecords();
}

/** Signing out: forget the records in memory (the local copy stays, keyed by account). */
export function closeHistory() {
  for (const t of timers.values()) clearTimeout(t);
  timers.clear();
  records = new Map();
  local?.close();
  local = null;
}

export function listRecords(): ExplainerRecord[] {
  return [...records.values()].sort((a, b) => b.updatedAt - a.updatedAt);
}

export async function getRecord(id: string): Promise<ExplainerRecord | undefined> {
  return records.get(id) ?? fetchRecord(id);
}

/** The server's current copy (e.g. a generation running there after a reload). */
export async function fetchRecord(id: string): Promise<ExplainerRecord | undefined> {
  try {
    const r = await apiJSON<ExplainerRecord>(`/api/explainers/${encodeURIComponent(id)}`, "GET");
    records.set(r.id, r);
    localPut(r);
    return r;
  } catch {
    return undefined;
  }
}

// ---------- saving ----------

async function upload(id: string): Promise<void> {
  const r = records.get(id);
  if (!r) return;
  try {
    await apiJSON(`/api/explainers/${encodeURIComponent(id)}`, "PUT", r);
    markUnsynced(id, false);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    onSaveError(`Couldn't save to your account: ${msg}`);
    throw e;
  }
}

/**
 * Save a record: to this browser now, to the account shortly after. remote: false keeps it local only
 * (generation progress, which the server records itself).
 */
export function putRecord(r: ExplainerRecord, opts: { remote?: boolean } = {}): Promise<void> {
  records.set(r.id, r);
  const done = localPut(r);
  if (opts.remote === false) return done;
  markUnsynced(r.id, true);
  clearTimeout(timers.get(r.id));
  timers.set(r.id, setTimeout(() => { timers.delete(r.id); flushRecord(r.id).catch(() => undefined); }, SAVE_DELAY));
  return done;
}

/** Send a record's pending save now and wait for the server (one upload per record at a time). */
export function flushRecord(id: string): Promise<void> {
  clearTimeout(timers.get(id));
  timers.delete(id);
  const prev = saving.get(id) ?? Promise.resolve();
  const next = prev.catch(() => undefined).then(() => upload(id));
  saving.set(id, next);
  next.finally(() => { if (saving.get(id) === next) saving.delete(id); }).catch(() => undefined);
  return next;
}

/** Send every pending save (the page is being hidden or closed). */
export function flushAll() {
  for (const id of [...timers.keys()]) flushRecord(id).catch(() => undefined);
}

export async function deleteRecord(id: string): Promise<void> {
  clearTimeout(timers.get(id));
  timers.delete(id);
  records.delete(id);
  markUnsynced(id, false);
  if (local) await tx(local, "readwrite", (s) => s.delete(id)).catch(() => undefined);
  try {
    await apiJSON(`/api/explainers/${encodeURIComponent(id)}`, "DELETE");
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 404)) throw e; // already gone is fine
  }
}

// ---------- history from before accounts ----------

/** Records saved in this browser before accounts existed (none once imported or dismissed). */
export async function legacyRecords(): Promise<ExplainerRecord[]> {
  const d = await openDb(LEGACY_DB, false).catch(() => null);
  if (!d) return [];
  try {
    return d.objectStoreNames.contains(STORE) ? await tx<ExplainerRecord[]>(d, "readonly", (s) => s.getAll()) : [];
  } finally {
    d.close();
  }
}

/** Copy pre-account records into the account, then remove the browser-only copy. Returns how many were saved. */
export async function importLegacy(list: ExplainerRecord[], onProgress: (n: number) => void): Promise<number> {
  let n = 0;
  for (const r of list) {
    // Old records interrupted mid-generation can't resume; they come in as retryable errors.
    const rec: ExplainerRecord = r.status === "generating" || (r.status as string) === "choosing"
      ? { ...r, status: "error", error: "This explainer was interrupted before it finished. Try again." } : r;
    if (records.has(rec.id)) continue;
    records.set(rec.id, rec);
    try {
      await upload(rec.id);
      localPut(rec);
      onProgress(++n);
    } catch {
      records.delete(rec.id);
    }
  }
  if (n === list.length) await discardLegacy();
  return n;
}

export function discardLegacy(): Promise<void> {
  return new Promise((resolve) => {
    const req = indexedDB.deleteDatabase(LEGACY_DB);
    req.onsuccess = req.onerror = req.onblocked = () => resolve();
  });
}
