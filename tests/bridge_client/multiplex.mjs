// Installed SDK + real Bridge: one process, independent runtime channels.
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';

await mkdir('input', { recursive: true });
for (const name of ['scene1_1.json', 'scene1_2.json', 'scene1_3.json']) {
  await writeFile(`input/${name}`, '{"P":[[0.5,0],[0.5,1]],"v":[10,3]}');
}
import { BridgeClient } from '@rade/client';

const queues = new Map();
const logs = new Map();
const waiters = new Map();
const errors = [];
let processReady;
let processEnded = false;
const ready = new Promise(resolve => { processReady = resolve; });
const client = new BridgeClient({
  command: process.env.RADE_TEST_BRIDGE,
  cwd: process.cwd(),
  onReady: () => processReady(),
  onMessage: (event, channelId) => deliver(channelId, event),
  onChannelClosed: channelId => deliver(channelId, { type: 'channel_closed' }),
  onProtocolError: error => errors.push(error.message),
  onProcessError: error => errors.push(error.message),
  onExit: () => {
    processEnded = true;
    for (const waiter of waiters.values()) waiter.reject(new Error('Bridge exited before the expected event'));
  },
});

function deliver(channelId, event) {
  if (!logs.has(channelId)) logs.set(channelId, []);
  logs.get(channelId).push(event);
  const waiter = waiters.get(channelId);
  if (waiter) {
    waiters.delete(channelId);
    waiter.resolve(event);
  } else {
    if (!queues.has(channelId)) queues.set(channelId, []);
    queues.get(channelId).push(event);
  }
}

async function until(channelId, kind) {
  for (;;) {
    const queued = queues.get(channelId);
    const event = queued?.length ? queued.shift() : await new Promise((resolve, reject) => {
      assert.equal(processEnded, false);
      waiters.set(channelId, { resolve, reject });
    });
    assert.notEqual(event.type, 'fatal', JSON.stringify(event));
    assert.notEqual(event.type, 'scene_task_failed', JSON.stringify(event));
    assert.notEqual(event.type, 'session_open_failed', JSON.stringify(event));
    if (event.type === kind) return event;
  }
}

function open(channelId) {
  client.send({
    type: 'open_session', session_id: `sdk-${channelId}`, provider: 'fixture',
  }, channelId);
}

const timeout = setTimeout(() => { void client.shutdown(); }, 45000);
try {
  await ready;
  open('one');
  open('two');
  await Promise.all([until('one', 'runtime_state'), until('two', 'runtime_state')]);
  client.send({ type: 'scene_task_start', turn_id: 'same', scene_input: 'scene1_1.json' }, 'one');
  assert.equal((await until('one', 'assistant_delta')).text, 'waiting');
  client.send({ type: 'scene_task_start', turn_id: 'same', scene_input: 'scene1_2.json' }, 'two');
  const second = await until('two', 'scene_task_completed');
  assert.ok(second.transcript.items.some(item => item.content === 'reply:scene1_2.json'));
  client.cancel('same', 'one');
  const cancelled = await until('one', 'scene_task_cancelled');
  client.send({ type: 'session_snapshot' }, 'one');
  const original = await until('one', 'session_ready');
  const snapshot = await until('one', 'runtime_state');
  assert.equal(original.session_id, 'sdk-one');
  assert.equal(snapshot.phase, 'idle');
  assert.deepEqual(snapshot.context_window, cancelled.runtime.context_window);
  client.closeChannel('one');
  await until('one', 'channel_closed');
  assert.equal(processEnded, false);
  client.send({ type: 'scene_task_start', turn_id: 'next', scene_input: 'scene1_3.json' }, 'two');
  const continued = await until('two', 'scene_task_completed');
  assert.ok(continued.transcript.items.some(item => item.content === 'reply:scene1_3.json'));
  for (const channelId of ['one', 'two']) {
    const events = logs.get(channelId).filter(event => event.event_sequence !== undefined);
    assert.deepEqual(events.map(event => event.event_sequence), events.map((_, index) => index + 1));
  }
  open('one');
  const reopened = await until('one', 'session_ready');
  assert.ok(reopened.transcript.items.some(item => item.role === 'user' && JSON.parse(item.content).filename === 'scene1_1.json'));
  assert.equal((await until('one', 'runtime_state')).phase, 'inactive');
  assert.equal((await client.shutdown()).code, 0);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ channels: 2, isolated: true, reused_process: true }));
} finally {
  clearTimeout(timeout);
  await client.shutdown();
}
