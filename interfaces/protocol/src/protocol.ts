// Public stdio Bridge protocol. Keep Python senders and the reference in sync.

export const PROTOCOL_VERSION = 10;

export type JSONValue =
  | string
  | number
  | boolean
  | null
  | readonly JSONValue[]
  | { readonly [key: string]: JSONValue };

export type ToolCall = {
  id: string;
  name: string;
  arguments: { readonly [key: string]: JSONValue } | string;
  argument_error?: ProtocolError;
};

export type ProtocolError = {
  type: string;
  message: string;
  details?: { readonly [key: string]: JSONValue };
};


export type RuntimeFailure = ProtocolError & {
  details: { readonly [key: string]: JSONValue };
};

export type Usage = {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
};

export type CacheUsage = {
  input_tokens: number;
  cache_read_tokens: number;
};

/** Token-weighted cache hit percentage over the Session's reported calls. */
export function cacheHitPercent(usage: CacheUsage | null): number | null {
  if (usage === null || usage.input_tokens === 0) return null;
  return usage.cache_read_tokens / usage.input_tokens * 100;
}

export type ContextWindow = {
  input_tokens: number;
  system_prompt_tokens: number;
  max_input_tokens: number;
  max_context_tokens: number;
  output_reserve_tokens: number;
  compression_threshold: number;
  compression_count: number;
  compression_summary: string | null;
};

export type PlanStepStatus = 'pending' | 'in_progress' | 'completed' | 'blocked';

export type PlanStep = {
  id: string;
  title: string;
  status: PlanStepStatus;
  outcome?: string;
};

export type PlanSnapshot = {
  plan_id: string;
  goal: string;
  revision: number;
  steps: PlanStep[];
};


export type RuntimePhase =
  | 'inactive'
  | 'starting'
  | 'idle'
  | 'running'
  | 'failed';

export function runtimeIsActive(phase: RuntimePhase): boolean {
  return phase === 'starting'
    || phase === 'running';
}

export type SessionSummary = {
  session_id: string;
  input_preview: string | null;
  started_at: string | null;
};

/**
 * One normalized conversation item in a transcript checkpoint or history response.
 *
 * A turn is several items: the user's message, an assistant step per model
 * call, and one item per Tool result answering that step's calls.
 */
export type SessionItem = {
  role: 'user' | 'assistant' | 'tool';
  content: string | null;
  timestamp_utc?: string;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
  reasoning?: string | null;
  // Non-conversation origins mark runtime-generated context that receives
  // special transcript rendering instead of a normal user message.
  origin?: 'conversation' | 'job_result' | 'user_interaction' | 'hook_feedback';
};

export type BackgroundJob = {
  job_id: string;
  kind: string;
  status: 'submitted' | 'running' | 'completed' | 'failed' | 'cancelled';
  details: Record<string, string>;
};

export type SceneMode = 'norethink' | 'rethink';

export type SceneTask = {
  task_id: string;
  filename: string;
  mode: SceneMode;
  scene_digest?: string;
} & (
  | { status: 'running' }
  | { status: 'cancelled'; elapsed_seconds: number }
  | { status: 'failed' | 'timed_out'; elapsed_seconds: number; error: RuntimeFailure }
  | { status: 'interrupted'; error: RuntimeFailure }
  | {
      status: 'completed'; elapsed_seconds: number; output_path: string;
      expected_value: number; experiment_id: string;
      baseline_experiment_id: string; baseline_expected_value: number;
    }
);

export type RuntimeState = {
  /**
   * Authoritative execution snapshot. The first snapshot after open_session
   * is the synchronization barrier before commands may be dispatched;
   * session_ready only describes the Session control plane.
   */
  type: 'runtime_state';
  phase: RuntimePhase;
  turn_id: string | null;
  provider: string | null;
  context_window: ContextWindow | null;
  cache_usage: CacheUsage | null;
  event_sequence?: number;
  jobs: BackgroundJob[];
  runtime_warnings?: string[];
  plan: PlanSnapshot | null;
  scene_task?: SceneTask;
};

/** Local placeholder only; commands still await an authoritative Bridge snapshot. */
export function initialRuntimeState(): RuntimeState {
  return {
    type: 'runtime_state',
    phase: 'inactive',
    turn_id: null,
    provider: null,
    context_window: null,
    cache_usage: null,
    jobs: [],
    runtime_warnings: [],
    plan: null,
  };
}

export type TranscriptCheckpoint = {
  items: SessionItem[];
  event_sequence: number;
};

export type Incoming = (
  | RuntimeState
  | {
      type: 'session_ready';
      protocol_version: typeof PROTOCOL_VERSION;
      session_id: string;
      provider: string;
      model: string;
      resumed: boolean;
      message_count: number;
    }
  | { type: 'assistant_delta'; turn_id: string; text: string; model_call_index: number }
  | { type: 'reasoning_delta'; turn_id: string; text: string; model_call_index: number }
  | { type: 'model_completed'; turn_id: string; model_call_index: number; usage: Usage | null; cache_usage: CacheUsage | null }
  | { type: 'compression_completed'; turn_id: string; cache_usage: CacheUsage | null }
  | { type: 'hook_completed'; turn_id: string; session_id: string; invocation_id: string; hook_id: string; event: 'InputSubmitted' | 'PreToolUse' | 'PostToolUse' | 'Stop'; status: 'completed' | 'failed' | 'cancelled'; decision: 'pass' | 'deny' | 'continue' | null; reason: string | null; duration_ms: number; tool_call_id: string | null }
  | ({ type: 'context_window'; turn_id: string | null } & ContextWindow)
  | {
      type: 'assistant_message';
      turn_id: string;
      content: string;
      timestamp_utc: string;
      model_call_index: number;
    }
  | {
      type: 'tool_batch_started';
      turn_id: string;
      model_call_index: number;
      tool_calls: ToolCall[];
    }
  | {
      type: 'tool_call';
      turn_id: string;
      tool_call: ToolCall;
      tool_index: number;
      tool_count: number;
    }
  | {
      type: 'tool_result';
      turn_id: string;
      tool_call_id: string;
      name: string;
      ok: boolean;
      error: ProtocolError | null;
      tool_index: number;
      tool_count: number;
    }
  | ({
      type: 'job_status';
      turn_id: string;
    } & BackgroundJob)
  | { type: 'user_steer_received'; turn_id: string | null; steer_id: string; text: string }
  | { type: 'user_steer_rejected'; turn_id: string | null; steer_id: string; text: string }
  | { type: 'scene_task_received'; turn_id: string }
  | {
      type: 'user_steer_applied';
      turn_id: string;
      steer_id: string;
      text: string;
      timestamp_utc: string;
    }
  | { type: 'provider_changed'; provider: string; model: string }
  | { type: 'runtime_refreshed'; provider: string; model: string }
  | { type: 'runtime_refresh_failed'; error: RuntimeFailure }
  | { type: 'sessions_listed'; sessions: SessionSummary[] }
  | { type: 'input_scenes_listed'; filenames: string[]; error?: string }
  | { type: 'session_items'; items: SessionItem[] }
  | { type: 'plan_updated'; plan: PlanSnapshot }
  | { type: 'mcp_server_status'; server: string; status: string; tool_count?: number; error?: string }
  | { type: 'scene_task_completed'; turn_id: string; task: Extract<SceneTask, { status: 'completed' }>; usage: Usage | null }
  | { type: 'scene_task_cancelled'; turn_id: string; task: Extract<SceneTask, { status: 'cancelled' }> }
  | { type: 'scene_task_failed' | 'scene_task_timed_out'; turn_id: string; task: Extract<SceneTask, { status: 'failed' | 'timed_out' }>; error: RuntimeFailure }
  | { type: 'fatal'; error: RuntimeFailure }
  | { type: 'session_open_failed'; session_id: string | null; error: ProtocolError }
) & {
  event_sequence: number;
  runtime?: RuntimeState;
  /** Replaces the transcript through event_sequence, independent of phase. */
  transcript?: TranscriptCheckpoint;
};

export type Outgoing =
  | {
      // May also rebind an idle Bridge to another Session without restarting it.
      type: 'open_session';
      session_id: string | null;
      // Omitted to use the provider selected in the configuration file.
      provider?: string;
    }
  | { type: 'scene_task_start'; turn_id: string; scene_input: string; mode?: SceneMode }
  | { type: 'user_steer'; turn_id: string; steer_id: string; text: string }
  | { type: 'provider_set'; provider: string }
  | { type: 'runtime_refresh' }
  | { type: 'cancel'; turn_id: string }
  // List stored conversations; control-plane-only Sessions can be absent.
  | { type: 'list_sessions' }
  | { type: 'list_input_scenes' }
  | { type: 'load_session' }
  | { type: 'session_snapshot' };

/** A channel owns one independent Runtime, including its event cursors. */
export type BridgeRequest<Command = Outgoing> =
  | { type: 'channel_command'; channel_id: string; command: Command }
  | { type: 'close_channel'; channel_id: string }
  | { type: 'shutdown' };

export type BridgeEvent<Event = Incoming> =
  | { type: 'bridge_ready'; protocol_version: number }
  | { type: 'channel_event'; channel_id: string; event: Event }
  | { type: 'channel_closed'; channel_id: string }
  | { type: 'fatal'; error: RuntimeFailure };

export { MessageDecoder } from './decoder.js';

export function formatArguments(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value !== 'object') return String(value);
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) return '';
  return entries
    .map(([key, item]) => `${key}=${typeof item === 'string' ? item : JSON.stringify(item)}`)
    .join(' ');
}
