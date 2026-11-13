import type { CacheUsage, ContextWindow, PlanSnapshot, SceneTask, SessionItem, SessionSummary } from "@rade/protocol";
import type { SettingsSnapshot } from "@rade/protocol/internal";

export type { SessionItem, SessionSummary };

export type Session = {
  session_id: string;
  items: SessionItem[];
  provider?: string | null;
  context_window?: ContextWindow | null;
  cache_usage?: CacheUsage | null;
  event_sequence?: number;
  plan?: PlanSnapshot | null;
  scene_task?: SceneTask | null;
};
export type SessionSearchMatch = SessionSummary & {
  item_index: number;
  role: "user" | "assistant";
  has_more_in_session: boolean;
  timestamp_utc: string | null;
  snippet: string;
  match_start: number;
  match_end: number;
  prefix_omitted: boolean;
  suffix_omitted: boolean;
};
export type SessionSearchResults = { matches: SessionSearchMatch[]; has_more: boolean };
export type ModelOption = { id: string; model: string };
export type ModelOptions = { default: string; models: ModelOption[] };
export type MemoryDocument = {
  preferences: string[];
  facts: string[];
  decisions: string[];
};
export type MemorySnapshot = {
  runs: { run_id: string; memory: MemoryDocument }[];
};
export type Directory = {
  path: string;
  entries: { name: string; type: "directory" | "file" | "symlink" | "other" }[];
  has_more: boolean;
  next_cursor: string | null;
};

export async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `请求失败 (${response.status})`);
  }
  return response.json();
}

export async function put<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `保存失败 (${response.status})`);
  }
  return response.json();
}

export type { SettingsSnapshot };

export function sessionUrl(sessionId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}`;
}

export async function deleteSession(sessionId: string): Promise<void> {
  const response = await fetch(sessionUrl(sessionId), { method: "DELETE" });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `会话删除失败 (${response.status})`);
  }
}

export async function releaseActiveSession(
  sessionId: string,
  provider?: string,
  attachmentId?: string,
): Promise<void> {
  const query = new URLSearchParams();
  if (provider) query.set("provider", provider);
  if (attachmentId) query.set("attachment_id", attachmentId);
  const suffix = query.size ? `?${query}` : "";
  const response = await fetch(`/api/active-sessions/${encodeURIComponent(sessionId)}${suffix}`, {
    method: "DELETE",
    keepalive: true,
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `会话连接释放失败 (${response.status})`);
  }
}

export async function openRunFolder(sessionId: string): Promise<void> {
  const response = await fetch(`${sessionUrl(sessionId)}/files/open`, { method: "POST" });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `打开运行文件夹失败 (${response.status})`);
  }
}
