// Calls to our server (never to Anthropic directly; the key lives server-side). Every call is signed in:
// authFetch adds the access token and refreshes it when it runs out.

import type { ChatRequest, EditRequest, EditResponse, FeedbackRun, FollowUp, FollowUpTarget, FormulaRequest, FormulaResponse, GenerateEvent, GenerateRequest, RouteRequest, RouteResponse, SectionRequest, SectionResponse } from "../../shared/spec";
import { apiJSON, authFetch, errorOf } from "./auth";

/** POST JSON and read a text/event-stream response, calling onData per event. */
async function postSSE(path: string, body: unknown, onData: (data: any) => void, signal?: AbortSignal) {
  const res = await authFetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
  if (!res.ok || !res.body) throw await errorOf(res);
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value;
    let cut;
    while ((cut = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      for (const line of chunk.split("\n")) if (line.startsWith("data: ")) onData(JSON.parse(line.slice(6)));
    }
  }
}

/** recordId: the history record to save progress and the result to (on the server, so it lands even if the tab closes). */
/** The stream broke after the server accepted the generation: it carries on there, so the record can be polled. */
export class ConnectionLost extends Error {
  constructor() { super("The connection closed before the explainer was finished."); }
}

/** recordId: the history record to save progress and the result to (on the server, so it lands even if the tab closes). */
export async function generateExplainer(req: GenerateRequest, onEvent: (e: GenerateEvent) => void, signal?: AbortSignal, recordId?: string) {
  let started = false;
  let finished = false;
  try {
    await postSSE("/api/generate", recordId ? { ...req, recordId } : req, (e: GenerateEvent) => {
      started = true;
      if (e.type === "done" || e.type === "error") finished = true;
      onEvent(e);
    }, signal);
  } catch (e) {
    if ((e as Error).name === "AbortError" || !started) throw e;
    throw new ConnectionLost();
  }
  if (!finished) {
    if (recordId) throw new ConnectionLost();
    onEvent({ type: "error", message: "The connection closed before the explainer was finished. Please try again." });
  }
}

export async function chat(req: ChatRequest, onText: (t: string) => void, signal?: AbortSignal) {
  let error: string | null = null;
  await postSSE("/api/chat", req, (d: { t?: string; error?: string }) => {
    if (d.t) onText(d.t);
    if (d.error) error = d.error;
  }, signal);
  if (error) throw new Error(error);
}

const postJSON = <T>(path: string, body: unknown, signal?: AbortSignal) => apiJSON<T>(path, "POST", body, signal);

/** A follow-up in a conversation: does it revise the explainer above, or ask for a new one? */
export const routeFollowUp = (req: RouteRequest, signal?: AbortSignal) => postJSON<RouteResponse>("/api/route", req, signal);

/** One Edit chat turn: Haiku's reply and tool calls (the browser runs the tools). */
export const editChat = (req: EditRequest, signal?: AbortSignal) => postJSON<EditResponse>("/api/edit", req, signal);

/** A section tool the reader clicked ("Another way", "Search the web"): one Haiku call, run only on the click. */
export const sectionTool = (req: SectionRequest, signal?: AbortSignal) => postJSON<SectionResponse>("/api/section", req, signal);

/** "Go deeper" on a formula: one Haiku call, run only on the first click (the note is then saved on the page). */
export const formulaDeeper = (req: FormulaRequest, signal?: AbortSignal) => postJSON<FormulaResponse>("/api/formula", req, signal);

/** A preference the reader saved through Feedback; active ones shape their future pages. */
export interface Preference { id: string; text: string; topic: string; status: "active" | "off"; createdAt: number }
/**
 * POST /api/feedback. followUp: the page as a revise follow-up (the server reads its sections and widgets from it).
 * warnings: the page's build log (spec.warnings). answer + round 2: the reader's own answer to the plan's question.
 */
export interface FeedbackRequest {
  text: string; recordId?: string; title: string; warnings: string[]; followUp: FollowUp; quality?: string;
  answer?: string; round?: 1 | 2;
  /** What the feedback that made this version changed, so "the play button" can mean the one it just added. */
  lastChange?: { stages: string[]; what: string };
}
/** A change in the plan, with the section's label for the card ("New section after …" for an added one). */
export type FeedbackChange = FollowUpTarget & { stageLabel: string };
/** A chip under the plan's question: one concrete change, or part "page" for the whole page. */
export interface FeedbackOption { label: string; part: FollowUpTarget["part"] | "page"; stage: string; after: string; what: string }
/**
 * What the app understood (nothing has changed yet). question: asked when it couldn't tell which part is meant.
 * wholeOnly: this page can only be changed as a whole. unmatched: even the answer didn't point at a part.
 */
export interface FeedbackPlan {
  changes: FeedbackChange[]; keep: { stage: string; label: string }[]; pageWide: boolean;
  question: { text: string; options: FeedbackOption[] } | null; wholeOnly: boolean; unmatched: boolean;
}
export interface FeedbackResponse { feedbackId: string; plan: FeedbackPlan; preference: { text: string; topic: string } | null; usd: number }
export interface FeedbackConfirm { targets: FollowUpTarget[]; keep: string[]; pageWide: boolean; remember: boolean }
/** feedback: what to run (null = nothing to change on the page). preference: what was saved, if anything. */
export interface FeedbackConfirmed { feedback: FeedbackRun | null; preference: Preference | null }

/** The Feedback button, step 1: one Haiku call that returns a plan for the reader to confirm. */
export const sendFeedback = (req: FeedbackRequest, signal?: AbortSignal) => postJSON<FeedbackResponse>("/api/feedback", req, signal);
/** Step 2 (no AI call): save what the reader confirmed, and their preference when "Remember" is ticked. */
export const confirmFeedback = (id: string, req: FeedbackConfirm) => apiJSON<FeedbackConfirmed>(`/api/feedback/${encodeURIComponent(id)}/confirm`, "POST", req);
export const listPreferences = () => apiJSON<{ preferences: Preference[] }>("/api/preferences", "GET").then((r) => r.preferences);
export const setPreferenceStatus = (id: string, status: Preference["status"]) => apiJSON<Preference>(`/api/preferences/${encodeURIComponent(id)}`, "PUT", { status });
export const deletePreference = (id: string) => apiJSON<{ ok: true }>(`/api/preferences/${encodeURIComponent(id)}`, "DELETE");
