import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bot, Boxes, Brain, ChevronDown, ChevronRight, KeyRound, LoaderCircle, MessageSquare, PanelLeftClose, Plug, Plus, Settings as SettingsIcon, Sparkles, Trash2 } from "lucide-react";
import { Chat } from "./Chat";
import { Settings, type SettingsSection } from "./Settings";
import { RunFiles } from "./RunFiles";
import { SessionSearch } from "./SessionSearch";
import { formatSessionStart, sessionLabel } from "@rade/protocol/internal";
import { runtimeIsActive, type ContextWindow, type RuntimePhase } from "@rade/protocol";
import { deleteSession, get, sessionUrl, type ModelOption, type ModelOptions, type Session, type SessionSummary } from "./api";

type ActiveSession = Session & { provider: string | null; phase: RuntimePhase };
const SELECTED_SESSION_KEY = "RADE.selectedSessionId";

function newSession(): Session {
  return {
    session_id: crypto.randomUUID(),
    items: [],
  };
}

export function App() {
  const [rememberedSessionId] = useState(() => localStorage.getItem(SELECTED_SESSION_KEY));
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [chatSessions, setChatSessions] = useState<Session[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState(rememberedSessionId ?? "");
  const [restoringSession, setRestoringSession] = useState(true);
  const [busyBySession, setBusyBySession] = useState<Record<string, boolean>>({});
  const [contextBySession, setContextBySession] = useState<Record<string, ContextWindow | null>>({});
  const [loadingSessionId, setLoadingSessionId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [filesVersion, setFilesVersion] = useState(0);
  const [readyRuns, setReadyRuns] = useState<Set<string>>(new Set());
  const [providers, setProviders] = useState<ModelOption[]>([]);
  const [defaultProvider, setDefaultProvider] = useState<string>();
  const [providerBySession, setProviderBySession] = useState<Record<string, string>>({});
  const [settingsSection, setSettingsSection] = useState<SettingsSection | null>(null);
  const [settingsMenuOpen, setSettingsMenuOpen] = useState(false);
  const [sessionsCollapsed, setSessionsCollapsed] = useState(false);
  const [filesCollapsed, setFilesCollapsed] = useState(false);
  const [searchTarget, setSearchTarget] = useState<{ sessionId: string; itemIndex: number; requestId: string } | null>(null);
  const settingsMenuRef = useRef<HTMLDivElement>(null);

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await get<SessionSummary[]>("/api/sessions"));
    } catch (error) {
      setError(String(error));
    }
  }, []);

  useEffect(() => { void refreshSessions(); }, [refreshSessions]);
  useEffect(() => {
    let cancelled = false;
    const restoreSession = async () => {
      try {
        const restored = rememberedSessionId
          ? await get<Session>(sessionUrl(rememberedSessionId))
          : newSession();
        if (cancelled) return;
        setChatSessions((all) => [
          restored,
          ...all.filter((item) => item.session_id !== restored.session_id),
        ]);
        setSelectedSessionId(restored.session_id);
        if (restored.provider) {
          setProviderBySession((all) => ({ ...all, [restored.session_id]: restored.provider! }));
        }
        if (restored.context_window) {
          setContextBySession((all) => ({ ...all, [restored.session_id]: restored.context_window ?? null }));
        }
      } catch (error) {
        if (cancelled) return;
        const fresh = newSession();
        setChatSessions((all) => [fresh, ...all]);
        setSelectedSessionId(fresh.session_id);
        setError(String(error));
      } finally {
        if (!cancelled) setRestoringSession(false);
      }
    };
    void restoreSession();
    return () => { cancelled = true; };
  }, [rememberedSessionId]);
  useEffect(() => {
    if (!restoringSession && selectedSessionId) {
      localStorage.setItem(SELECTED_SESSION_KEY, selectedSessionId);
    }
  }, [restoringSession, selectedSessionId]);
  const refreshModels = useCallback(() => {
    get<ModelOptions>("/api/models").then((options) => {
      setProviders(options.models);
      setDefaultProvider(options.default);
    }).catch((error) => setError(String(error)));
  }, []);
  useEffect(() => { refreshModels(); }, [refreshModels]);
  useEffect(() => {
    if (!settingsMenuOpen) return;
    const closeMenu = (event: MouseEvent) => {
      if (!settingsMenuRef.current?.contains(event.target as Node)) setSettingsMenuOpen(false);
    };
    document.addEventListener("mousedown", closeMenu);
    return () => document.removeEventListener("mousedown", closeMenu);
  }, [settingsMenuOpen]);
  useEffect(() => {
    get<ActiveSession[]>("/api/active-sessions").then(async (activeSessions) => {
      setBusyBySession((all) => ({
        ...all,
        ...Object.fromEntries(activeSessions.map((session) => [session.session_id, runtimeIsActive(session.phase)])),
      }));
      setProviderBySession((all) => ({
        ...all,
        ...Object.fromEntries(activeSessions.flatMap((session) => session.provider ? [[session.session_id, session.provider]] : [])),
      }));
      setContextBySession((all) => ({
        ...all,
        ...Object.fromEntries(activeSessions.map((session) => [session.session_id, session.context_window ?? null])),
      }));
      setChatSessions((all) => [
        ...all,
        ...activeSessions.filter((active) => !all.some((item) => item.session_id === active.session_id)),
      ]);
    }).catch(() => undefined);
  }, []);

  const selectedSession = useMemo(
    () => chatSessions.find((item) => item.session_id === selectedSessionId) ?? chatSessions[0],
    [chatSessions, selectedSessionId],
  );
  async function selectSession(id: string) {
    setSearchTarget(null);
    setSettingsSection(null);
    setSettingsMenuOpen(false);
    if (chatSessions.some((item) => item.session_id === id)) {
      setSelectedSessionId(id);
      setError("");
      return true;
    }
    setLoadingSessionId(id);
    setError("");
    try {
      const loaded = await get<Session>(sessionUrl(id));
      setChatSessions((all) => all.some((item) => item.session_id === id) ? all : [...all, loaded]);
      if (loaded.provider) {
        setProviderBySession((all) => ({ ...all, [id]: loaded.provider! }));
      }
      if (loaded.context_window) {
        setContextBySession((all) => ({ ...all, [id]: loaded.context_window ?? null }));
      }
      setSelectedSessionId(id);
      return true;
    } catch (error) {
      setError(String(error));
      return false;
    } finally {
      setLoadingSessionId(null);
    }
  }

  function createNewChat() {
    setSearchTarget(null);
    const fresh = newSession();
    // Add the new chat to the stable mounted set before making it visible.
    setChatSessions((all) => [...all, fresh]);
    setSelectedSessionId(fresh.session_id);
    setSettingsSection(null);
    setSettingsMenuOpen(false);
    setError("");
  }

  async function removeSession(id: string) {
    if (busyBySession[id] || !window.confirm("确定删除这个会话吗？此操作无法撤销。")) return;
    setLoadingSessionId(id);
    setError("");
    try {
      await deleteSession(id);
      const remaining = chatSessions.filter((item) => item.session_id !== id);
      if (selectedSessionId === id) {
        const next = remaining[0] ?? newSession();
        setChatSessions(remaining.length ? remaining : [next]);
        setSelectedSessionId(next.session_id);
      } else {
        setChatSessions(remaining);
      }
      setBusyBySession((all) => { const next = { ...all }; delete next[id]; return next; });
      setContextBySession((all) => { const next = { ...all }; delete next[id]; return next; });
      setProviderBySession((all) => { const next = { ...all }; delete next[id]; return next; });
      setReadyRuns((all) => {
        const next = new Set(all);
        next.delete(id);
        return next;
      });
      await refreshSessions();
    } catch (error) {
      setError(String(error));
    } finally {
      setLoadingSessionId(null);
    }
  }

  async function refreshSession(sessionId: string) {
    try {
      const loaded = await get<Session>(sessionUrl(sessionId));
      setChatSessions((all) => all.map((item) => item.session_id === sessionId ? loaded : item));
    } catch (error) {
      setError(String(error));
    }
  }

  const selectedSummary = sessions.find((item) => item.session_id === selectedSessionId);
  const selectedLabel = selectedSummary ? sessionLabel(selectedSummary) : "新建运行";
  const busy = Boolean(busyBySession[selectedSessionId]);
  const contextWindow = contextBySession[selectedSessionId] ?? selectedSession?.context_window ?? null;
  const settingsItems: { id: SettingsSection; label: string; description: string; icon: typeof Bot }[] = [
    { id: "providers", label: "Providers", description: "模型、密钥和路由", icon: KeyRound },
    { id: "agent", label: "Agent", description: "工具和运行时设置", icon: Bot },
    { id: "memory", label: "Memory", description: "查看长期记忆", icon: Brain },
    { id: "skills", label: "Skills", description: "查看可用技能", icon: Sparkles },
    { id: "plugins", label: "Plugins", description: "查看已安装扩展", icon: Boxes },
    { id: "mcp", label: "MCP", description: "查看外部工具服务", icon: Plug },
  ];
  const settingsPopover = settingsMenuOpen && <div className="settings-popover" role="menu">
    <div className="settings-popover-label">Settings</div>
    {settingsItems.map((item) => { const Icon = item.icon; return <button type="button" role="menuitem" key={item.id} onClick={() => { setSettingsSection(item.id); setSettingsMenuOpen(false); }}><Icon size={15} /><span><strong>{item.label}</strong><small>{item.description}</small></span><ChevronRight size={13} /></button>; })}
  </div>;
  const sessionRow = (item: SessionSummary) => {
    const label = sessionLabel(item);
    return <div className={`session-row ${item.session_id === selectedSessionId ? "selected" : ""}`} key={item.session_id}>
      <button className="session-button" title={label} disabled={loadingSessionId === item.session_id} onClick={() => void selectSession(item.session_id)}>
        {busyBySession[item.session_id] ? <LoaderCircle size={15} className="spin" role="img" aria-label="正在执行任务" /> : <MessageSquare size={15} />}
        <span className="session-input">
          <span>{item.input_preview ?? item.session_id}</span>
          {item.started_at && <time dateTime={item.started_at}>{formatSessionStart(item.started_at)}</time>}
        </span>
      </button>
      <button className="delete-session" aria-label={`删除会话 ${label}`} title="删除会话" disabled={loadingSessionId === item.session_id || Boolean(busyBySession[item.session_id])} onClick={(event) => { event.stopPropagation(); void removeSession(item.session_id); }}>
        <Trash2 size={14} />
      </button>
    </div>;
  };

  return (
    <div className={`app-shell ${sessionsCollapsed ? "sessions-collapsed" : ""} ${filesCollapsed ? "run-files-collapsed" : ""}`}>
      <aside className={`sessions-panel ${sessionsCollapsed ? "collapsed" : ""}`} aria-label="Sessions">
        <div className={`sessions-header ${sessionsCollapsed ? "rail" : "brand"}`}>
          {sessionsCollapsed
            ? <button className="sidebar-rail-logo" aria-label="展开会话栏" title="展开会话栏" onClick={() => setSessionsCollapsed(false)}><img src="/RADE-avatar-128.png" alt="" /></button>
            : <span>RADE<span className="brand-dot">.</span></span>}
          <div className="sessions-header-actions">
            <SessionSearch collapsed={sessionsCollapsed} onSelect={async (match) => {
              if (!await selectSession(match.session_id)) return false;
              setSearchTarget({ sessionId: match.session_id, itemIndex: match.item_index, requestId: crypto.randomUUID() });
              return true;
            }} />
            {!sessionsCollapsed && <button className="sidebar-collapse-button" aria-label="收起会话栏" title="收起会话栏" onClick={() => { setSessionsCollapsed(true); setSettingsMenuOpen(false); }}><PanelLeftClose size={17} /></button>}
          </div>
        </div>
        {sessionsCollapsed ? <>
          <button className="sidebar-rail-button" aria-label="新建运行" title="新建运行" onClick={() => createNewChat()}><Plus size={19} /></button>
          <div className="settings-menu-wrap rail" ref={settingsMenuRef}>
            {settingsPopover}
            <button className={`sidebar-rail-button ${settingsSection ? "selected" : ""}`} aria-label="设置" title="Settings" aria-expanded={settingsMenuOpen} onClick={() => setSettingsMenuOpen((open) => !open)}><SettingsIcon size={17} /></button>
          </div>
        </> : <>
          <div className="new-chat-actions">
            <button className="new-chat" onClick={() => createNewChat()}><Plus size={17} /> 新建运行</button>
          </div>
          <nav className="session-list">
            <div className="section-label">运行历史</div>
            {sessions.map(sessionRow)}
            {sessions.length === 0 && <p className="session-empty">暂无运行记录。</p>}
          </nav>
          <div className="settings-menu-wrap" ref={settingsMenuRef}>
            {settingsPopover}
            <button className={`sidebar-footer ${settingsSection ? "selected" : ""}`} aria-expanded={settingsMenuOpen} onClick={() => setSettingsMenuOpen((open) => !open)}><SettingsIcon size={14} /> Settings<ChevronDown size={12} /></button>
          </div>
        </>}
      </aside>

      {settingsSection && <Settings section={settingsSection} onClose={() => { setSettingsSection(null); void refreshSessions(); }} onChanged={refreshModels} />}
      <main className="chat-panel" style={{ display: settingsSection ? "none" : undefined }}>
        <header className="chat-header"><div className="breadcrumb">Chat <ChevronRight size={14} /><span>{restoringSession ? "…" : selectedLabel}</span></div><span className="status-label"><span className={`status-dot ${busy ? "working" : ""}`} />{busy ? "Working" : "Ready"}</span></header>
        {error && <div className="error-banner" role="alert">{error}</div>}
        {!restoringSession && chatSessions.map((current) => {
          const provider = providerBySession[current.session_id] ?? defaultProvider;
          return <div key={current.session_id} style={{ display: current.session_id === selectedSessionId ? "contents" : "none" }}><Chat session={current} selected={current.session_id === selectedSessionId} searchTarget={searchTarget?.sessionId === current.session_id ? searchTarget : undefined} contextWindow={current.session_id === selectedSessionId ? contextWindow : contextBySession[current.session_id] ?? current.context_window ?? null} backgroundActive={Boolean(busyBySession[current.session_id])}
            providers={providers} provider={provider} onProviderChange={(value) => setProviderBySession((all) => ({ ...all, [current.session_id]: value }))} onBusyChange={(value) => {
              setBusyBySession((all) => ({ ...all, [current.session_id]: value }));
            }} onContextWindowChange={(value) => setContextBySession((all) => ({ ...all, [current.session_id]: value }))} onTurnEnd={() => {
            void refreshSessions();
            void refreshSession(current.session_id);
            setFilesVersion((value) => value + 1);
          }} onSessionAvailable={() => { void refreshSessions(); }} onRunReady={() => {
            setReadyRuns((all) => all.has(current.session_id) ? all : new Set([...all, current.session_id]));
          }} /></div>;
        })}
      </main>
      {!settingsSection && selectedSession && <RunFiles version={filesVersion} sessionId={selectedSession.session_id} ready={readyRuns.has(selectedSession.session_id)} collapsed={filesCollapsed} onCollapsedChange={setFilesCollapsed} />}
    </div>
  );
}
