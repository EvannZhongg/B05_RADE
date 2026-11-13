// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AssistantRuntimeProvider, ThreadPrimitive } from "@assistant-ui/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Incoming, Outgoing } from "@rade/protocol/internal";
import type { SessionSocketOptions } from "../src/session";
import { Composer } from "../src/chat/Composer";
import { useSessionRuntime } from "../src/chat/useSessionRuntime";

const sent: Outgoing[] = [];
let connection: SessionSocketOptions;
const onTurnEnd = vi.fn();
const onSessionAvailable = vi.fn();
const noop = () => {};
const session = { session_id: "commands", items: [] };
const providers = [{ id: "test", model: "test/model" }];

vi.mock("../src/session", () => ({
  SessionSocket: class {
    constructor(options: SessionSocketOptions) { connection = options; }
    send(message: Outgoing) { sent.push(message); }
    async sendUserTurn(message: Outgoing) { sent.push(message); }
    close() {}
  },
}));

function TestComposer() {
  const runtime = useSessionRuntime({
    session, selected: true, contextWindow: null, backgroundActive: false,
    providers, provider: "test", onProviderChange: noop, onBusyChange: noop,
    onContextWindowChange: noop, onSessionAvailable, onTurnEnd,
  });
  return <AssistantRuntimeProvider runtime={runtime.assistantRuntime}><ThreadPrimitive.Root className="thread">
    <Composer runtime={runtime} />
  </ThreadPrimitive.Root></AssistantRuntimeProvider>;
}

describe("Scene composer", () => {
  let container: HTMLDivElement;
  let root: Root;
  const snapshot: Incoming = {
    type: "runtime_state", phase: "idle", turn_id: null, provider: "test",
    context_window: null, cache_usage: null,
    jobs: [], plan: null,
  };

  beforeEach(async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    vi.stubGlobal("fetch", vi.fn(async (path: string) => ({
      ok: true,
      json: async () => path === "/api/input-scenes"
        ? ["scene1_1.json", "scene2_1.json"]
        : { text: '{"P":[[0.5]],"v":[1]}', truncated: false },
    })));
    sent.length = 0;
    onTurnEnd.mockClear();
    onSessionAvailable.mockClear();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root.render(<TestComposer />));
    await receive(snapshot);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function receive(message: Incoming) {
    await act(async () => connection.onMessage(message));
  }

  async function select(filename: string) {
    const input = container.querySelector<HTMLSelectElement>('[aria-label="选择 input 场景"]')!;
    input.value = filename;
    await act(async () => input.dispatchEvent(new Event("change", { bubbles: true })));
  }

  const refresh = () => container.querySelector<HTMLButtonElement>('[aria-label="重新加载运行环境"]')!;
  const run = () => container.querySelector<HTMLButtonElement>(".json-run")!;

  it("previews a listed scene and keeps the selection during runtime refresh", async () => {
    await select("scene1_1.json");
    expect(container.querySelector('input[type="file"]')).toBeNull();
    expect(container.querySelector("pre")?.textContent).toBe('{"P":[[0.5]],"v":[1]}');
    await act(async () => refresh().click());
    expect(sent).toEqual([{ type: "runtime_refresh" }]);
    expect(run().disabled).toBe(true);
    await receive({ type: "runtime_refreshed", provider: "test", model: "test/model" });
    await receive(snapshot);
    await act(async () => run().click());
    expect(sent.at(-1)).toEqual({
      type: "user_turn", turn_id: "turn-1",
      scene_input: "scene1_1.json",
    });
  });

  it("requires a selection and only submits it while idle", async () => {
    expect(run().disabled).toBe(true);
    await receive({ ...snapshot, phase: "running", turn_id: "active" });
    await select("scene2_1.json");
    expect(run().disabled).toBe(true);
    await act(async () => run().click());
    expect(sent).toEqual([]);
    await receive(snapshot);
    await act(async () => run().click());
    expect(sent).toEqual([{
      type: "user_turn", turn_id: "turn-1",
      scene_input: "scene2_1.json",
    }]);
  });
});
