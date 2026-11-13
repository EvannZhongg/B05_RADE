// Runs from an external npm installation; imports only the public packages.
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { BridgeClient } from '@rade/client';
import { PROTOCOL_VERSION } from '@rade/protocol';

const jsonText = '{"P":[[0,0.5],[1,0],[0.25,0.75]],"v":[12,25]}';
await mkdir('input', { recursive: true });
await writeFile('input/scene1_1.json', jsonText);
await writeFile('input/other.json', jsonText);
const messages = [];
let sourcePath;
const problems = [];
let ready;
let stage = 'open';
let synchronized = false;
const client = new BridgeClient({
  command: process.env.RADE_TEST_BRIDGE,
  cwd: process.cwd(),
  onMessage(message) {
    try {
      handleMessage(message);
    } catch (error) {
      problems.push(error.stack);
      void client.shutdown();
    }
  },
  onProtocolError(error) { problems.push(error.message); },
  onProcessError(error) { problems.push(error.message); },
  onExit() {},
});

function handleMessage(message) {
  messages.push(message);
  if (message.type === 'session_ready') {
    assert.equal(message.protocol_version, PROTOCOL_VERSION);
    ready = message;
    assert.deepEqual(message.transcript.items, []);
  }
  if (message.type === 'runtime_state' && ready && !synchronized) {
    synchronized = true;
    client.send({ type: 'list_input_scenes' }, 'main');
  }
  if (message.type === 'input_scenes_listed') {
    assert.deepEqual(message.filenames, ['scene1_1.json']);
    stage = 'complete';
    client.send({ type: 'user_turn', turn_id: 'complete', scene_input: 'scene1_1.json' }, 'main');
  }
  if (message.type === 'user_turn_received' && message.turn_id === 'complete') {
    client.send({ type: 'user_steer', turn_id: 'complete', steer_id: 'steer-1', text: 'Use this answer' }, 'main');
  }
  if (message.type === 'turn_completed') {
    assert.equal(stage, 'complete');
    assert.equal(message.turn_id, 'complete');
    assert.ok(message.transcript.items.some(item => item.role === 'assistant' && item.content === '完成🙂'));
    assert.ok(messages.some(item => item.type === 'user_steer_applied' && item.steer_id === 'steer-1'));
    const input = message.transcript.items.find(item => item.role === 'user').content;
    assert.equal(typeof input, 'string');
    const features = JSON.parse(input);
    assert.equal(features.filename, 'scene1_1.json');
    assert.equal(features.T.count, 2);
    assert.equal(features.W.count, 3);
    assert.equal(features.P.positive_fraction, 0.666667);
    assert.equal(features.P.positive_probability_mean, 0.625);
    assert.equal(features.P.best_probability_per_task.median, 0.875);
    assert.equal(features.V.total_value, 37);
    sourcePath = features.source_path;
    stage = 'cancel';
    client.send({ type: 'user_turn', turn_id: 'cancel', text: 'Wait for cancellation' }, 'main');
    client.cancel('cancel', 'main');
  }
  if (message.type === 'turn_cancelled') {
    assert.equal(message.turn_id, 'cancel');
    assert.equal(message.runtime.phase, 'idle');
    assert.ok(message.transcript.items.length > 0);
    stage = 'failure';
    client.send({
      type: 'user_turn', turn_id: 'failure', scene_input: 'scene1_2.json',
    }, 'main');
  }
  if (message.type === 'turn_failed') {
    assert.equal(stage, 'failure');
    assert.equal(message.turn_id, 'failure');
    assert.equal(typeof message.error.message, 'string');
    assert.equal(typeof message.error.details, 'object');
    assert.equal(message.runtime.phase, 'idle');
    stage = 'history';
    client.send({ type: 'load_session' }, 'main');
  }
  if (message.type === 'session_items' && stage === 'history') {
    assert.ok(message.items.some(item => item.role === 'user'));
    stage = 'done';
    void client.shutdown();
  }
  if (message.type === 'fatal' || message.type === 'session_open_failed') {
    problems.push(message);
    void client.shutdown();
  }
}
const timeout = setTimeout(() => {
  console.error(JSON.stringify({ stage, messages, diagnostics: client.diagnostics() }));
  void client.shutdown();
}, 45000);
client.send({ type: 'open_session', session_id: null, provider: 'fixture' }, 'main');
const result = await client.closed;
clearTimeout(timeout);
assert.equal(stage, 'done', JSON.stringify({ stage, messages, diagnostics: client.diagnostics() }));
assert.deepEqual(problems, []);
assert.equal(await readFile(`.RADE/runs/${ready.session_id}/files/${sourcePath}`, 'utf8'), jsonText);
assert.equal(result.code, 0);
assert.equal(messages.filter(message => message.type === 'model_completed').length, 2);
assert.ok(messages.some(message => message.type === 'assistant_delta' && message.text === '完成🙂'));
assert.ok(messages.some(message => message.type === 'tool_result' && message.name === 'shell' && message.ok));
for (let i = 0; i < messages.length; i++) assert.equal(messages[i].event_sequence, i + 1);
for (const message of messages) {
  if (message.transcript) assert.equal(message.transcript.event_sequence, message.event_sequence);
  assert.notEqual(message.type, 'attachment_replaced');
  assert.equal(message.replayed, undefined);
}
for (const turn of ['complete', 'cancel', 'failure']) {
  const turnEvents = messages.filter(message => message.turn_id === turn);
  assert.equal(turnEvents[0].type, 'user_turn_received');
  assert.equal(turnEvents.filter(message => ['turn_completed', 'turn_cancelled', 'turn_failed'].includes(message.type)).length, 1);
}
console.log(JSON.stringify({ session_id: ready.session_id, events: messages.length }));
