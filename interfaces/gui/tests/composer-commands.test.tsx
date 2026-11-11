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
const onTitleChange = vi.fn();
const noop = () => {};
const session = { session_id: "commands", workspace: "/workspace", items: [] };
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
    session, selected: true, contextWindow: null, workspaceOptions: [], backgroundActive: false,
    providers, provider: "test", onProviderChange: noop, onBusyChange: noop,
    onContextWindowChange: noop, onSessionAvailable, onTitleChange, onTurnEnd,
  });
  return <AssistantRuntimeProvider runtime={runtime.assistantRuntime}><ThreadPrimitive.Root className="thread">
    <Composer runtime={runtime} />
  </ThreadPrimitive.Root></AssistantRuntimeProvider>;
}

describe("JSON composer", () => {
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
    sent.length = 0;
    onTurnEnd.mockClear();
    onSessionAvailable.mockClear();
    onTitleChange.mockClear();
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

  it("applies title updates during a running turn without treating them as turn completion", async () => {
    await receive({ ...snapshot, phase: "running", turn_id: "active" });
    await receive({
      type: "session_title_updated", session_id: session.session_id,
      title: "分析会话加载", title_source: "generated",
    });
    expect(onTitleChange).toHaveBeenCalledWith(session.session_id, "分析会话加载");
    expect(onTurnEnd).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });



  async function select(filename: string, text: string) {
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    Object.defineProperty(input, "files", { configurable: true, value: [{ name: filename, arrayBuffer: async () => new TextEncoder().encode(text).buffer }] });
    await act(async () => input.dispatchEvent(new Event("change", { bubbles: true })));
  }

  const refresh = () => container.querySelector<HTMLButtonElement>('[aria-label="重新加载运行环境"]')!;
  const run = () => container.querySelector<HTMLButtonElement>(".json-run")!;

  it("refreshes with a button and keeps the selected JSON for the next turn", async () => {
    await select("example.json", '{"items":[1,2]}');
    expect(container.querySelector("textarea")).toBeNull();
    expect(container.textContent).toContain("example.json");
    expect(container.querySelector("pre")?.textContent).toBe('{"items":[1,2]}');
    await act(async () => refresh().click());
    expect(sent).toEqual([{ type: "runtime_refresh" }]);
    expect(run().disabled).toBe(true);
    await receive({ type: "runtime_refreshed", provider: "test", model: "test/model" });
    await receive(snapshot);
    await act(async () => run().click());
    expect(sent.at(-1)).toEqual({
      type: "user_turn", turn_id: "turn-1",
      json_input: { filename: "example.json", text: '{"items":[1,2]}' },
    });
  });

  it("rejects invalid JSON and allows a valid replacement to run when idle", async () => {
    await select("invalid.json", "{bad}");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("有效的 UTF-8 JSON");
    expect(run().disabled).toBe(true);
    await receive({ ...snapshot, phase: "running", turn_id: "active" });
    await select("replacement.JSON", '{"n":9007199254740993}');
    expect(run().disabled).toBe(true);
    await act(async () => run().click());
    expect(sent).toEqual([]);
    await receive(snapshot);
    await act(async () => run().click());
    expect(sent).toEqual([{
      type: "user_turn", turn_id: "turn-1",
      json_input: { filename: "replacement.JSON", text: '{"n":9007199254740993}' },
    }]);
  });
});
