# Public Bridge protocol, major 10

This is the local client API for third-party frontends. Install `rade-agent`,
start **`RADE-bridge`** as a child process, and exchange UTF-8 newline-delimited
JSON on stdin/stdout. The executable is installed beside the Python environment's
other console scripts; use its absolute path when that directory is not on PATH.
No Node, browser, HTTP server, GUI, or repository working directory is required.
The executable uses the same Runtime as RADE's first-party frontends.

Each nonblank line is one JSON object with a string `type`. Lines end in `\n`
(`\r\n` is accepted). Chunk boundaries are arbitrary, including inside UTF-8
characters. Flush each command; stdout is flushed per event. Stdout contains
protocol messages only; stderr contains diagnostics and must be drained separately.
There is no fixed small line limit: checkpoints can contain the full transcript.

## Stability and scope

Protocol 7 selects contest scenes by filename from the Bridge process working directory's `input/`.

Protocol 9 restricts `SessionItem.content` to strings and `null`. It removes
attachment and content-part arrays from transcript checkpoints and history.

Protocol 10 starts scene tasks with `scene_task_start`. Scene task events report
the persisted outcome of validation and export. Agent turns are internal to the
task. The first mode is `rethink`.

Every `session_ready` advertises `protocol_version: 10`. Clients must check this
before using the session. A breaking change to documented public fields or
semantics increments this major and is recorded in this document. Additive fields
and event kinds may appear within a major; ignore unknown fields/events. This is
version identification, without version negotiation or a compatibility layer.
Major 3 replaces the one-session process stream with independent runtime channels.
There is no support for earlier wire formats. Every command and event belongs to
an explicit channel, except process startup, fatal transport errors and shutdown.

## Process transport

The process initializes configuration and warms the default provider SDK, tokenizer
and TLS trust context once, without resolving credentials or calling a model. It
then emits `{"type":"bridge_ready","protocol_version":10}`. A host can start this
process during application startup and keep it alive across session creation.

Create a channel by sending its first command:

```json
{"type":"channel_command","channel_id":"chat-1","command":{"type":"open_session","session_id":null}}
```

All subsequent session commands use that same `channel_command` envelope. Session
events are returned as:

```json
{"type":"channel_event","channel_id":"chat-1","event":{"type":"runtime_state","event_sequence":2,"phase":"inactive"}}
```

The event above is abbreviated; actual snapshots carry all fields defined by
`Incoming`. Each channel owns an independent RuntimeHost, command queue, event
sequence, cancellation, Journal writer, execution plane and MCP clients.
Channels execute concurrently. Request and turn IDs need only be unique within
their channel; one channel waiting on HTTP, MCP or user input does not block others.
GUI, TUI and SDK hosts use this same server and lifecycle. The TUI retains a
separate channel for each visited Session. `session_snapshot` returns an existing
channel's ready/snapshot pair without rebinding or rebuilding its Runtime.

Send `{"type":"close_channel","channel_id":"chat-1"}` to cancel and close only
that runtime. Wait for `{"type":"channel_closed","channel_id":"chat-1"}` before
reusing the channel ID or opening the same Journal elsewhere. Closing a channel
keeps the process and the other channels alive. Shared process resources include
configuration services, imported modules, tokenizer vocabularies
and TLS trust contexts; mutable execution state is not shared.

Send `{"type":"shutdown"}` or close stdin to cancel all channels and close the
process. `BridgeRequest` / `BridgeEvent` define these process envelopes. The
`Outgoing` / `Incoming` definitions below describe the nested channel payloads.
All JSON examples in the remaining sections are channel payloads unless stated
otherwise; raw stdio clients must wrap them.

The normative field definitions are `Incoming`, `Outgoing` and their referenced
types in [`@rade/protocol`](../interfaces/protocol/src/protocol.ts). Directions
are from the client's perspective: Outgoing commands, Incoming events. Optional
fields can be absent; explicit `null` is distinct from absence. IDs are opaque,
case-sensitive strings. Allocate a new nonempty `turn_id` for each submission and
a new `steer_id` per steering input; IDs are not retry/idempotency keys.

The following are **internal**, with no public stability promise:

- `@rade/protocol/internal`, all `settings_*` messages, their configuration
  editor schemas, Python classes/module layout and session journal storage format.
- GUI Host `open_session` fields `attach_only`, `after_event`, `attachment_id`,
  `takeover`, and GUI events `attachment_replaced` / `replayed`.
- `resume_after` delivery metadata, GUI HTTP/WebSocket routes, attachment
  ownership and host event caches.
- Concrete Tool argument/output schemas, Job `details`, error `details` keys,
  provider-specific errors and diagnostic text. Their envelopes remain public.

These extra fields may be visible on the wire; their presence does not make them
public. Stdio clients own a single Bridge process and do not attach to a GUI-owned
Bridge. Stdio has no `after_event` replay command or reconnect-to-process API.

## Session initialization and Runtime synchronization

Send the opening command first:

```json
{"type":"open_session","session_id":null}
```

The `session_id` identifies the run directory `.RADE/runs/<run_id>/`.
Pass `null` to allocate an ID, or an ID to open that run.
Optional `provider` selects a configured provider. When omitted, the Runtime
uses the run's saved provider or the application default.

Successful open emits, in order:

1. `session_ready`: protocol major, actual `session_id`, `provider`,
   `model`, `resumed`, and `message_count`. Its `transcript`
   contains the restored conversation.
2. `runtime_state`: the initial authoritative snapshot, initially `inactive`.

`session_ready` confirms the control plane only. Apply the first standalone
`runtime_state` before dispatching follow-up commands. Provider/Tool/MCP/Agent
construction is lazy and happens on the first turn or an explicit refresh.
On startup, agent_runtime fills in missing default configuration, prompts, and
Skills under `.RADE` in the Bridge process's working directory and creates the
directory for user-installed Plugins. It preserves existing files. A session can open without
provider credentials. Configure credentials in the installed provider config or
its referenced environment variables before a model turn; this API does not
prompt interactively or invent credentials.

Opening failures caused by a writer conflict, invalid run identifier or configuration,
or filesystem error emit `session_open_failed` with the requested `session_id`
(null when creating a session) and `error: {type, message}`. Writer conflicts
use `type: "SessionInUseError"`. No ready/sync pair follows that failure.
The channel stays connected and accepts `list_sessions` or another
`open_session`. A failed rebind retains the previously opened session and execution
state; the failure event includes its complete `runtime` snapshot and commands
can continue against that session. An initial opening failure has no retained
session or runtime snapshot. Unexpected opening errors are fatal. A fresh
`open_session` on an idle channel rebinds it and produces a new ready/sync pair;
the channel's sequence continues. Do not pipeline a rebind with a turn.

## Explicit Runtime refresh

After opening and synchronizing an idle session, send:

```json
{"type":"runtime_refresh"}
```

This forces a new execution plane even when configuration is unchanged. It
reloads configuration, prompts, Skills and Plugin Agents, reconnects MCP and
discovers tools again. The same Session, journal and plan
remain bound. No model request or conversation turn is created.

Clients must disable refresh during an active turn; ordinary commands are
processed serially by the Bridge. Runtime also enforces the idle requirement.
Wait for `runtime_refreshed` (with `provider` and `model`) or
`runtime_refresh_failed` (with a structured `error`). These events carry the
current `runtime` snapshot. Assembly publishes `starting`, then `idle` on
success or `failed` on failure. A failed refresh keeps the Bridge alive so the
client can correct configuration and refresh again. It does not restore resources
already closed. Individual unavailable MCP servers are reported through
`mcp_server_status` and do not necessarily fail the entire assembly.

Normal turns still check configuration changes automatically before executing.

## Runtime state

| phase | Meaning |
| --- | --- |
| `inactive` | Session open; execution plane not yet started or was invalidated. |
| `starting` | Runtime assembly in progress. |
| `idle` | No active turn. |
| `running` | Executing a turn. |
| `failed` | Execution plane startup failed, or a fatal process error occurred. |

The snapshot also includes `turn_id`, `provider`,
`context_window`, `cache_usage`, `jobs`, `plan` and optional
`runtime_warnings`. A standalone `runtime_state` or embedded
`runtime` replaces the prior snapshot. Embedded snapshots are complete, not
patches. Render state from these snapshots; do not infer it from model text,
Tool messages, commands sent or terminal events. `idle` alone does not complete a
turn: it is emitted before the terminal event. The process may have queued input.

## Commands and responses

| Command | Payload / response |
| --- | --- |
| `open_session` | Above; `session_ready` then `runtime_state`, or opening failure. |
| `scene_task_start` | `turn_id` and `scene_input`; starts a `rethink` task. |
| `user_steer` | Target `turn_id`, `steer_id`, `text`; received/rejected then applied if consumed. |
| `cancel` | Target `turn_id`; matching queued/active turn is cancelled cooperatively. No immediate acknowledgment. |
| `provider_set` | Configured `provider`; `provider_changed` with `provider`, `model`, `runtime`. |
| `list_sessions` | `sessions_listed.sessions`: saved run summaries. |
| `list_input_scenes` | `input_scenes_listed.filenames`: sorted scene filenames in `input/`. The event includes `error` if listing fails. |
| `load_session` | `session_items.items`: currently bound conversation. |
| `session_snapshot` | `session_ready` then `runtime_state` for the existing Runtime; preserves its execution plane, Jobs, MCP and Journal writer. |

Responses confirm the Runtime's actual changes. Changing the provider may
also emit `runtime_state: inactive`. Send these changes while idle. Commands other
than steering and cancellation are serialized behind an active
turn within that channel. There is no generic RPC or universal request ID. Serialize listing/history requests if matching responses matters.
Use the opening checkpoint to display initial history; `load_session` is an
explicit refresh, not a stream replay or cursor acknowledgment.
The session listing contains stored conversations; a newly opened empty Session
can be absent from that list.
Session summaries include `session_id`, `input_preview` (a preview of the first
user input), and `started_at` (the first run time in UTC).

For a contest scene, send `scene_input: "scene1_1.json"`. Runtime reads the UTF-8
file from `input/` relative to the Bridge process working directory. Filenames
follow `scene<S>_<I>.json`, with `S` in 1–2 and `I` in 1–5. Runtime validates
`P` and `v` and derives processing units from the rows of `P` and tasks from
its columns. The user message contains
the extracted `T`, `W`, `P`, and `V` features, a run-relative `source_path`,
and the evaluated baseline. An invalid scene fails the task before a model call.
After the reflection Turn returns, Runtime evaluates this task's candidates
against the original input, exports the highest-value allocation to
`output/rethink/<scene_input>`, and records the task result in the Session journal.
The latest task is available as `runtime_state.scene_task` after reopening.

Candidates have `P[i][j] > 0`. The `T` and `W` features count tasks or
units without candidates and summarize candidate counts across tasks or units,
respectively. Distributions use nearest-rank `p10` and `p90` and the
ordinary median. The positive probability mean and median are `null` when
there are no positive entries. Per-task best probabilities include zero for
unreachable tasks.

`V.total_value` sums all task values, and `V.unreachable_value` sums the
values of tasks without candidates. `V.top_10_percent_value_share` divides
the sum of the highest `ceil(task_count / 10)` task values by the total.
The share is defined for nonnegative values with a positive total and is
`null` otherwise.

## Scene task lifecycle

```text
client → scene_task_start(turn_id, scene_input)
       ← scene_task_received(turn_id)
       ← runtime_state(starting/running), context/model/tool/... events
       ← runtime_state(idle or failed)
       ← scene_task_completed | scene_task_cancelled | scene_task_failed | scene_task_timed_out  + transcript
```

`scene_task_received` acknowledges enqueueing, not model startup or persistence.
Clients should submit one turn at a time and use `user_steer` for new input during
execution. The transport does not convert `scene_task_start` into steering. Steering
receipt is `user_steer_received` or `user_steer_rejected`, identified by both IDs;
only `user_steer_applied` means Core consumed it. Accepted steering can remain
unapplied when the turn ends. Stale cancellation IDs have no effect.
Do not resubmit after an ambiguous disconnect as though IDs made it idempotent.

File writes (`apply_patch`, `edit_file`, `write_file`) execute directly
and resolve paths within the current run's `files/` directory.
Tool errors can include optional structured `details`. An `apply_patch` commit
failure reports `completed`, `failed`, `pending`, and `cause` there; completed
operations remain applied. The same details are retained in Session tool results.

`cancel` addresses exactly the supplied queued/active turn. It can race completion;
a completed terminal outcome remains valid. It has no separate response. On an
ordinary continuing connection each processed turn produces one terminal outcome:
`scene_task_completed` with a persisted `task` result and last-call `usage`,
`scene_task_cancelled`, `scene_task_failed`, or `scene_task_timed_out`.
The failure and timeout events carry a structured `error`. During shutdown/EOF or process failure a terminal event is not
guaranteed; observe process exit and resume stored history if needed.

## Events, order and transcript checkpoints

All Bridge events have a positive `event_sequence`, strictly increasing in stdout
emission order for the lifetime of that process. It starts at 1 in a new process;
it is not a journal offset or globally unique Session cursor. Do not reuse it after
restarting Bridge. Nested Runtime snapshots need not have a sequence of their own.

| Event family | Meaning / correlation |
| --- | --- |
| `assistant_delta`, `reasoning_delta` | Streamed `text`, keyed by `turn_id` and `model_call_index`. |
| `assistant_message` | Complete assistant `content`, timestamp and model-call index; replaces that call's displayed deltas. |
| `model_completed` | Per-call `usage` and Session cumulative `cache_usage`. |
| `compression_completed` | Session cumulative cache usage after a compression attempt. |
| `context_window` | Current token limits and compression metrics. |
| `tool_batch_started` | Tool declarations for a model call, with IDs/names/arguments. |
| `tool_call`, `tool_result` | Call execution/status keyed by Tool call ID; indices are 1-based. Result has `ok` and nullable structured `error`. |
| `plan_updated` | Full plan snapshot and revision. |
| `job_status` | Background job ID, kind, status, opaque details. |
| `hook_completed` | Hook invocation ID, Plugin-qualified hook ID, checkpoint, Session ID, outcome, decision, reason, timing and optional Tool call ID. See [Plugin Hooks](plugin-hooks.md). |
| `mcp_server_status` | Startup/status diagnostics for a named server. |
| `user_steer_*`, terminal events | Lifecycle described above. |

Model deltas precede the complete message for that call; Tool calls can execute
concurrently and their results can interleave. Preserve actual wire order and
correlate IDs, without imposing a fixed order between unrelated event families.
`model_completed` can precede Tool execution; it does not end the turn.
`cache_usage` is a complete Session total; replace it instead of summing repeated
snapshots. Unknown token counts are `null`. `scene_task_completed.usage` is the last
model call, not the sum of the turn.

`transcript: {items: SessionItem[], event_sequence: N}` is an authoritative
checkpoint replacing the entire conversation through event N. It is attached to
`session_ready`, the first context-window event after accepting turn input (when
one is emitted), and terminal events. Apply the event's non-transcript state, then
replace transcript with the checkpoint; do not append that event's conversation
content again. Apply subsequent conversation events only after N. Session items
contain normalized user/assistant/tool content as text or `null`. Tool results
can contain Artifact references in their JSON text. Session items do
not expose the append-only journal format or provider payloads. Tool-result live
events deliberately omit output; read normalized content from checkpoints.

The GUI Host separately caches Bridge events and supplies `after_event` replay,
`replayed`, `resume_after` and attachment ownership. Those features do not change
the public event order/checkpoint rules and are not available over this stdio API.

## Errors and close

`turn_failed` and `fatal` contain `error: {type, message, details}`. Error class
names and detail keys are diagnostic; branch on the event kind. `turn_failed`
ends a turn and permits further valid commands; a startup failure has phase
`failed`, while an execution failure can leave phase `idle`. A channel `fatal` makes that channel unusable and closes its Runtime, while other
channels continue. A top-level process `fatal` closes the process with exit code 1. It may occur before any session
opens or before a protocol major is available. Malformed JSON/envelopes, unknown
command kinds and out-of-order Session commands produce fatal `ProtocolError`;
invalid command arguments can produce other fatal error types. Interaction
responses, steer and cancel require an opened Session; sending them before opening
has no error-response guarantee. Only the documented commands are supported.

`session_open_failed` is the recoverable opening failure described
above. SDK process errors (missing executable, broken pipe), framing errors,
stderr and abnormal exit are transport diagnostics, not synthetic Agent events.
An abrupt exit may occur without `fatal`; do not assume terminal events will arrive.

To close, send `shutdown`, close stdin and continue draining stdout/stderr until
process close. EOF also requests shutdown and cancels active work; do not close
stdin immediately after submitting a turn you intend to finish. There is no
`shutdown_complete` event. The SDK allows 2 seconds of cooperative cleanup before
terminating the process group/tree; `await client.shutdown()` resolves on drained
process close. Any events still emitted during cleanup are delivered normally.
Forced termination cannot guarantee completed persistence.

## Minimal client sequence

```text
spawn RADE-bridge (pipes)
receive bridge_ready; check protocol_version == 10
send channel_command(chat-1, open_session)
receive channel_event(chat-1, session_ready); check protocol_version == 10; apply checkpoint
receive channel_event(chat-1, runtime_state); apply snapshot
send channel_command(chat-1, scene_task_start) with a fresh turn_id
receive/render events
receive terminal event; apply checkpoint
send close_channel(chat-1); receive channel_closed(chat-1)
send shutdown; close stdin; drain output; await process close
```

See the [TypeScript client](../interfaces/client/README.md) and
[implementation review](bridge-api-design.md).
