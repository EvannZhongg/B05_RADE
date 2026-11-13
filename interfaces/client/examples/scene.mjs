import { randomUUID } from 'node:crypto';
import { BridgeClient } from '@rade/client';
import { PROTOCOL_VERSION } from '@rade/protocol';

const [filename] = process.argv.slice(2);
if (!filename) throw new Error('Usage: scene.mjs <scene-filename>');
let opened = false;
let submitted = false;
const client = new BridgeClient({
  onMessage(message) {
    if (message.type === 'session_ready') {
      if (message.protocol_version !== PROTOCOL_VERSION) {
        console.error('Unsupported Bridge protocol major');
        process.exitCode = 1;
        void client.shutdown();
        return;
      }
      opened = true;
    }
    if (message.type === 'runtime_state' && opened && !submitted) {
      submitted = true;
      client.send({ type: 'scene_task_start', turn_id: randomUUID(), scene_input: filename }, 'main');
    }
    if (message.type === 'assistant_delta') process.stdout.write(message.text);
    if (message.type === 'fatal' || message.type === 'scene_task_failed' || message.type === 'scene_task_timed_out' || message.type === 'session_open_failed') {
      console.error(message.error);
      process.exitCode = 1;
      void client.shutdown();
    }
    if (message.type === 'scene_task_completed' || message.type === 'scene_task_cancelled') {
      if (message.type === 'scene_task_completed') console.log('\n' + message.task.output_path);
      void client.shutdown();
    }
  },
  onProcessError(error) { console.error(error); process.exitCode = 1; },
  onProtocolError(error) { console.error(error); process.exitCode = 1; },
  onExit(code, signal) { if (code !== 0 || signal) process.exitCode = 1; },
});
client.send({ type: 'open_session', session_id: null }, 'main');
await client.closed;
