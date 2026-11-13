import { useCallback, useMemo, useRef, useState, useEffect } from "react";
import { useExternalStoreRuntime } from "@assistant-ui/react";
import { initialRuntimeState, runtimeIsActive, type CacheUsage, type ContextWindow, type PlanSnapshot, type RuntimePhase, type SceneMode, type SceneTask } from "@rade/protocol";
import { releaseActiveSession, type ModelOption, type Session } from "../api";
import { SessionSocket } from "../session";
import { applyMessage, isTurnActivity, toMessages, type Feedback, type TranscriptItem } from "../transcript";
import { jobKindLabel, shouldShowPlan, updateBackgroundJobs, userMessagePreview, type BackgroundJob } from "./runtimeState";

export type SessionRuntimeOptions = {
  session: Session;
  selected: boolean;
  contextWindow: ContextWindow | null;
  backgroundActive: boolean;
  providers: ModelOption[];
  provider?: string;
  onProviderChange: (provider: string) => void;
  onBusyChange: (busy: boolean) => void;
  onContextWindowChange: (window: ContextWindow | null) => void;
  onSessionAvailable: () => void;
  onRunReady?: () => void;
  onTurnEnd: () => void;
};

export function useSessionRuntime({
  session,
  selected,
  contextWindow,
  backgroundActive,
  providers,
  provider,
  onProviderChange,
  onBusyChange,
  onContextWindowChange,
  onSessionAvailable,
  onRunReady,
  onTurnEnd,
}: SessionRuntimeOptions) {
  const [items, setItems] = useState<TranscriptItem[]>(session.items);
  const [cacheUsage, setCacheUsage] = useState<CacheUsage | null>(session.cache_usage ?? null);
  const [pendingUserItem, setPendingUserItem] = useState<TranscriptItem | null>(null);
  const [runtimePhase, setRuntimePhase] = useState<RuntimePhase>(() => initialRuntimeState().phase);
  const [sceneTask, setSceneTask] = useState<SceneTask | null>(session.scene_task ?? null);
  const [jobs, setJobs] = useState<Record<string, BackgroundJob>>({});
  const [attaching, setAttaching] = useState(false);
  const [attachmentReplaced, setAttachmentReplaced] = useState(false);
  const [toast, setToast] = useState<Extract<Feedback, { kind: "toast" }> | null>(null);
  const [alerts, setAlerts] = useState<Record<string, Extract<Feedback, { kind: "alert" }>>>({});
  const [reconnecting, setReconnecting] = useState(false);
  const [plan, setPlan] = useState<PlanSnapshot | null>(session.plan ?? null);
  const [pendingProvider, setPendingProvider] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const socketRef = useRef<SessionSocket | null>(null);
  const socketModelRef = useRef("");
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  const toastTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const turnCounter = useRef(0);
  const activeTurnIdRef = useRef<string | null>(null);
  const eventCounterRef = useRef(session.event_sequence ?? 0);
  const [attachmentId] = useState(() => crypto.randomUUID());
  // Socket callbacks fire outside React's render, so the transcript and
  // turn state they fold onto are kept in refs.
  const itemsRef = useRef(items);
  const pendingUserItemRef = useRef(pendingUserItem);
  const runtimePhaseRef = useRef<RuntimePhase>(runtimePhase);
  const attachmentReplacedRef = useRef(false);
  const awaitingSessionActivityRef = useRef(false);

  const messages = useMemo(() => toMessages(
    pendingUserItem ? [...items, pendingUserItem] : items,
  ), [items, pendingUserItem]);
  const turnPreviews = useMemo(() => messages
    .filter((message) => message.role === "user")
    .map((message) => userMessagePreview(message)), [messages]);
  const providerSaving = pendingProvider !== null;
  const configurationPending = providerSaving || refreshing;
  const displayedProvider = pendingProvider ?? provider ?? "";
  const running = runtimeIsActive(runtimePhase);
  const backgroundDisconnected = backgroundActive && socketRef.current === null;
  const controlsDisabled = attachmentReplaced || backgroundDisconnected || pendingUserItem !== null || refreshing;
  const sessionRunning = running || backgroundActive || pendingUserItem !== null || refreshing;

  const canSubmitScene = !sessionRunning && !controlsDisabled && !configurationPending && !attaching;


  const showItems = useCallback((next: TranscriptItem[]) => {
    itemsRef.current = next;
    setItems(next);
  }, []);

  const showPendingUserItem = useCallback((next: TranscriptItem | null) => {
    pendingUserItemRef.current = next;
    setPendingUserItem(next);
  }, []);

  const applyRuntimePhase = useCallback((phase: RuntimePhase) => {
    runtimePhaseRef.current = phase;
    setRuntimePhase(phase);
    onBusyChange(runtimeIsActive(phase));
  }, [onBusyChange]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      socketRef.current?.close();
      socketRef.current = null;
      if (toastTimeoutRef.current) clearTimeout(toastTimeoutRef.current);
    };
  }, []);

  useEffect(() => {
    const releaseOnPageLeave = () => {
      if (!runtimeIsActive(runtimePhaseRef.current)) {
        void releaseActiveSession(session.session_id, socketModelRef.current || provider, attachmentId).catch(() => undefined);
      }
    };
    window.addEventListener("pagehide", releaseOnPageLeave);
    return () => window.removeEventListener("pagehide", releaseOnPageLeave);
  }, [attachmentId, provider, session.session_id]);

  // A view can detach without destroying its Session's Bridge and MCP clients.
  useEffect(() => {
    if ((selected || backgroundActive) && socketRef.current === null && !attachmentReplacedRef.current) {
      connect({ attachOnly: !selected });
    }
  }, [backgroundActive, provider, selected, session.session_id]);

  useEffect(() => {
    if (selected || running || pendingUserItem !== null || configurationPending || socketRef.current === null) return;
    const socket = socketRef.current;
    socketRef.current = null;
    socketModelRef.current = "";
    socket.close();
  }, [attachmentId, configurationPending, pendingUserItem, provider, running, selected, session.session_id]);

  const showToast = useCallback((next: Extract<Feedback, { kind: "toast" }>) => {
    if (toastTimeoutRef.current) clearTimeout(toastTimeoutRef.current);
    setToast(next);
    toastTimeoutRef.current = setTimeout(() => {
      toastTimeoutRef.current = null;
      setToast(null);
    }, 4000);
  }, []);

  const showAlert = useCallback((next: Extract<Feedback, { kind: "alert" }>) => {
    setAlerts((current) => ({ ...current, [next.id]: next }));
  }, []);

  const endTurn = useCallback(() => {
    activeTurnIdRef.current = null;
    awaitingSessionActivityRef.current = false;
    setJobs({});
    setReconnecting(false);
    onTurnEnd();
  }, [onTurnEnd]);

  function clearPendingSettings() {
    setPendingProvider(null);
    setRefreshing(false);
  }

  function connect({ attachOnly = false, takeover = false }: { attachOnly?: boolean; takeover?: boolean } = {}): SessionSocket {
    setAttaching(true);
    let socket: SessionSocket;
    socket = new SessionSocket({
      sessionId: session.session_id,
      provider,
      attachmentId,
      attachOnly,
      afterEvent: eventCounterRef.current,
      takeover,
      onConfigurationRejected: (type) => {
        if (socketRef.current !== socket) return;
        if (type === "runtime_refresh") {
          setRefreshing(false);
          showToast({ kind: "toast", level: "info", text: "请在当前任务结束后重新加载。" });
          return;
        }
        if (type === "provider_set") setPendingProvider(null);
        showToast({ kind: "toast", level: "info", text: "会话正在执行任务，本次模型切换未应用，请在任务结束后重试。" });
      },
      onMessage: (message) => {
        // A socket that was replaced during a model switch may still have
        // messages queued in the browser event loop. Ignore those messages
        // so stale transcript, feedback, usage, and turn callbacks cannot
        // mutate the active connection's state.
        if (socketRef.current !== socket) return;
        if (message.type === "session_open_failed") {
          clearPendingSettings();
          setAttaching(false);
          setReconnecting(false);
          setJobs({});
          applyRuntimePhase("inactive");
          eventCounterRef.current = 0;
        }
        if (message.type === "scene_task_received" && message.turn_id === activeTurnIdRef.current && pendingUserItemRef.current) {
          // Opening checkpoints update history underneath the pending bubble.
          // Acceptance commits it once; later checkpoints replace it normally.
          showItems([...itemsRef.current, pendingUserItemRef.current]);
          showPendingUserItem(null);
          applyRuntimePhase("starting");
        }
        if (message.type === "attachment_replaced" || message.type === "fatal") clearPendingSettings();
        if (message.type === "attachment_replaced") {
          if (toastTimeoutRef.current) {
            clearTimeout(toastTimeoutRef.current);
            toastTimeoutRef.current = null;
          }
          setToast(null);
          setJobs({});
          setReconnecting(false);
          socketRef.current = null;
          socketModelRef.current = "";
          attachmentReplacedRef.current = true;
          setAttachmentReplaced(true);
          setAttaching(false);
          applyRuntimePhase(message.phase);
          socket.close();
          return;
        }
        if (message.type === "runtime_state") {
          if (message.replayed) return;
          if (message.phase !== "failed") {
            setAlerts((current) => {
              const next = { ...current };
              delete next.connection;
              delete next["session-open"];
              return next;
            });
          }
          if (message.event_sequence !== undefined) eventCounterRef.current = message.event_sequence;
          if (toastTimeoutRef.current) {
            clearTimeout(toastTimeoutRef.current);
            toastTimeoutRef.current = null;
          }
          setToast(null);
          setReconnecting(false);
          setAttaching(false);
          attachmentReplacedRef.current = false;
          setAttachmentReplaced(false);
          if (message.provider) {
            onRunReady?.();
            socketModelRef.current = message.provider;
            onProviderChange(message.provider);
          }
          applyRuntimePhase(message.phase);
          setSceneTask(message.scene_task ?? null);
          if (message.phase === "starting" && message.turn_id === null) setRefreshing(true);
          if (!pendingUserItemRef.current) activeTurnIdRef.current = message.turn_id;
          setJobs(Object.fromEntries(message.jobs.map((job) => [job.job_id, job])));
          setPlan(message.plan ?? null);
          onContextWindowChange(message.context_window);
          setCacheUsage(message.cache_usage);
          if (message.runtime_warnings?.length) {
            showAlert({ kind: "alert", id: "runtime-warnings", level: "warning", text: message.runtime_warnings.join("\n") });
          }
          return;
        }
        if (message.type !== "session_open_failed" && typeof message.event_sequence === "number") eventCounterRef.current = message.event_sequence;
        if (message.type === "runtime_refreshed" || message.type === "runtime_refresh_failed") {
          setRefreshing(false);
          if (message.replayed) return;
          if (message.type === "runtime_refreshed") {
            showToast({ kind: "toast", level: "info", text: "运行环境已重新加载。" });
          } else {
            showAlert({ kind: "alert", id: "runtime-refresh", level: "error", text: `重新加载失败：${message.error.message}` });
          }
          return;
        }
        if (message.type === "mcp_server_status" && message.status === "unavailable" && !message.replayed) {
          showAlert({ kind: "alert", id: `mcp:${message.server}`, level: "warning", text: `MCP ${message.server} 连接失败：${message.error ?? "服务不可用"}` });
        }
        if (message.type === "job_status") {
          setJobs((current) => updateBackgroundJobs(current, message));
          if (message.status === "failed") {
            showAlert({ kind: "alert", id: `job:${message.job_id}`, level: "error", text: `${jobKindLabel(message.kind)}任务失败。` });
          }
        }
        if (message.type === "hook_completed" && !message.replayed
            && (message.status === "failed" || message.decision === "deny")) {
          showAlert({ kind: "alert", id: `hook:${message.invocation_id}`,
            level: message.status === "failed" ? "error" : "warning",
            text: message.reason ?? `${message.hook_id}: ${message.status}` });
        }
        if (message.type === "session_ready") {
          onRunReady?.();
          attachmentReplacedRef.current = false;
          setAttachmentReplaced(false);
          setReconnecting(false);
          if (toastTimeoutRef.current) {
            clearTimeout(toastTimeoutRef.current);
            toastTimeoutRef.current = null;
          }
          setToast(null);
        }
        if (message.type === "plan_updated") setPlan(message.plan);
        if (message.type === "provider_changed") {
          if (!message.replayed) setPendingProvider(null);
          socketModelRef.current = message.provider;
          onProviderChange(message.provider);
          onContextWindowChange(null);
        }
        if (awaitingSessionActivityRef.current && isTurnActivity(message)) {
          awaitingSessionActivityRef.current = false;
          onSessionAvailable();
        }
        if ("task" in message) {
          if (!message.replayed) setSceneTask(message.task);
          if (awaitingSessionActivityRef.current) {
            awaitingSessionActivityRef.current = false;
            onSessionAvailable();
          }
        }
        const applied = applyMessage(itemsRef.current, message);
        showItems(applied.items);
        if (applied.feedback?.kind === "toast") showToast(applied.feedback);
        if (applied.feedback?.kind === "alert") showAlert(applied.feedback);
        if (applied.contextWindow !== undefined) onContextWindowChange(applied.contextWindow);
        if (applied.cacheUsage !== undefined) setCacheUsage(applied.cacheUsage);
        if (applied.finished) endTurn();
      },
      onClose: () => {
        // A stale socket can close after a replacement has already been
        // installed (for example after changing the model). Do not clear
        // the newer connection in that case.
        if (socketRef.current !== socket) return;
        socketRef.current = null;
        socketModelRef.current = "";
        setAttaching(false);
        clearPendingSettings();
        if (runtimeIsActive(runtimePhaseRef.current) && !attachmentReplacedRef.current) {
          setReconnecting(true);
          reconnectTimeoutRef.current = setTimeout(() => {
            reconnectTimeoutRef.current = null;
            if (mountedRef.current && runtimeIsActive(runtimePhaseRef.current) && socketRef.current === null && !attachmentReplacedRef.current) {
              connect({ attachOnly: true });
            }
          }, 1000);
        }
      },
      onError: () => {
        // Browsers report an error before closing a WebSocket that went
        // stale while the page was idle. That is not an interrupted turn;
        // leave the idle UI quiet. The close callback clears the socket so
        // the next message can establish a fresh connection.
        if (socketRef.current !== socket) return;
        if (!runtimeIsActive(runtimePhaseRef.current)) showAlert({ kind: "alert", id: "connection", level: "error", text: "无法连接 RADE。" });
      },
    });
    socketRef.current = socket;
    socketModelRef.current = provider ?? "";
    return socket;
  }

  function takeOverAttachment() {
    if (attaching || socketRef.current !== null) return;
    connect({ attachOnly: true, takeover: true });
  }

  async function submitScene(filename: string, mode: SceneMode): Promise<boolean> {
    if (!canSubmitScene || runtimeIsActive(runtimePhaseRef.current)) return false;
    const startsSession = !itemsRef.current.some(
      (item) => item.role === "user"
        && (item.origin === undefined || item.origin === "conversation"),
    );
    showPendingUserItem({ role: "user", content: filename });
    applyRuntimePhase("starting");
    if (toastTimeoutRef.current) clearTimeout(toastTimeoutRef.current);
    setToast(null);
    setAlerts({});
    setJobs({});
    setReconnecting(false);
    turnCounter.current += 1;
    activeTurnIdRef.current = `turn-${turnCounter.current}`;
    const socket = socketRef.current ?? connect();
    awaitingSessionActivityRef.current = startsSession;
    try {
      await socket.sendSceneTask({ type: "scene_task_start", turn_id: activeTurnIdRef.current, scene_input: filename, mode });
    } catch (error) {
      showPendingUserItem(null);
      activeTurnIdRef.current = null;
      awaitingSessionActivityRef.current = false;
      applyRuntimePhase("inactive");
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      setReconnecting(false);
      showAlert({ kind: "alert", id: "scene-submit", level: "error", text: String(error) });
      return false;
    }
    return true;
  }

  function changeProvider(nextProvider: string) {
    setPendingProvider(nextProvider);
    (socketRef.current ?? connect()).send({ type: "provider_set", provider: nextProvider });
  }

  function refreshRuntime(): boolean {
    if (controlsDisabled || attaching || configurationPending) return false;
    if (sessionRunning || runtimeIsActive(runtimePhaseRef.current)) {
      showToast({ kind: "toast", level: "info", text: "请在当前任务结束后重新加载。" });
      return false;
    }
    setRefreshing(true);
    setAlerts((current) => Object.fromEntries(Object.entries(current).filter(
      ([id]) => id !== "runtime-refresh" && id !== "runtime-warnings" && !id.startsWith("mcp:"),
    )));
    (socketRef.current ?? connect()).send({ type: "runtime_refresh" });
    return true;
  }

  function stop() {
    const turnId = activeTurnIdRef.current;
    if (turnId) socketRef.current?.send({ type: "cancel", turn_id: turnId });
  }

  const assistantRuntime = useExternalStoreRuntime({
    messages,
    convertMessage: (message) => message,
    isRunning: running,
    isSendDisabled: true,
    onNew: async () => {},
  });

  return {
    assistantRuntime,
    messages,
    turnPreviews,
    running,
    runtimePhase,
    sceneTask,
    jobs: Object.values(jobs),
    attaching,
    attachmentReplaced,
    toast,
    alerts: Object.values(alerts),
    reconnecting,
    plan: shouldShowPlan(plan) ? plan : null,
    displayedProvider,
    providerSaving,
    refreshing,
    canSubmitScene,
    controlsDisabled,
    sessionRunning,
    dismissAlert: (id: string) => setAlerts((current) => {
      const next = { ...current };
      delete next[id];
      return next;
    }),
    submitScene,
    takeOverAttachment,
    changeProvider,
    refreshRuntime,
    stop,
    contextWindow,
    providers,
    cacheUsage,
  };
}

export type SessionRuntime = ReturnType<typeof useSessionRuntime>;
