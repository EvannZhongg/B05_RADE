import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Incoming, Outgoing } from "@rade/protocol/internal";
import { SessionSocket } from "../src/session";

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;
  static latest: FakeWebSocket;

  readyState = FakeWebSocket.CONNECTING;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.latest = this;
  }

  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  receive(message: Incoming) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  send(message: string) {
    this.sent.push(message);
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }
}

const snapshot: Incoming = {
  type: "runtime_state", cache_usage: null,
  phase: "inactive",
  turn_id: null,
  provider: "default",
  context_window: null,
  jobs: [],
  plan: null,
};

describe("SessionSocket synchronization", () => {
  beforeEach(() => {
    vi.stubGlobal("location", { protocol: "http:", host: "localhost" });
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  it("reports an occupied session without sending queued commands and allows another selection", async () => {
    const onMessage = vi.fn();
    const onClose = vi.fn();
    const socket = new SessionSocket({
      sessionId: "occupied", attachmentId: "page", onMessage, onClose, onError: vi.fn(),
    });
    socket.send({ type: "provider_set", provider: "second" });
    const pending = socket.sendSceneTask({ type: "scene_task_start", turn_id: "t1", scene_input: "scene1_1.json" });
    const rejected = expect(pending).rejects.toThrow("already in use");
    const failed = FakeWebSocket.latest;
    failed.open();
    failed.receive({
      type: "session_open_failed", session_id: "occupied",
      error: { type: "SessionInUseError", message: "Session is already in use" },
    });
    await rejected;
    expect(onMessage).toHaveBeenCalledWith(expect.objectContaining({ type: "session_open_failed" }));
    expect(onClose).toHaveBeenCalledOnce();
    expect(failed.sent.map((message) => JSON.parse(message).type)).toEqual(["open_session"]);

    const next = new SessionSocket({
      sessionId: "available", attachmentId: "page", onMessage, onClose, onError: vi.fn(),
    });
    next.send({ type: "scene_task_start", turn_id: "t2", scene_input: "scene1_1.json" });
    FakeWebSocket.latest.open();
    FakeWebSocket.latest.receive(snapshot);
    expect(FakeWebSocket.latest.sent.map((message) => JSON.parse(message).type)).toEqual(["open_session", "scene_task_start"]);
  });

  it("sends open_session first and holds commands until runtime_state", () => {
    const received: string[] = [];
    const socket = new SessionSocket({
      sessionId: "session-1",
      attachmentId: "page-1",
      onMessage: (message) => received.push(message.type),
      onClose: () => {},
      onError: () => {},
    });
    socket.send({ type: "scene_task_start", turn_id: "turn-1", scene_input: "scene1_1.json" });

    FakeWebSocket.latest.open();
    expect(FakeWebSocket.latest.sent.map((message) => JSON.parse(message))).toEqual([{
      type: "open_session",
      session_id: "session-1",
      attachment_id: "page-1",
    }]);

    FakeWebSocket.latest.receive({
      type: "session_ready", protocol_version: 10,
      session_id: "session-1",
      provider: "default",
      model: "test/model",
      resumed: false,
      message_count: 0,
    });
    expect(FakeWebSocket.latest.sent).toHaveLength(1);

    FakeWebSocket.latest.receive(snapshot);
    expect(received).toEqual(["session_ready", "runtime_state"]);
    expect(FakeWebSocket.latest.sent.map((message) => JSON.parse(message))[1]).toEqual({
      type: "scene_task_start",
      turn_id: "turn-1",
      scene_input: "scene1_1.json",
    });
  });

  it("confirms a user turn only after the bridge accepts it", async () => {
    const socket = new SessionSocket({
      sessionId: "session-1",
      attachmentId: "page-1",
      onMessage: () => {},
      onClose: () => {},
      onError: () => {},
    });
    const confirmed = vi.fn();
    const delivery = socket.sendSceneTask({
      type: "scene_task_start",
      turn_id: "turn-1",
      scene_input: "scene1_1.json",
    }).then(confirmed);

    FakeWebSocket.latest.open();
    FakeWebSocket.latest.receive(snapshot);
    await Promise.resolve();
    expect(confirmed).not.toHaveBeenCalled();

    FakeWebSocket.latest.receive({
      type: "scene_task_received",
      turn_id: "turn-1",
    });
    await delivery;
    expect(confirmed).toHaveBeenCalledOnce();
  });

  it("rejects an unconfirmed turn when the connection closes", async () => {
    const socket = new SessionSocket({
      sessionId: "session-1",
      attachmentId: "page-1",
      onMessage: () => {},
      onClose: () => {},
      onError: () => {},
    });
    const consumed = vi.fn();
    const delivery = socket.sendSceneTask({
      type: "scene_task_start",
      turn_id: "turn-1",
      scene_input: "scene1_1.json",
    }).then(consumed);

    FakeWebSocket.latest.open();
    FakeWebSocket.latest.receive(snapshot);
    FakeWebSocket.latest.close();

    await expect(delivery).rejects.toThrow("before confirming the turn");
    expect(consumed).not.toHaveBeenCalled();
  });

  it("queues model and refresh choices during startup in order", () => {
    const socket = new SessionSocket({
      sessionId: "session-1",
      attachmentId: "page-1",
      onMessage: () => {},
      onClose: () => {},
      onError: () => {},
    });
    const choices = [
      { type: "provider_set", provider: "selected-model" },
      { type: "runtime_refresh" },
    ] satisfies Outgoing[];
    socket.send(choices[0]);
    FakeWebSocket.latest.open();
    socket.send(choices[1]);
    expect(FakeWebSocket.latest.sent).toHaveLength(1);

    FakeWebSocket.latest.receive(snapshot);
    expect(FakeWebSocket.latest.sent.slice(1).map((message) => JSON.parse(message))).toEqual(choices);

    // Later snapshots from configuration changes must not resend the choices.
    FakeWebSocket.latest.receive(snapshot);
    expect(FakeWebSocket.latest.sent).toHaveLength(3);
  });

  it("discards startup choices when the page cannot acquire the session", () => {
    const socket = new SessionSocket({
      sessionId: "session-1",
      attachmentId: "page-2",
      onMessage: (message) => {
        if (message.type === "attachment_replaced") socket.close();
      },
      onClose: () => {},
      onError: () => {},
    });
    socket.send({ type: "provider_set", provider: "second" });
    FakeWebSocket.latest.open();
    FakeWebSocket.latest.receive({ type: "attachment_replaced", phase: "running" });

    expect(FakeWebSocket.latest.readyState).toBe(FakeWebSocket.CLOSED);
    expect(FakeWebSocket.latest.sent.map((message) => JSON.parse(message).type)).toEqual(["open_session"]);
  });

  it.each(["starting", "running"] as const)(
    "rejects queued configuration for a %s session without blocking steer or cancel",
    (phase) => {
      const rejected = vi.fn();
      const socket = new SessionSocket({
        sessionId: "session-1",
        attachmentId: "new-page",
        onMessage: () => {},
        onConfigurationRejected: rejected,
        onClose: () => {},
        onError: () => {},
      });
      socket.send({ type: "provider_set", provider: "second" });
      socket.send({ type: "runtime_refresh" });
      socket.send({ type: "user_steer", turn_id: "active-turn", steer_id: "steer", text: "continue" });
      FakeWebSocket.latest.open();
      FakeWebSocket.latest.receive({ ...snapshot, phase, turn_id: "active-turn" });

      expect(rejected.mock.calls).toEqual([["provider_set"], ["runtime_refresh"]]);
      expect(FakeWebSocket.latest.sent.slice(1).map((message) => JSON.parse(message))).toEqual([
        { type: "user_steer", turn_id: "active-turn", steer_id: "steer", text: "continue" },
      ]);
      socket.send({ type: "cancel", turn_id: "active-turn" });
      expect(JSON.parse(FakeWebSocket.latest.sent.at(-1)!)).toEqual({ type: "cancel", turn_id: "active-turn" });

      FakeWebSocket.latest.receive({ ...snapshot, phase: "idle" });
      expect(FakeWebSocket.latest.sent).toHaveLength(3);
      socket.send({ type: "provider_set", provider: "second" });
      expect(JSON.parse(FakeWebSocket.latest.sent.at(-1)!)).toEqual({ type: "provider_set", provider: "second" });
    },
  );

  it("waits through replayed idle snapshots for the current running snapshot", () => {
    const rejected = vi.fn();
    const socket = new SessionSocket({
      sessionId: "session-1",
      attachmentId: "new-page",
      onMessage: () => {},
      onConfigurationRejected: rejected,
      onClose: () => {},
      onError: () => {},
    });
    socket.send({ type: "provider_set", provider: "second" });
    FakeWebSocket.latest.open();
    FakeWebSocket.latest.receive({ ...snapshot, replayed: true, event_sequence: 2 });
    FakeWebSocket.latest.receive({ type: "scene_task_completed", turn_id: "old-turn", task: { task_id: 'task', filename: 'scene1_1.json', mode: 'rethink', status: 'completed', elapsed_seconds: 1, output_path: 'output/rethink/scene1_1.json', expected_value: 8, baseline_expected_value: 7.5, experiment_id: 'candidate', baseline_experiment_id: 'baseline' }, usage: null, replayed: true });
    expect(FakeWebSocket.latest.sent).toHaveLength(1);
    expect(rejected).not.toHaveBeenCalled();

    FakeWebSocket.latest.receive({ ...snapshot, phase: "running", turn_id: "active-turn", event_sequence: 8 });
    expect(FakeWebSocket.latest.sent).toHaveLength(1);
    expect(rejected.mock.calls).toEqual([["provider_set"]]);
  });

  it("guards changes made before React renders the active phase and unlocks after completion", () => {
    const rejected = vi.fn();
    const socket = new SessionSocket({
      sessionId: "session-1",
      attachmentId: "page-1",
      onMessage: () => {},
      onConfigurationRejected: rejected,
      onClose: () => {},
      onError: () => {},
    });
    FakeWebSocket.latest.open();
    FakeWebSocket.latest.receive(snapshot);
    socket.send({ type: "scene_task_start", turn_id: "t1", scene_input: "scene1_1.json" });
    socket.send({ type: "provider_set", provider: "second" });
    expect(rejected).toHaveBeenCalledWith("provider_set");
    expect(FakeWebSocket.latest.sent).toHaveLength(2);

    FakeWebSocket.latest.receive({
      type: "scene_task_completed", turn_id: "t1", task: { task_id: 'task', filename: 'scene1_1.json', mode: 'rethink', status: 'completed', elapsed_seconds: 1, output_path: 'output/rethink/scene1_1.json', expected_value: 8, baseline_expected_value: 7.5, experiment_id: 'candidate', baseline_experiment_id: 'baseline' }, usage: null,
      runtime: { ...snapshot, phase: "idle" },
    });
    socket.send({ type: "provider_set", provider: "second" });
    expect(FakeWebSocket.latest.sent).toHaveLength(3);
  });
});
