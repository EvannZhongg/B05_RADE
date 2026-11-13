import { beforeEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
import { render } from 'ink-testing-library';
import type { Incoming } from '@rade/protocol/internal';

const sent: any[] = [];
let emit: (message: Incoming, channelId?: string) => void = () => {};
let autoReady = true;
let bridgeCount = 0;
let shutdownCount = 0;
const sentChannels: string[] = [];
const closedChannels: string[] = [];
let activeMockChannel = 'session-1';
const readyByChannel = new Map<string, any>();
const stateByChannel = new Map<string, any>();

vi.mock('@rade/client', () => ({
  BridgeClient: class {
    constructor(private options: any) {
      emit = (message, channelId = activeMockChannel) => {
        if (message.type === 'session_ready') readyByChannel.set(channelId, message);
        if (message.type === 'runtime_state') stateByChannel.set(channelId, message);
        options.onMessage(message, channelId);
      };
      bridgeCount += 1;
    }
    send(message: any, channelId: string) {
      activeMockChannel = channelId;
      sent.push(message);
      sentChannels.push(channelId);
      if (message.type === 'list_input_scenes') {
        setTimeout(() => emit({ type: 'input_scenes_listed', filenames: [sceneFilename, 'scene2_1.json'] }, channelId), 10);
      }
      if (autoReady && message.type === 'session_snapshot') {
        setTimeout(() => {
          emit({ ...readyByChannel.get(channelId), resumed: true }, channelId);
          emit(stateByChannel.get(channelId), channelId);
        }, 10);
      }
      if (autoReady && message.type === 'open_session') {
        setTimeout(() => {
            emit({
              type: 'session_ready', protocol_version: 10,
              session_id: message.session_id ?? (channelId === 'session-1' ? 'sess-1234' : `sess-${channelId}`),
              provider: 'test',
              model: 'test/model',
              resumed: false,
              message_count: 0,
            }, channelId);
            emit({
              type: 'runtime_state', cache_usage: null,
              phase: 'inactive',
              turn_id: null,
              provider: 'test',
              context_window: null,
              jobs: [],
              plan: null,
            }, channelId);
          }, 10);
      }
    }
    cancel(turnId: string, channelId: string) {
      sent.push({ type: '__cancel', turn_id: turnId });
      sentChannels.push(channelId);
    }
    closeChannel(channelId: string) {
      closedChannels.push(channelId);
      this.options.onChannelClosed?.(channelId);
    }
    shutdown() { shutdownCount += 1; }
    diagnostics() {
      return '';
    }
  },
}));

const sceneFilename = 'scene1_1.json';

const { App } = await import('../src/app.js');

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

type Frame = () => string | undefined;

/**
 * Polls an assertion instead of sleeping for a fixed delay, so the suite does
 * not race Ink when the machine is busy. The condition must also survive one
 * event-loop turn before we return: Ink detaches its stdin listener while focus
 * moves between inputs and only re-attaches it in a passive
 * effect, so a frame can look right slightly before keys are accepted.
 */
async function waitFor(assertion: () => void, timeout = 1500): Promise<void> {
  const deadline = Date.now() + timeout;
  let passes = 0;
  for (;;) {
    try {
      assertion();
      passes += 1;
      if (passes === 2) return;
    } catch (error) {
      passes = 0;
      if (Date.now() >= deadline) throw error;
    }
    await wait(10);
  }
}

function renderApp() {
  return render(
    <App
      python="python3"
    />,
  );
}

/** The status bar shows the model once the Session control plane is ready. */
const waitForReady = (lastFrame: Frame) =>
  waitFor(() => expect(lastFrame()).toContain('test/model'));

const turns = () => sent.filter((m) => m.type === 'scene_task_start');
const steers = () => sent.filter((m) => m.type === 'user_steer');

/** Types text and waits until Ink has parsed it into the draft. */
async function typeDraft(
  stdin: { write: (data: string) => void },
  lastFrame: Frame,
  text: string,
): Promise<void> {
  stdin.write(text);
  await waitFor(() => expect(lastFrame()).toContain(text));
}

async function selectScene(stdin: { write: (data: string) => void }, lastFrame: Frame): Promise<void> {
  await waitFor(() => expect(lastFrame()).toContain('❯ ' + sceneFilename));
  stdin.write('\r');
}

async function runScene(stdin: { write: (data: string) => void }, lastFrame: Frame): Promise<void> {
  await typeDraft(stdin, lastFrame, '/input');
  stdin.write('\r');
  await selectScene(stdin, lastFrame);
}

function lineContaining(frame: string | undefined, text: string): number {
  return (frame ?? '').split('\n').findIndex((line) => line.includes(text));
}

describe('App', () => {
  it('opens the session picker on occupancy and can retry the same selection', async () => {
    autoReady = false;
    const { stdin, lastFrame, unmount } = renderApp();
    await waitFor(() => expect(sent.filter((m) => m.type === 'open_session')).toHaveLength(1));
    expect(sent.some((m) => m.type === 'load_session')).toBe(false);
    const fail = () => emit({
      type: 'session_open_failed', session_id: 'occupied',
      error: { type: 'SessionInUseError', message: 'occupied' },
    });
    fail();
    await waitFor(() => expect(lastFrame()).toContain('in use in another window'));
    expect(sent.some((m) => m.type === 'list_sessions')).toBe(true);
    emit({ type: 'sessions_listed', sessions: [{ session_id: 'occupied', input_preview: 'Retry session', started_at: null }] });
    await waitFor(() => expect(lastFrame()).toContain('Retry session'));
    stdin.write('\r');
    await waitFor(() => expect(sent.filter((m) => m.type === 'open_session')).toHaveLength(2));
    fail();
    emit({ type: 'sessions_listed', sessions: [{ session_id: 'occupied', input_preview: 'Retry session', started_at: null }] });
    await waitFor(() => expect(lastFrame()).toContain('Retry session'));
    autoReady = true;
    stdin.write('\r');
    await waitForReady(lastFrame);
    expect(sent.filter((m) => m.type === 'open_session')).toHaveLength(3);
    expect(sent.filter((m) => m.type === 'load_session')).toHaveLength(1);
    expect(turns()).toHaveLength(0);
    unmount();
  });

  beforeEach(() => {
    sent.length = 0;
    sentChannels.length = 0;
    closedChannels.length = 0;
    readyByChannel.clear();
    stateByChannel.clear();
    activeMockChannel = 'session-1';
    bridgeCount = 0;
    shutdownCount = 0;
    autoReady = true;
  });

  it('updates cache hits after each model call while the turn is still running', async () => {
    const { lastFrame, unmount } = renderApp();
    await waitForReady(lastFrame);
    emit({
      type: 'context_window', turn_id: 't1',
      input_tokens: 120, system_prompt_tokens: 40, max_input_tokens: 900, max_context_tokens: 1000,
      output_reserve_tokens: 100, compression_threshold: 800,
      compression_count: 0, compression_summary: null,
    });
    emit({ type: 'assistant_delta', turn_id: 't1', text: 'working', model_call_index: 1 });
    await waitFor(() => expect(lastFrame()).toContain('Cache hit: unknown'));
    emit({
      type: 'model_completed', turn_id: 't1', model_call_index: 1, usage: {
        input_tokens: 1000, output_tokens: 20, total_tokens: 1020,
        cache_read_tokens: 995, cache_write_tokens: null,
      },
      cache_usage: { input_tokens: 1000, cache_read_tokens: 995 },
    });
    await waitFor(() => expect(lastFrame()).toContain('Cache hit: 99.5%'));
    emit({ type: 'assistant_delta', turn_id: 't1', text: ' continuing', model_call_index: 2 });
    await waitFor(() => {
      expect(lastFrame()).toContain('working');
      expect(lastFrame()).toContain('Cache hit: 99.5%');
    });
    emit({
      type: 'model_completed', turn_id: 't1', model_call_index: 2, usage: {
        input_tokens: 100, output_tokens: 20, total_tokens: 120,
        cache_read_tokens: 0, cache_write_tokens: null,
      },
      cache_usage: { input_tokens: 1100, cache_read_tokens: 995 },
    });
    await waitFor(() => expect(lastFrame()).toContain('Cache hit: 90.5%'));
    emit({ type: 'model_completed', turn_id: 't1', model_call_index: 3, usage: null,
      cache_usage: { input_tokens: 1100, cache_read_tokens: 995 } });
    await waitFor(() => expect(lastFrame()).toContain('Cache hit: 90.5%'));
    emit({
      type: 'compression_completed', turn_id: 't1',
      cache_usage: { input_tokens: 2100, cache_read_tokens: 1990 },
    });
    await waitFor(() => expect(lastFrame()).toContain('Cache hit: 94.8%'));
    emit({
      type: 'compression_completed', turn_id: 't1',
      cache_usage: { input_tokens: 3100, cache_read_tokens: 2985 },
    });
    await waitFor(() => expect(lastFrame()).toContain('Cache hit: 96.3%'));
    unmount();
  });

  it('renders plan progress from structured protocol state', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);
    emit({
      type: 'plan_updated',
      plan: {
        plan_id: 'plan-1',
        goal: 'Ship plans',
        revision: 1,
        steps: [
          { id: 'inspect', title: 'Inspect architecture', status: 'completed', outcome: 'Architecture mapped.' },
          { id: 'frontend', title: 'Connect frontends', status: 'in_progress' },
          { id: 'verify', title: 'Verify recovery', status: 'pending' },
        ],
      },
    });

    await waitFor(() => {
      expect(lastFrame()).toContain('Plan');
      expect(lastFrame()).toContain('✓ Inspect architecture');
      expect(lastFrame()).toContain('Architecture mapped.');
      expect(lastFrame()).toContain('◉ Connect frontends');
      expect(lastFrame()).toContain('○ Verify recovery');
    });
  });

  it('opens a new run', async () => {
    renderApp();
    await waitFor(() =>
      expect(sent[0]).toMatchObject({
        type: 'open_session',
        session_id: null,
      }),
    );
  });

  it('ignores a local path and submits only the scene selected from /input', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    await typeDraft(stdin, lastFrame, 'outside/scene1_1.json');
    stdin.write('\r');
    await wait(30);
    expect(turns()).toHaveLength(0);
    stdin.write('\u001b');
    await waitFor(() => expect(lastFrame()).toContain('Enter /input to select a scene'));
    await typeDraft(stdin, lastFrame, '/input');
    stdin.write('\r');
    await waitFor(() => expect(lastFrame()).toContain('❯ ' + sceneFilename));
    stdin.write('\u001b[B');
    await waitFor(() => expect(lastFrame()).toContain('❯ scene2_1.json'));
    stdin.write('\r');
    await waitFor(() => expect(turns()[0]).toMatchObject({ scene_input: 'scene2_1.json' }));
  });

  it('keeps an opening draft without submitting before runtime synchronization', async () => {
    autoReady = false;
    const { stdin, lastFrame } = renderApp();
    await typeDraft(stdin, lastFrame, '/input');
    stdin.write('\r');
    await wait(20);
    expect(turns()).toHaveLength(0);
    expect(lastFrame()).toContain('/input');

    emit({
      type: 'session_ready', protocol_version: 10,
      session_id: 'sess-1234',
      provider: 'test',
      model: 'test/model',
      resumed: false,
      message_count: 0,
    });
    emit({
      type: 'runtime_state', cache_usage: null,
      phase: 'inactive',
      turn_id: null,
      provider: 'test',
      context_window: null,
      jobs: [],
      plan: null,
    });
    await waitForReady(lastFrame);
    stdin.write('\r');
    await selectScene(stdin, lastFrame);
    await waitFor(() => expect(turns()[0]).toMatchObject({ scene_input: sceneFilename }));
  });

  it('refreshes the runtime without creating a turn or restarting the bridge', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    await typeDraft(stdin, lastFrame, '/refresh');
    stdin.write('\r');
    await waitFor(() => expect(sent).toContainEqual({ type: 'runtime_refresh' }));
    expect(turns()).toHaveLength(0);
    expect(bridgeCount).toBe(1);
    await typeDraft(stdin, lastFrame, '/input');
    stdin.write('\r');
    await waitFor(() => expect(lastFrame()).toContain('/input'));
    expect(turns()).toHaveLength(0);
    emit({ type: 'runtime_refreshed', provider: 'test', model: 'test/updated' });
    await waitFor(() => expect(lastFrame()).toContain('Runtime refreshed.'));
    expect(lastFrame()).toContain('test/updated');
    stdin.write('\r');
    await selectScene(stdin, lastFrame);
    await waitFor(() => expect(turns()).toContainEqual(expect.objectContaining({ scene_input: sceneFilename })));
  });

  it('opens /model and switches through provider_set', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    await typeDraft(stdin, lastFrame, '/model');
    stdin.write('\r');
    await waitFor(() => expect(sent).toContainEqual({ type: 'settings_get' }));
    emit({
      type: 'settings_snapshot',
      settings: {
        revision: 'r1',
        config_directory: '/config',
        default_provider: 'test',
        providers: [
          { id: 'test', model: 'test/model', url: null, max_context_tokens: null, credential: { source: 'none', env_name: null, configured: false } },
          { id: 'second', model: 'test/second', url: null, max_context_tokens: null, credential: { source: 'none', env_name: null, configured: false } },
        ],
        agent: { max_same_tool_calls: 5, output_reserve_tokens: 100, max_generation_tokens: null, tools: {}, context: { compression: { enabled: true, trigger_ratio: null, keep_recent_units: 4, user_anchor_token_budget: 2000 } }, memory: { enabled: true, max_tokens: 3000 }, subagent_roles: {}, mcp_enabled: false },
        skills: [], plugins: [], mcp_servers: [], plugin_agents: [], warnings: [],
      },
    });
    await waitFor(() => expect(lastFrame()).toContain('❯ test · test/model'));
    stdin.write('\u001b[B');
    await waitFor(() => expect(lastFrame()).toContain('❯ second · test/second'));
    stdin.write('\r');
    await waitFor(() => expect(sent).toContainEqual({ type: 'provider_set', provider: 'second' }));
    expect(turns()).toHaveLength(0);
  });

  it('shows /provider configuration without sending a turn', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    await typeDraft(stdin, lastFrame, '/provider');
    stdin.write('\r');
    emit({
      type: 'settings_snapshot',
      settings: {
        revision: 'r1', config_directory: '/config', default_provider: 'test',
        providers: [{ id: 'test', model: 'test/model', url: null, max_context_tokens: null, credential: { source: 'none', env_name: null, configured: false } }],
        agent: { max_same_tool_calls: 5, output_reserve_tokens: 100, max_generation_tokens: null, tools: {}, context: { compression: { enabled: true, trigger_ratio: null, keep_recent_units: 4, user_anchor_token_budget: 2000 } }, memory: { enabled: true, max_tokens: 3000 }, subagent_roles: {}, mcp_enabled: false },
        skills: [], plugins: [], mcp_servers: [], plugin_agents: [], warnings: [],
      },
    });
    await waitFor(() => expect(lastFrame()).toContain('Configuration · /config'));
    expect(turns()).toHaveLength(0);
  });

  it('lists the commands after a slash and runs the highlighted one', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);

    stdin.write('/');
    await waitFor(() => expect(lastFrame()).toContain('❯ /sessions'));

    const frame = lastFrame() ?? '';
    const menuLine = lineContaining(frame, '❯ /sessions');
    const ruleLine = frame.split('\n').findIndex((line) => line.includes('─'));
    expect(menuLine).toBeGreaterThanOrEqual(0);
    expect(menuLine).toBeLessThan(ruleLine);

    stdin.write('\r');
    await waitFor(() => expect(sent).toContainEqual(expect.objectContaining({ type: 'list_sessions' })));
    expect(turns()).toHaveLength(0);
  });

  it('switches to another conversation and replays it', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    const startedAt = '2026-10-26T01:00:00Z';
    const time = new Date(startedAt).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'medium' });

    await typeDraft(stdin, lastFrame, '/sessions');
    stdin.write('\r');
    await waitFor(() => expect(sent.find((m) => m.type === 'list_sessions')).toBeDefined());
    await waitFor(() => expect(lastFrame()).toContain('Runs'));

    emit({
      type: 'sessions_listed',
      sessions: [
        { session_id: 'newer', input_preview: 'parser.json', started_at: startedAt },
        { session_id: 'older', input_preview: 'about the tests', started_at: null },
      ],
    });
    await waitFor(() => expect(lastFrame()).toContain(`❯ ${time} · parser.json`));

    stdin.write('\u001b[B');
    await waitFor(() => expect(lastFrame()).toContain('❯ about the tests'));
    stdin.write('\r');

    // Session switching stays on the existing process.
    await waitFor(() => expect(sent.filter((m) => m.type === 'open_session')).toHaveLength(2));
    expect(sent.filter((m) => m.type === 'open_session')[1]).toMatchObject({
      session_id: 'older',
    });
    expect(lastFrame()).not.toContain('❯ about the tests');
    expect(bridgeCount).toBe(1);
    expect(shutdownCount).toBe(0);

    emit({
      type: 'session_items',
      items: [
        { role: 'user', content: 'earlier question' },
        {
          role: 'assistant',
          content: 'earlier answer',
          timestamp_utc: '2026-09-16T10:00:00Z',
        },
      ],
    });
    await waitFor(() => expect(lastFrame()).toContain('earlier question'));
    expect(lastFrame()).toContain('earlier answer');
    expect(turns()).toHaveLength(0);
  });

  it('keeps independent runtime channels and snapshots the original when switching back', async () => {
    const { stdin, lastFrame, unmount } = renderApp();
    await waitForReady(lastFrame);
    const firstChannel = sentChannels[sent.findIndex(message => message.type === 'open_session')];
    await typeDraft(stdin, lastFrame, '/sessions');
    stdin.write('\r');
    await waitFor(() => expect(lastFrame()).toContain('Runs'));
    emit({ type: 'sessions_listed', sessions: [{ session_id: 'second', input_preview: 'Second runtime', started_at: null }] });
    await waitFor(() => expect(lastFrame()).toContain('Second runtime'));
    stdin.write('\r');
    await waitFor(() => expect(sent.filter(message => message.type === 'open_session')).toHaveLength(2));
    await waitForReady(lastFrame);
    const secondIndex = sent.findIndex(message => message.type === 'open_session' && message.session_id === 'second');
    const secondChannel = sentChannels[secondIndex];
    expect(secondChannel).not.toBe(firstChannel);
    emit({ type: 'assistant_delta', turn_id: 'background', text: 'hidden first session', model_call_index: 1 }, firstChannel);
    await wait(30);
    expect(lastFrame()).not.toContain('hidden first session');

    await typeDraft(stdin, lastFrame, '/sessions');
    stdin.write('\r');
    await waitFor(() => expect(lastFrame()).toContain('Runs'));
    emit({ type: 'sessions_listed', sessions: [{ session_id: 'sess-1234', input_preview: 'Original runtime', started_at: null }] });
    await waitFor(() => expect(lastFrame()).toContain('Original runtime'));
    stdin.write('\r');
    await waitFor(() => expect(sent.some(message => message.type === 'session_snapshot')).toBe(true));
    await waitForReady(lastFrame);
    const snapshotIndex = sent.findIndex(message => message.type === 'session_snapshot');
    expect(sentChannels[snapshotIndex]).toBe(firstChannel);
    expect(sent.filter(message => message.type === 'open_session')).toHaveLength(2);
    expect(closedChannels).toEqual([]);
    emit({ type: 'assistant_delta', turn_id: 'background', text: 'hidden second session', model_call_index: 1 }, secondChannel);
    await runScene(stdin, lastFrame);
    await waitFor(() => expect(turns()).toHaveLength(1));
    expect(sentChannels[sent.findIndex(message => message.type === 'scene_task_start')]).toBe(firstChannel);
    expect(lastFrame()).not.toContain('hidden second session');
    expect(bridgeCount).toBe(1);
    unmount();
    expect(shutdownCount).toBe(1);
  });

  it('opens a fresh run and selects a saved run', async () => {
    const { stdin, lastFrame, unmount } = renderApp();
    await waitForReady(lastFrame);
    emit({ type: 'session_items', items: [{ role: 'user', content: 'old run history' }] });
    await waitFor(() => expect(lastFrame()).toContain('old run history'));
    autoReady = false;
    await typeDraft(stdin, lastFrame, '/new');
    stdin.write('\r');
    await waitFor(() => expect(sent.filter((m) => m.type === 'open_session')).toHaveLength(2));
    expect(sent.filter((m) => m.type === 'open_session')[1]).toEqual({
      type: 'open_session', session_id: null,
    });
    expect(lastFrame()).toContain('old run history');

    await typeDraft(stdin, lastFrame, '/input');
    stdin.write('\r');
    await wait(30);
    expect(turns()).toHaveLength(0);
    emit({
      type: 'session_ready', protocol_version: 10, session_id: 'fresh',
      provider: 'test', model: 'test/model', resumed: false, message_count: 0,
    });
    await waitFor(() => expect(sent.filter((m) => m.type === 'load_session')).toHaveLength(2));
    await waitFor(() => expect(lastFrame()).not.toContain('old run history'));
    stdin.write('\r');
    await wait(30);
    expect(turns()).toHaveLength(0);
    const snapshot = {
      type: 'runtime_state' as const, phase: 'inactive' as const, turn_id: null,
      provider: 'test',
      context_window: null, cache_usage: null, jobs: [], plan: null,
    };
    emit(snapshot);
    await waitFor(() => expect(lastFrame()).not.toContain('opening session…'));
    stdin.write('\r');
    await selectScene(stdin, lastFrame);
    await waitFor(() => expect(turns()).toHaveLength(1));
    expect(turns()[0].scene_input).toBe(sceneFilename);

    emit(snapshot);
    emit({ type: 'scene_task_completed', turn_id: turns()[0].turn_id, task: { task_id: 'task', filename: 'scene1_1.json', mode: 'rethink', status: 'completed', elapsed_seconds: 1, output_path: 'output/rethink/scene1_1.json', expected_value: 8, baseline_expected_value: 7.5, experiment_id: 'candidate', baseline_experiment_id: 'baseline' }, usage: null });
    await waitForReady(lastFrame);
    await typeDraft(stdin, lastFrame, '/sessions');
    stdin.write('\r');
    await waitFor(() => expect(sent.some((m) => m.type === 'list_sessions')).toBe(true));
    emit({ type: 'sessions_listed', sessions: [{ session_id: 'next-history', input_preview: 'Next history', started_at: null }] });
    await waitFor(() => expect(lastFrame()).toContain('Next history'));
    stdin.write('\r');
    await waitFor(() => expect(sent.filter((m) => m.type === 'open_session')).toHaveLength(3));
    expect(sent.filter((m) => m.type === 'open_session')[2]).toEqual({
      type: 'open_session', session_id: 'next-history',
    });
    expect(bridgeCount).toBe(1);
    expect(shutdownCount).toBe(0);
    unmount();
  });

  it.each(['/sessions', '/new'])('keeps %s from switching during a turn', async (command) => {
    const { stdin, lastFrame, unmount } = renderApp();
    await waitForReady(lastFrame);
    emit({ type: 'assistant_delta', turn_id: 't1', text: 'working', model_call_index: 1 });
    await waitFor(() => expect(lastFrame()).toContain('working'));
    await typeDraft(stdin, lastFrame, command);
    stdin.write('\r');
    await wait(30);
    expect(sent.some((m) => m.type === 'list_sessions')).toBe(false);
    expect(sent.filter((m) => m.type === 'open_session')).toHaveLength(1);
    expect(lastFrame()).toContain(command);
    unmount();
  });

  it('continues the original session after selecting an occupied GUI session and pressing Escape', async () => {
    const { stdin, lastFrame, unmount } = renderApp();
    await waitForReady(lastFrame);
    emit({ type: 'session_items', items: [{ role: 'user', content: 'original conversation' }] });
    await waitFor(() => expect(lastFrame()).toContain('original conversation'));
    const snapshot = {
      type: 'runtime_state' as const, phase: 'idle' as const, turn_id: null,
      provider: 'test',
      context_window: null, cache_usage: null, jobs: [], plan: null,
    };
    autoReady = false;
    for (let attempt = 1; attempt <= 2; attempt += 1) {
      await typeDraft(stdin, lastFrame, '/sessions');
      stdin.write('\r');
      await waitFor(() => expect(lastFrame()).toContain('Runs'));
      emit({ type: 'sessions_listed', sessions: [{ session_id: 'gui-session', input_preview: 'Open in GUI', started_at: null }] });
      await waitFor(() => expect(lastFrame()).toContain('Open in GUI'));
      stdin.write('\r');
      await waitFor(() => expect(sent.filter((m) => m.type === 'open_session')).toHaveLength(attempt + 1));
      // Let the picker close before the failure reopens it, so Escape is not
      // delivered to the handler of the previous frame.
      await waitFor(() => expect(lastFrame()).not.toContain('↑/↓ select'));
      emit({
        type: 'session_open_failed', session_id: 'gui-session',
        error: { type: 'SessionInUseError', message: 'occupied' },
      });
      await waitFor(() => expect(lastFrame()).toContain('↑/↓ select'));
      expect(lastFrame()).toContain('in use in another window');
      expect(lastFrame()).toContain('original conversation');
      stdin.write('\u001b');
      await waitFor(() => expect(lastFrame()).toContain('Enter /input to select a scene'));
      await runScene(stdin, lastFrame);
      await waitFor(() => expect(turns()).toHaveLength(attempt));
      expect(turns().at(-1).scene_input).toBe(sceneFilename);
      expect(sentChannels[sent.lastIndexOf(turns().at(-1))]).toBe('session-1');
      emit(snapshot);
      emit({ type: 'scene_task_completed', turn_id: turns().at(-1).turn_id, task: { task_id: 'task', filename: 'scene1_1.json', mode: 'rethink', status: 'completed', elapsed_seconds: 1, output_path: 'output/rethink/scene1_1.json', expected_value: 8, baseline_expected_value: 7.5, experiment_id: 'candidate', baseline_experiment_id: 'baseline' }, usage: null });
      await waitForReady(lastFrame);
    }
    expect(sent.filter((m) => m.type === 'load_session')).toHaveLength(1);
    expect(bridgeCount).toBe(1);
    expect(shutdownCount).toBe(0);
    unmount();
  });

  it('replays a long conversation without walking into the render limit', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);

    await typeDraft(stdin, lastFrame, '/sessions');
    stdin.write('\r');
    await waitFor(() => expect(lastFrame()).toContain('Runs'));
    emit({
      type: 'sessions_listed',
      sessions: [{ session_id: 'older', input_preview: 'a long conversation', started_at: null }],
    });
    await waitFor(() => expect(lastFrame()).toContain('a long conversation'));
    stdin.write('\r');
    await waitFor(() => expect(sent.filter((m) => m.type === 'open_session')).toHaveLength(2));

    // The whole conversation arrives in one message, so the transcript has
    // to commit it in one step: committing item by item from a single
    // update exhausts React's nested-update limit.
    emit({
      type: 'session_items',
      items: Array.from({ length: 60 }, (_, index) => ({
        role: index % 2 === 0 ? 'user' : 'assistant',
        content: `replayed line ${index + 1}`,
      })),
    });

    await waitFor(() => expect(lastFrame()).toContain('replayed line 60'));
    expect(lastFrame()).toContain('Enter /input to select a scene');
  });

  it('closes the session picker on escape without switching', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);

    await typeDraft(stdin, lastFrame, '/sessions');
    stdin.write('\r');
    await waitFor(() => expect(lastFrame()).toContain('Runs'));

    stdin.write('\u001b');
    await waitFor(() => expect(lastFrame()).not.toContain('Loading…'));
    expect(sent.filter((m) => m.type === 'open_session')).toHaveLength(1);
  });

  it('keeps /input from opening during an active turn', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);

    await runScene(stdin, lastFrame);
    await waitFor(() => expect(turns()).toHaveLength(1));

    await typeDraft(stdin, lastFrame, '/input');
    stdin.write('\r');
    await waitFor(() => expect(lastFrame()).not.toContain('Input scenes'));
    expect(sent.filter((message) => message.type === 'list_input_scenes')).toHaveLength(1);
    expect(turns()).toHaveLength(1);
    expect(steers()).toHaveLength(0);
  });

  it('keeps the input above the one-line status when a turn starts running', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);

    await runScene(stdin, lastFrame);
    await waitFor(() => expect(lastFrame()).toContain('esc to cancel'));

    const inputLine = lineContaining(lastFrame(), 'Enter / for commands');
    const statusLine = lineContaining(lastFrame(), 'test/model');
    expect(statusLine).toBe(inputLine + 2);
    expect((lastFrame() ?? '').split('\n')[statusLine]).toContain('esc to cancel');
  });

  it('cancels the active turn on escape', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    await runScene(stdin, lastFrame);
    // Wait for the busy status: escape only cancels once the turn is running.
    await waitFor(() => expect(lastFrame()).toContain('esc to cancel'));

    stdin.write('\u001b');
    await waitFor(() => expect(sent).toContainEqual({ type: '__cancel', turn_id: 'turn-1' }));
  });

  it('renders streamed reasoning before the answer', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);
    emit({
      type: 'reasoning_delta',
      turn_id: 't1',
      text: 'weighing ',
      model_call_index: 1,
    });
    emit({
      type: 'reasoning_delta',
      turn_id: 't1',
      text: 'the options',
      model_call_index: 1,
    });
    // Both deltas land in one live entry, so the frame carries the whole text.
    await waitFor(() => expect(lastFrame()).toContain('weighing the options'));
  });

  it('grows model output downward from the top of the TUI', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);

    emit({
      type: 'assistant_delta',
      turn_id: 't1',
      text: 'first output',
      model_call_index: 1,
    });
    await waitFor(() => expect(lastFrame()).toContain('first output'));

    expect(lineContaining(lastFrame(), 'first output')).toBeLessThan(3);
    expect(lineContaining(lastFrame(), 'first output')).toBeLessThan(
      lineContaining(lastFrame(), 'Enter / for commands'),
    );
  });

  it('keeps short multiline output in the viewport until it fills downward', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);

    emit({
      type: 'assistant_delta',
      turn_id: 't1',
      text: 'line one\nline two\nline three\nline four\nline five',
      model_call_index: 1,
    });
    await waitFor(() => expect(lastFrame()).toContain('line five'));

    const first = lineContaining(lastFrame(), 'line one');
    const fifth = lineContaining(lastFrame(), 'line five');
    const input = lineContaining(lastFrame(), 'Enter / for commands');
    expect(first).toBeLessThan(3);
    expect(fifth).toBe(first + 4);
    expect(input).toBeGreaterThan(fifth + 1);
  });

  it('lets long model output grow through the terminal stream', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);

    emit({
      type: 'assistant_delta',
      turn_id: 't1',
      text: Array.from({ length: 30 }, (_, index) => `output line ${index + 1}`).join('\n'),
      model_call_index: 1,
    });

    await waitFor(() => {
      const frame = lastFrame() ?? '';
      expect(frame).toContain('output line 1');
      expect(frame).toContain('output line 30');
      expect(frame).toContain('Enter / for commands');
      expect(frame).toContain('test/model');
    });
  });

  it('keeps completed output in the transcript viewport', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);

    emit({
      type: 'assistant_delta',
      turn_id: 't1',
      text: 'completed answer',
      model_call_index: 1,
    });
    emit({
      type: 'assistant_message',
      turn_id: 't1',
      content: 'completed answer',
      timestamp_utc: '2026-09-15T12:00:00Z',
    });
    emit({
      type: 'runtime_state', cache_usage: null,
      phase: 'idle',
      turn_id: null,
      provider: 'test',
      context_window: null,
      jobs: [],
      plan: null,
    });
    emit({ type: 'scene_task_completed', turn_id: 't1', task: { task_id: 'task', filename: 'scene1_1.json', mode: 'rethink', status: 'completed', elapsed_seconds: 1, output_path: 'output/rethink/scene1_1.json', expected_value: 8, baseline_expected_value: 7.5, experiment_id: 'candidate', baseline_experiment_id: 'baseline' }, usage: null });

    await waitFor(() => {
      expect(lastFrame()).toContain('completed answer');
      expect(lastFrame()).toContain('test/model');
    });
  });

  it('renders streamed text', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);
    emit({
      type: 'assistant_delta',
      turn_id: 't1',
      text: 'partial answer',
      model_call_index: 1,
    });
    await waitFor(() => expect(lastFrame()).toContain('partial answer'));

  });


  it('uses one status line before and after startup', async () => {
    autoReady = false;
    const { lastFrame } = renderApp();
    await waitFor(() => expect(lastFrame()).toContain('opening session…'));

    expect(
      lineContaining(lastFrame(), 'opening session…') -
        lineContaining(lastFrame(), 'Enter / for commands'),
    ).toBe(2);

    emit({
      type: 'session_ready', protocol_version: 10,
      session_id: 'sess-1234',
      provider: 'test',
      model: 'test/model',
      resumed: false,
      message_count: 0,
    });
    emit({
      type: 'runtime_state', cache_usage: null,
      phase: 'inactive',
      turn_id: null,
      provider: 'test',
      context_window: null,
      jobs: [],
      plan: null,
    });
    await waitForReady(lastFrame);

    expect(
      lineContaining(lastFrame(), 'test/model') -
        lineContaining(lastFrame(), 'Enter /input to select a scene'),
    ).toBe(2);
  });

  it('updates a later tool when it completes before an earlier call', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);

    emit({
      type: 'tool_call',
      turn_id: 't1',
      tool_call: { id: 'call-a', name: 'subagent', arguments: { task: 'A' } },
      tool_index: 1,
      tool_count: 3,
    });
    emit({
      type: 'tool_call',
      turn_id: 't1',
      tool_call: { id: 'call-b', name: 'subagent', arguments: { task: 'B' } },
      tool_index: 2,
      tool_count: 3,
    });
    emit({
      type: 'tool_call',
      turn_id: 't1',
      tool_call: { id: 'call-c', name: 'subagent', arguments: { task: 'C' } },
      tool_index: 3,
      tool_count: 3,
    });
    await waitFor(() => {
      expect(lastFrame()).toContain('[1/3]');
      expect(lastFrame()).toContain('[2/3]');
      expect(lastFrame()).toContain('[3/3]');
    });

    emit({
      type: 'tool_result',
      turn_id: 't1',
      tool_call_id: 'call-b',
      name: 'subagent',
      ok: true,
      error: null,
      tool_index: 2,
      tool_count: 3,
    });

    await waitFor(() => {
      const frame = lastFrame() ?? '';
      expect(frame).toContain('[1/3]');
      expect(frame).toContain('subagent task=A');
      expect(frame).toContain('[2/3] ✓ subagent task=B');
      expect(frame).toContain('[3/3]');
      expect(frame).toContain('subagent task=C');
    });
  });

  it('shows the index for a single tool call', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);

    emit({
      type: 'tool_call',
      turn_id: 't1',
      tool_call: { id: 'call-1', name: 'shell', arguments: { command: 'ls' } },
      tool_index: 1,
      tool_count: 1,
    });

    await waitFor(() => expect(lastFrame()).toContain('[1/1]'));
  });
});
