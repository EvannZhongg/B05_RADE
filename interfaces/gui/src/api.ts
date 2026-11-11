import type { CacheUsage, ContextWindow, PlanSnapshot, SessionItem, SessionSummary } from "@rade/protocol";
import type { SettingsSnapshot } from "@rade/protocol/internal";

export type { SessionItem, SessionSummary };

export type Session = {
  session_id: string;
  items: SessionItem[];
  workspace?: string | null;
  provider?: string | null;
  context_window?: ContextWindow | null;
  cache_usage?: CacheUsage | null;
  event_sequence?: number;
  plan?: PlanSnapshot | null;
  editable_user_messages?: EditableUserMessage[];
};
export type EditableUserMessage = { anchor_id: string; item_index: number };
export type WorkspaceOption = { workspace: string; scratch?: boolean };
export type WorkspaceSessions = WorkspaceOption & { sessions: SessionSummary[] };
export type SessionSearchMatch = {
  session_id: string;
  title: string;
  workspace: string;
  scratch: boolean;
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
  workspaces: { workspace: string; memory: MemoryDocument }[];
};
export type Directory = {
  root: string;
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

export async function selectWorkspace(): Promise<string | null> {
  const response = await fetch("/api/select-workspace");
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `选择工作区失败 (${response.status})`);
  }
  return (await response.json() as { workspace: string | null }).workspace;
}

export async function openWorkspaceFolder(workspace: string): Promise<void> {
  const response = await fetch("/api/workspace/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace }),
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `打开工作区文件夹失败 (${response.status})`);
  }
}

export async function createScratchWorkspace(sessionId: string): Promise<string> {
  const response = await fetch("/api/workspaces/scratch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `临时工作区创建失败 (${response.status})`);
  }
  return (await response.json() as { workspace: string }).workspace;
}

export async function createSignedImageUrl(
  path: string,
  sessionId?: string,
  signal?: AbortSignal,
): Promise<string> {
  const response = await fetch("/api/image-url", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, session_id: sessionId ?? null }),
    signal,
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `图片加载失败 (${response.status})`);
  }
  return (await response.json() as { url: string }).url;
}

export function attachmentDownloadUrl(path: string, filename: string, sessionId?: string): string {
  const query = new URLSearchParams();
  if (sessionId) query.set("session_id", sessionId);
  query.set("download_name", filename);
  const prefix = ".RADE/attachments/";
  if (!path.startsWith(prefix)) return path;
  return `/api/attachments/${encodeURIComponent(path.slice(prefix.length))}?${query}`;
}
