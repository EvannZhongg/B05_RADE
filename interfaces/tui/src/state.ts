import { initialRuntimeState } from '@rade/protocol';
import type {
  Incoming,
  CacheUsage,
  ContextWindow,
  PlanSnapshot,
  ProtocolError,
  SessionItem,
  SceneTask,
  SessionSummary,
  SettingsSnapshot,
  Usage,
} from '@rade/protocol/internal';

export type Status =
  | 'opening'
  | 'starting'
  | 'idle'
  | 'streaming'
  | 'running'
  | 'cancelling'
  | 'runtime_failed'
  | 'session_unavailable'
  | 'fatal';

export type Entry =
  | { kind: 'user'; id: string; text: string }
  | { kind: 'assistant'; id: string; text: string; settled: boolean; timestamp_utc?: string }
  | { kind: 'reasoning'; id: string; text: string; settled: boolean }
  | {
      kind: 'tool';
      id: string;
      name: string;
      args: unknown;
      index: number;
      count: number;
      state: 'running' | 'ok' | 'error';
      error?: ProtocolError;
    }
  | { kind: 'notice'; id: string; level: 'info' | 'warning' | 'error'; text: string };


export type NavigationPicker = (
  | { kind: 'sessions'; list: SessionSummary[] | null }
  | { kind: 'input'; list: string[] | null; error?: string }
) & { selectedIndex: number };

export type State = {
  status: Status;
  sessionId: string | null;
  model: string;
  provider: string;
  entries: Entry[];
  /** Latest MCP startup status; cleared once Runtime initialization finishes. */
  mcpStatus: string | null;
  picker: NavigationPicker | null;
  settings: SettingsSnapshot | null;
  turnId: string | null;
  sceneTask: SceneTask | null;
  usage: Usage | null;
  cacheUsage: CacheUsage | null;
  contextWindow: ContextWindow | null;
  plan: PlanSnapshot | null;
};

export type Action =
  | { type: 'message'; message: Incoming }
  | { type: 'submit'; turnId: string; text: string }
  | { type: 'refresh_submitted' }
  | { type: 'picker_opened'; kind: NavigationPicker['kind'] }
  | { type: 'picker_choice'; selectedIndex: number }
  | { type: 'picker_closed' }
  | { type: 'settings_closed' }
  | { type: 'session_opening' }
  | { type: 'session_restored'; state: State }
  | { type: 'cancelling' }
  | { type: 'exited'; code: number | null };

const initialRuntime = initialRuntimeState();

export const initialState: State = {
  status: 'opening',
  sessionId: null,
  model: '',
  provider: initialRuntime.provider ?? '',
  entries: [],
  mcpStatus: null,
  picker: null,
  settings: null,
  turnId: initialRuntime.turn_id,
  sceneTask: null,
  usage: null,
  cacheUsage: initialRuntime.cache_usage,
  contextWindow: initialRuntime.context_window,
  plan: initialRuntime.plan,
};

let entryCounter = 0;
function nextId(prefix: string): string {
  entryCounter += 1;
  return `${prefix}-${entryCounter}`;
}

/** Appends to the open assistant entry, creating it on the first delta. */
function appendDelta(entries: Entry[], text: string): Entry[] {
  const last = entries[entries.length - 1];
  if (last && last.kind === 'assistant' && !last.settled) {
    return [...entries.slice(0, -1), { ...last, text: last.text + text }];
  }
  return [...entries, { kind: 'assistant', id: nextId('assistant'), text, settled: false }];
}

/**
 * Appends to the open reasoning entry, creating it on the first delta.
 *
 * A model call streams reasoning as many small deltas, so they are merged
 * into one entry the same way assistant text is.
 */
function appendReasoning(entries: Entry[], text: string): Entry[] {
  const last = entries[entries.length - 1];
  if (last && last.kind === 'reasoning' && !last.settled) {
    return [...entries.slice(0, -1), { ...last, text: last.text + text }];
  }
  return [
    ...entries,
    { kind: 'reasoning', id: nextId('reasoning'), text, settled: false },
  ];
}

/**
 * Closes every reasoning entry that is still open.
 *
 * Only one can be open at a time: deltas extend the trailing entry, and any
 * other action closes it. Ink writes the entries before the open one once
 * and never again, so they must stop changing before they leave the live
 * region.
 */
function settleReasoning(state: State): State {
  const open = state.entries.some((entry) => entry.kind === 'reasoning' && !entry.settled);
  if (!open) return state;
  return {
    ...state,
    entries: state.entries.map((entry) =>
      entry.kind === 'reasoning' && !entry.settled ? { ...entry, settled: true } : entry,
    ),
  };
}

function settleAssistant(entries: Entry[], content: string, timestamp_utc?: string): Entry[] {
  const last = entries[entries.length - 1];
  if (last && last.kind === 'assistant' && !last.settled) {
    return [...entries.slice(0, -1), { ...last, text: content, timestamp_utc, settled: true }];
  }
  return [...entries, { kind: 'assistant', id: nextId('assistant'), text: content, timestamp_utc, settled: true }];
}

function resolveTool(
  entries: Entry[],
  toolCallId: string,
  state: 'ok' | 'error',
  error: ProtocolError | null,
): Entry[] {
  return entries.map((entry) =>
    entry.kind === 'tool' && entry.id === toolCallId
      ? { ...entry, state, ...(error ? { error } : {}) }
      : entry,
  );
}

type ToolOutcome = { ok: boolean; error?: ProtocolError };

/** Stored Tool results keyed by the call each one answers. */
function toolOutcomes(items: SessionItem[]): Map<string, ToolOutcome> {
  const outcomes = new Map<string, ToolOutcome>();
  for (const item of items) {
    if (
      item.role !== 'tool'
      || item.tool_call_id === undefined
      || typeof item.content !== 'string'
    ) {
      continue;
    }
    const parsed = JSON.parse(item.content) as { ok?: boolean; error?: ProtocolError };
    outcomes.set(item.tool_call_id, {
      ok: parsed.ok !== false,
      ...(parsed.error ? { error: parsed.error } : {}),
    });
  }
  return outcomes;
}

/**
 * Rebuilds transcript entries from a stored conversation.
 *
 * A resumed Session is replayed from the items the model itself saw, so the
 * entries mirror what the live protocol messages would have produced: one
 * user entry per turn, reasoning ahead of the answer, and one entry per Tool
 * call whose outcome comes from the item answering it.
 */
function historyEntries(items: SessionItem[]): Entry[] {
  const outcomes = toolOutcomes(items);
  const entries: Entry[] = [];

  for (const item of items) {
    // Tool results only carry an outcome; the call entry shows it.
    if (item.role === 'tool') continue;

    if (item.origin === 'job_result' || item.origin === 'user_interaction' || item.origin === 'hook_feedback') continue;

    if (item.reasoning) {
      entries.push({
        kind: 'reasoning',
        id: nextId('reasoning'),
        text: item.reasoning,
        settled: true,
      });
    }

    const text = item.content;
    if (item.role === 'user') {
      if (text) entries.push({ kind: 'user', id: nextId('user'), text });
      continue;
    }

    if (text) {
      entries.push({
        kind: 'assistant',
        id: nextId('assistant'),
        text,
        settled: true,
        ...(item.timestamp_utc ? { timestamp_utc: item.timestamp_utc } : {}),
      });
    }

    const calls = item.tool_calls ?? [];
    calls.forEach((call, index) => {
      const outcome = outcomes.get(call.id);
      entries.push({
        kind: 'tool',
        id: call.id,
        name: call.name,
        args: call.arguments,
        index: index + 1,
        count: calls.length,
        // A call the journal never settled was interrupted, and nothing
        // in a replay is still running.
        state: outcome === undefined || !outcome.ok ? 'error' : 'ok',
        ...(outcome?.error ? { error: outcome.error } : {}),
      });
    });
  }

  return entries;
}

export function reducer(state: State, action: Action): State {
  const next = reduceAction(state, action);
  // Reasoning deltas extend the open entry; any other action means the model
  // moved on to content, a tool call, or the end of the turn.
  const streaming =
    action.type === 'message' && action.message.type === 'reasoning_delta';
  return streaming ? next : settleReasoning(next);
}

/** The state transition for a single action. */
function reduceAction(state: State, action: Action): State {
  switch (action.type) {
    case 'submit':
      return {
        ...state,
        status: 'streaming',
        turnId: action.turnId,
        entries: [
          ...state.entries,
          { kind: 'user', id: nextId('user'), text: action.text },
        ],
      };

    case 'refresh_submitted':
      return { ...state, status: 'starting', turnId: null };

    case 'picker_opened':
      return { ...state, picker: { kind: action.kind, list: null, selectedIndex: 0 } };

    case 'picker_choice':
      return state.picker === null
        ? state
        : {
            ...state,
            picker: { ...state.picker, selectedIndex: action.selectedIndex },
          };

    case 'picker_closed':
      return { ...state, picker: null };

    case 'settings_closed':
      return { ...state, settings: null };

    case 'session_opening':
      return { ...state, status: 'opening', picker: null };

    case 'session_restored':
      return action.state;

    case 'cancelling':
      return state.status === 'idle' || state.status === 'fatal'
        ? state
        : { ...state, status: 'cancelling' };

    case 'exited':
      return {
        ...state,
        status: 'fatal',
        entries: [
          ...state.entries,
          {
            kind: 'notice',
            id: nextId('notice'),
            level: 'error',
            text: `Agent process exited (code ${action.code ?? 'unknown'}).`,
          },
        ],
      };

    case 'message':
      return applyMessage(state, action.message);
  }
}

function applyMessage(state: State, message: Incoming): State {
  switch (message.type) {
    case 'runtime_state': {
      const startupNotices: Entry[] = (message.runtime_warnings ?? []).map((text) => ({
        kind: 'notice',
        id: nextId('notice'),
        level: 'warning',
        text,
      }));
      const status: Status = message.phase === 'starting'
        ? 'starting'
        : message.phase === 'running'
          ? 'running'
          : message.phase === 'failed'
            ? 'runtime_failed'
            : 'idle';
      return {
        ...state,
        status,
        turnId: message.turn_id,
        sceneTask: message.scene_task ?? null,
        contextWindow: message.context_window,
        cacheUsage: message.cache_usage,
        plan: visiblePlan(message.plan),
        mcpStatus: message.phase === 'starting' ? state.mcpStatus : null,
        entries: [...state.entries, ...startupNotices],
      };
    }

    // Attachment bookkeeping belongs to the GUI server; the bridge
    // never sends it to this frontend.
    case 'attachment_replaced':
      return state;

    case 'mcp_server_status':
      return {
        ...state,
        mcpStatus: `MCP ${message.server}: ${message.status}${message.tool_count === undefined ? '' : ` (${message.tool_count} tools)`}${message.error === undefined ? '' : ` — ${message.error}`}`,
      };

    case 'session_open_failed': {
      const retained = state.sessionId !== null
        ? message.runtime ? applyMessage(state, message.runtime) : state
        : { ...initialState, status: 'session_unavailable' as const };
      return {
        ...retained,
        picker: { kind: 'sessions', list: null, selectedIndex: 0 },
        entries: [...retained.entries, {
          kind: 'notice', id: nextId('notice'), level: 'warning',
          text: message.error.type === 'SessionInUseError'
            ? retained.sessionId !== null
              ? 'This session is in use in another window. Press Esc to continue the current conversation, or select another session.'
              : 'This session is in use in another window. Select another session, or return to its current window.'
            : message.error.message,
        }],
      };
    }

    case 'session_ready':
      return {
        ...initialState,
        sessionId: message.session_id,
        usage: null,
        provider: message.provider,
        model: message.model,
        entries: message.resumed
          ? [
              {
                kind: 'notice',
                id: nextId('notice'),
                level: 'info',
                text: `Resumed session with ${message.message_count} message(s).`,
              },
            ]
          : [],
      };

    case 'assistant_delta':
      return {
        ...state,
        status: state.status === 'cancelling' ? state.status : 'streaming',
        entries: appendDelta(state.entries, message.text),
      };
    case 'reasoning_delta':
      return {
        ...state,
        entries: appendReasoning(state.entries, message.text),
      };

    case 'assistant_message':
      return { ...state, entries: settleAssistant(state.entries, message.content, message.timestamp_utc) };

    case 'tool_batch_started':
      return { ...state, status: state.status === 'cancelling' ? state.status : 'running' };

    case 'tool_call':
      return {
        ...state,
        status: state.status === 'cancelling' ? state.status : 'running',
        entries: [
          ...state.entries,
          {
            kind: 'tool',
            id: message.tool_call.id,
            name: message.tool_call.name,
            args: message.tool_call.arguments,
            index: message.tool_index,
            count: message.tool_count,
            state: 'running',
          },
        ],
      };

    case 'context_window':
      return { ...state, contextWindow: message };

    case 'tool_result':
      return {
        ...state,
        entries: resolveTool(
          state.entries,
          message.tool_call_id,
          message.ok ? 'ok' : 'error',
          message.error,
        ),
      };

    case 'hook_completed':
      if (message.status === 'completed' && message.decision !== 'deny') return state;
      return {
        ...state,
        entries: [...state.entries, {
          kind: 'notice', id: nextId('notice'),
          level: message.status === 'failed' ? 'error' : 'info',
          text: message.reason ?? `Hook ${message.hook_id}: ${message.status}.`,
        }],
      };

    case 'job_status':
      return {
        ...state,
        entries: [
          ...state.entries,
          {
            kind: 'notice',
            id: nextId('notice'),
            level: message.status === 'failed' ? 'error' : 'info',
            text: `Background ${message.kind} ${message.job_id}: ${message.status}.`,
          },
        ],
      };

    case 'plan_updated':
      return { ...state, plan: visiblePlan(message.plan) };

    case 'scene_task_received':
    case 'user_steer_received':
      return state;

    case 'user_steer_applied':
      return {
        ...state,
        entries: [
          ...state.entries,
          { kind: 'user', id: nextId('user'), text: message.text },
        ],
      };

    case 'user_steer_rejected':
      return {
        ...state,
        entries: [
          ...state.entries,
          {
            kind: 'notice',
            id: nextId('notice'),
            level: 'info',
            text: 'Steering arrived after the turn finished and was not applied.',
          },
        ],
      };

    case 'provider_changed':
      return { ...state, provider: message.provider, model: message.model, usage: null };

    case 'runtime_refreshed':
      return {
        ...state, status: 'idle', provider: message.provider, model: message.model,
        entries: [...state.entries, { kind: 'notice', id: nextId('notice'), level: 'info', text: 'Runtime refreshed.' }],
      };

    case 'runtime_refresh_failed':
      return {
        ...state,
        entries: [...state.entries, { kind: 'notice', id: nextId('notice'), level: 'error', text: `Runtime refresh failed: ${message.error.message}` }],
      };

    case 'settings_snapshot':
      return { ...state, settings: message.settings };

    case 'settings_update_failed':
      return {
        ...state,
        entries: [...state.entries, { kind: 'notice', id: nextId('notice'), level: 'error', text: message.error.message }],
      };

    case 'sessions_listed':
      return state.picker?.kind === 'sessions'
        ? { ...state, picker: { kind: 'sessions', list: message.sessions, selectedIndex: 0 } }
        : state;

    case 'input_scenes_listed':
      return state.picker?.kind === 'input'
        ? { ...state, picker: { kind: 'input', list: message.filenames, error: message.error, selectedIndex: 0 } }
        : state;

    case 'session_items':
      // session_ready cleared the previous transcript and added the resumed notice.
      return { ...state, entries: [...state.entries, ...historyEntries(message.items)] };

    case 'model_completed':
      return { ...state, usage: message.usage, cacheUsage: message.cache_usage };

    case 'compression_completed':
      return { ...state, cacheUsage: message.cache_usage };

    case 'scene_task_completed':
      return { ...state, turnId: null, sceneTask: message.task, usage: message.usage };

    case 'scene_task_cancelled':
      return {
        ...state,
        turnId: null,
        entries: [
          ...state.entries,
          {
            kind: 'notice',
            id: nextId('notice'),
            level: 'info',
            text: 'Cancelled.',
          },
        ],
      };

    case 'scene_task_timed_out':
    case 'scene_task_failed':
      return {
        ...state,
        turnId: null,
        entries: [
          ...state.entries,
          {
            kind: 'notice',
            id: nextId('notice'),
            level: 'error',
            text: `${message.error.type}: ${message.error.message}`,
          },
        ],
      };

    case 'fatal':
      return {
        ...state,
        status: 'fatal',
        entries: [
          ...state.entries,
          {
            kind: 'notice',
            id: nextId('notice'),
            level: 'error',
            text: `${message.error.type}: ${message.error.message}`,
          },
        ],
      };
  }
}

function visiblePlan(plan: PlanSnapshot | null): PlanSnapshot | null {
  return plan !== null && plan.steps.some((step) => step.status !== 'completed')
    ? plan
    : null;
}
