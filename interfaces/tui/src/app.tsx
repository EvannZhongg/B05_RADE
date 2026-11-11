import { readFile, stat } from 'node:fs/promises';
import { basename, resolve } from 'node:path';
import React, { useEffect, useReducer, useRef, useState } from 'react';
import { Box, Text, useApp, useInput, useWindowSize } from 'ink';
import type { Incoming, Outgoing } from '@rade/protocol/internal';
import { BridgeClient } from '@rade/client';
import { COMMANDS, parseCommand } from './commands.js';
import { readJsonInput } from '../../jsonInput.js';
import { Prompt } from './input.js';
import {
  PlanView,
  ModelPicker,
  ProviderOverview,
  NavigationPicker,
  StatusBar,
  Transcript,
} from './renderer.js';
import { initialState, reducer, type State } from './state.js';

export type AppProps = {
  python: string;
  workspace: string;
};

type SessionSelection = {
  workspace: string;
  sessionId: string | null;
  channelId: string;
};

/** Keep Windows paths unambiguous on the newline-delimited JSON channel. */
function protocolPath(path: string): string {
  return path.replaceAll('\\', '/');
}

export function App(props: AppProps): React.ReactElement {
  const { exit } = useApp();
  const { rows } = useWindowSize();
  const [state, dispatch] = useReducer(reducer, initialState);
  const [draft, setDraft] = useState('');
  const [jsonError, setJsonError] = useState('');
  const [readingJson, setReadingJson] = useState(false);
  const readingJsonRef = useRef(false);
  const [selection, setSelection] = useState<SessionSelection>({ workspace: props.workspace, sessionId: null, channelId: 'session-1' });
  const [settingsMode, setSettingsMode] = useState<'model' | 'provider' | null>(null);
  const [modelChoice, setModelChoice] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const bridgeRef = useRef<BridgeClient<Incoming, Outgoing> | null>(null);
  const activeChannel = useRef('session-1');
  const channelCounter = useRef(1);
  const sessionChannels = useRef(new Map<string, string>());
  const previousView = useRef<{ channelId: string; state: State } | null>(null);
  const currentState = useRef(state);
  currentState.current = state;
  const turnCounter = useRef(0);
  const startedAt = useRef<number | null>(null);

  /**
   * The TUI owns one resident Bridge; each visited Session keeps its own Runtime channel.
   */
  useEffect(() => {
    const client = new BridgeClient<Incoming, Outgoing>({
      command: props.python,
      args: ['-m', 'interfaces.bridge'],
      onMessage: (message, channelId) => {
        if (bridgeRef.current !== client || channelId !== activeChannel.current) return;
        if (message.type === 'session_ready') {
          sessionChannels.current.set(message.session_id, channelId);
          previousView.current = null;
        }
        if (message.type === 'session_open_failed' && previousView.current) {
          const retained = previousView.current;
          previousView.current = null;
          activeChannel.current = retained.channelId;
          dispatch({ type: 'session_restored', state: retained.state });
          client.closeChannel(channelId);
        }
        dispatch({ type: 'message', message });
        if (message.type === 'session_ready') client.send({ type: 'load_session' }, channelId);
        if (message.type === 'session_open_failed') {
          client.send({ type: message.error.type === 'SessionInUseError' ? 'list_sessions' : 'list_workspaces' }, activeChannel.current);
        }
      },
      onChannelClosed: (channelId) => {
        for (const [sessionId, channel] of sessionChannels.current) {
          if (channel === channelId) sessionChannels.current.delete(sessionId);
        }
      },
      onExit: (code) => {
        if (bridgeRef.current !== client) return;
        if (code === 0) exit();
        else dispatch({ type: 'exited', code });
      },
      onProtocolError: (error) => {
        if (bridgeRef.current !== client) return;
        dispatch({
          type: 'message',
          message: {
            type: 'fatal',
            error: { type: 'ProtocolError', message: error.message, details: {} },
          },
        });
      },
      onProcessError: (error) => {
        if (bridgeRef.current !== client) return;
        dispatch({
          type: 'message',
          message: { type: 'fatal', error: { type: 'ProcessError', message: error.message, details: {} } },
        });
      },
    });
    bridgeRef.current = client;
    return () => {
      bridgeRef.current = null;
      client.shutdown();
    };
  }, [props.python, props.workspace]);

  useEffect(() => {
    const client = bridgeRef.current;
    if (!client) return;
    if (selection.sessionId && sessionChannels.current.get(selection.sessionId) === selection.channelId) {
      client.send({ type: 'session_snapshot' }, selection.channelId);
    } else {
      client.send({
        type: 'open_session',
        workspace: protocolPath(selection.workspace),
        session_id: selection.sessionId,
      }, selection.channelId);
    }
  }, [selection, props.python, props.workspace]);

  function send(message: Outgoing): void {
    bridgeRef.current?.send(message, activeChannel.current);
  }

  const busy =
    state.status === 'starting' ||
    state.status === 'streaming' ||
    state.status === 'running' ||
    state.status === 'cancelling';
  const inputBusy = state.status !== 'idle' && state.status !== 'runtime_failed' && state.status !== 'session_unavailable';

  useEffect(() => {
    if (!busy) {
      startedAt.current = null;
      setElapsed(0);
      return;
    }
    if (startedAt.current === null) startedAt.current = Date.now();
    const timer = setInterval(() => {
      if (startedAt.current !== null) {
        setElapsed((Date.now() - startedAt.current) / 1000);
      }
    }, 100);
    return () => clearInterval(timer);
  }, [busy]);

  useEffect(() => {
    if (settingsMode !== 'model' || !state.settings) return;
    const index = state.settings.providers.findIndex((provider) => provider.id === state.provider);
    setModelChoice(index >= 0 ? index : 0);
  }, [settingsMode, state.provider, state.settings]);

  const openSession = (workspace: string, sessionId: string | null): void => {
    const current = currentState.current;
    if (current.sessionId !== null) {
      previousView.current = { channelId: activeChannel.current, state: current };
    } else {
      bridgeRef.current?.closeChannel(activeChannel.current);
    }
    const existing = sessionId ? sessionChannels.current.get(sessionId) : undefined;
    const channelId = existing ?? `session-${++channelCounter.current}`;
    activeChannel.current = channelId;
    dispatch({ type: 'session_opening' });
    setSelection({ workspace, sessionId, channelId });
  };

  useInput(
    (_input, key) => {
      const picker = state.picker;
      if (picker === null) return;
      if (key.escape) {
        dispatch({ type: 'picker_closed' });
        return;
      }
      if (!picker.list?.length) return;
      if (key.upArrow || (key.tab && key.shift)) {
        dispatch({
          type: 'picker_choice',
          selectedIndex:
            (picker.selectedIndex - 1 + picker.list.length) % picker.list.length,
        });
        return;
      }
      if (key.downArrow || key.tab) {
        dispatch({
          type: 'picker_choice',
          selectedIndex: (picker.selectedIndex + 1) % picker.list.length,
        });
        return;
      }
      if (key.return) {
        if (picker.kind === 'workspaces') {
          const workspace = picker.list[picker.selectedIndex];
          if (workspace) openSession(workspace, null);
        } else {
          const session = picker.list[picker.selectedIndex];
          if (session) openSession(state.workspace || selection.workspace, session.session_id);
        }
      }
    },
    { isActive: state.picker !== null },
  );

  useInput(
    (_input, key) => {
      if (!settingsMode) return;
      if (key.escape) {
        setSettingsMode(null);
        dispatch({ type: 'settings_closed' });
        return;
      }
      if (!state.settings) return;
      if (settingsMode !== 'model' || state.settings.providers.length === 0) return;
      if (key.upArrow || (key.tab && key.shift)) {
        setModelChoice((index) => (index - 1 + state.settings!.providers.length) % state.settings!.providers.length);
      } else if (key.downArrow || key.tab) {
        setModelChoice((index) => (index + 1) % state.settings!.providers.length);
      } else if (key.return) {
        const choice = state.settings.providers[modelChoice];
        if (choice) send({ type: 'provider_set', provider: choice.id });
        setSettingsMode(null);
        dispatch({ type: 'settings_closed' });
      }
    },
    { isActive: settingsMode !== null && state.settings !== null },
  );

  // Global keys. Ink delivers Ctrl+C as input, so cancelling is explicit.
  useInput(
    (input, key) => {
      if (readingJsonRef.current) return;
      if (key.ctrl && input === 'c') {
        if (busy) {
          if (state.turnId) bridgeRef.current?.cancel(state.turnId, activeChannel.current);
          dispatch({ type: 'cancelling' });
        } else if (draft === '') {
          exit();
        } else {
          setDraft('');
        }
        return;
      }

      if (key.escape) {
        if (busy) {
          if (state.turnId) bridgeRef.current?.cancel(state.turnId, activeChannel.current);
          dispatch({ type: 'cancelling' });
        } else {
          setDraft('');
        }
        return;
      }

      if (key.ctrl && input === 'd' && !busy && draft === '') exit();
    },
    {
      isActive: state.picker === null && settingsMode === null,
    },
  );

  const submit = async (value: string): Promise<void> => {
    const text = value.trim();
    if (text === '' || readingJsonRef.current) return;
    // runtime_state is the synchronization barrier. Keep the draft editable
    // while the Session opens, but do not let a command overtake its initial
    // execution snapshot and then get reset back to idle by that snapshot.
    if (state.status === 'opening') return;
    if (state.status === 'session_unavailable' && text !== '/sessions' && text !== '/workspaces') return;
    const parsed = parseCommand(text);
    if ((parsed?.command.name === '/sessions' || parsed?.command.name === '/workspaces') && inputBusy) return;
    if (parsed?.command.name === '/refresh' && inputBusy) return;
    if (parsed) {
      setDraft('');
      setJsonError('');
      if (parsed.command.name === '/sessions') {
        send({ type: 'list_sessions' });
        dispatch({ type: 'picker_opened', kind: 'sessions' });
      } else if (parsed.command.name === '/workspaces') {
        send({ type: 'list_workspaces' });
        dispatch({ type: 'picker_opened', kind: 'workspaces' });
      } else if (parsed.command.name === '/model') {
        if (busy) return;
        setSettingsMode('model');
        setModelChoice(0);
        send({ type: 'settings_get' });
      } else if (parsed.command.name === '/provider') {
        setSettingsMode('provider');
        send({ type: 'settings_get' });
      } else if (parsed.command.name === '/refresh') {
        dispatch({ type: 'refresh_submitted' });
        send({ type: 'runtime_refresh' });
      }
      return;
    }
    if (inputBusy) return;
    const path = resolve(state.workspace || selection.workspace, text.replace(/^"(.*)"$/, '$1'));
    setJsonError('');
    readingJsonRef.current = true;
    setReadingJson(true);
    try {
      const jsonInput = await readJsonInput(basename(path), async () => {
        if (!(await stat(path)).isFile()) throw new Error('Expected a JSON file.');
        return readFile(path);
      });
      const turnId = 'turn-' + ++turnCounter.current;
      send({ type: 'user_turn', turn_id: turnId, json_input: jsonInput });
      dispatch({ type: 'submit', turnId, text: jsonInput.filename });
      setDraft((current) => current === value ? '' : current);
    } catch (error) {
      setJsonError(error instanceof Error ? error.message : String(error));
    } finally {
      readingJsonRef.current = false;
      setReadingJson(false);
    }
  };

  return (
    <Box height={Math.max(1, rows - 1)} flexDirection="column">
      <Box flexGrow={1} flexShrink={1} flexDirection="column" overflowY="hidden">
        {state.status !== 'opening' && state.entries.length === 0 ? (
          <Box flexShrink={0}>
            <Text dimColor>RADE · {state.workspace}</Text>
          </Box>
        ) : null}

        <Transcript state={state} />

        {state.plan ? <PlanView plan={state.plan} /> : null}

        {state.picker ? (
          <Box flexShrink={0}>
            <NavigationPicker picker={state.picker} />
          </Box>
        ) : null}
        {settingsMode === 'model' && state.settings ? <Box flexShrink={0}><ModelPicker settings={state.settings} selectedIndex={modelChoice} /></Box> : null}
        {settingsMode === 'provider' && state.settings ? <Box flexShrink={0}><ProviderOverview settings={state.settings} /></Box> : null}
        {settingsMode && !state.settings ? <Box flexShrink={0} marginTop={1}><Text dimColor>Loading configuration…</Text></Box> : null}
      </Box>

      {state.status === 'fatal' ? (
        <Box flexShrink={0} marginTop={1}>
          <Text dimColor>Press Ctrl+C to exit.</Text>
        </Box>
      ) : state.picker || settingsMode ? null : (
        <Prompt
          value={draft}
          onChange={(value) => { setDraft(value); setJsonError(''); }}
          onSubmit={(value) => void submit(value)}
          focus
          busy={inputBusy || readingJson}
          docked
          commands={state.status === 'session_unavailable' ? COMMANDS.filter((command) => command.name === '/sessions' || command.name === '/workspaces') : COMMANDS}
          placeholder={inputBusy ? 'Enter / for commands' : 'JSON file path · Enter to run · / for commands'}
        />
      )}

      {readingJson && <Text dimColor>Reading JSON…</Text>}
      {jsonError && <Text color="red" wrap="truncate-end">{jsonError}</Text>}
      <StatusBar state={state} elapsed={elapsed} />
    </Box>
  );
}
