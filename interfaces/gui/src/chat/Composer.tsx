import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { AlertTriangle, Check, ChevronDown, FileJson, Folder, LoaderCircle, Plus, RefreshCw, X } from "lucide-react";
import { get } from "../api";
import { ContextWindowIndicator, RuntimeIndicators } from "./RuntimeIndicators";
import type { SessionRuntime } from "./useSessionRuntime";

type ScenePreview = { text: string; truncated: boolean };

export function Composer({ runtime }: { runtime: SessionRuntime }) {
  const refreshDisabled = !runtime.workspaceSelected || runtime.controlsDisabled || runtime.sessionRunning
    || runtime.attaching || runtime.providerSaving || runtime.workspaceSaving || runtime.refreshing;
  const [workspaceEditing, setWorkspaceEditing] = useState(false);
  const [workspaceMenuBounds, setWorkspaceMenuBounds] = useState({ maxHeight: 360, maxWidth: 320 });
  const workspaceMenuId = useId();
  const workspaceGroups = [
    { label: "正式工作区", options: runtime.availableWorkspaces.filter((option) => !option.scratch) },
    { label: "临时工作区", options: runtime.availableWorkspaces.filter((option) => option.scratch) },
  ];
  const workspacePickerRef = useRef<HTMLDivElement | null>(null);
  const feedbackRef = useRef<HTMLDivElement | null>(null);
  const [filenames, setFilenames] = useState<string[] | null>(null);
  const [listRevision, setListRevision] = useState(0);
  const [listError, setListError] = useState("");
  const [filename, setFilename] = useState("");
  const [preview, setPreview] = useState<ScenePreview | null>(null);
  const [previewError, setPreviewError] = useState("");

  useEffect(() => {
    const request = new AbortController();
    get<string[]>("/api/input-scenes", request.signal).then((names) => {
      if (request.signal.aborted) return;
      setFilenames(names);
      setListError("");
      setFilename((current) => names.includes(current) ? current : "");
    }).catch((error: Error) => {
      if (!request.signal.aborted) setListError(error.message);
    });
    return () => request.abort();
  }, [listRevision]);

  useEffect(() => {
    if (!filename) return;
    const request = new AbortController();
    const query = new URLSearchParams({ filename });
    get<ScenePreview>(`/api/input-scenes/preview?${query}`, request.signal).then((result) => {
      if (!request.signal.aborted) setPreview(result);
    }).catch((error: Error) => {
      if (!request.signal.aborted) setPreviewError(error.message);
    });
    return () => request.abort();
  }, [filename]);
  useLayoutEffect(() => {
    const feedback = feedbackRef.current!;
    const thread = feedback.closest<HTMLElement>(".thread")!;
    const updateHeight = () => thread.style.setProperty("--composer-feedback-height", `${feedback.offsetHeight}px`);
    updateHeight();
    const observer = new ResizeObserver(updateHeight);
    observer.observe(feedback);
    return () => {
      observer.disconnect();
      thread.style.removeProperty("--composer-feedback-height");
    };
  }, []);

  useLayoutEffect(() => {
    if (!workspaceEditing) return;
    const picker = workspacePickerRef.current!;
    const updateBounds = () => {
      const bounds = picker.getBoundingClientRect();
      setWorkspaceMenuBounds({ maxHeight: Math.max(0, bounds.top - 12), maxWidth: window.innerWidth - bounds.left - 12 });
    };
    updateBounds();
    const observer = new ResizeObserver(updateBounds);
    observer.observe(picker.closest(".composer-area")!);
    window.addEventListener("resize", updateBounds);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", updateBounds);
    };
  }, [workspaceEditing]);

  useEffect(() => {
    if (!workspaceEditing) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!workspacePickerRef.current?.contains(event.target as Node)) setWorkspaceEditing(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [workspaceEditing]);

  return <div className="composer-area">
    {runtime.attachmentReplaced && <div className="attachment-replaced" role="status"><span>此会话由另一个页面控制，可在此页面手动接管。</span><button type="button" onClick={runtime.takeOverAttachment} disabled={runtime.attaching}>{runtime.attaching ? "正在接管…" : "在此页面接管"}</button></div>}
    <div ref={feedbackRef} className="composer-feedback">
      {runtime.alerts.length > 0 && <div className="feedback-alerts">{runtime.alerts.map((item) => <div className={`feedback-alert ${item.level}`} role="alert" key={item.id}><AlertTriangle size={15} /><span>{item.text}</span><button type="button" aria-label="关闭提示" onClick={() => runtime.dismissAlert(item.id)}><X size={13} /></button></div>)}</div>}
      {runtime.toast && <div className={`feedback-toast ${runtime.toast.level}`} role="status">{runtime.toast.text}</div>}
      {!runtime.attachmentReplaced && <RuntimeIndicators
        running={runtime.running || runtime.refreshing}
        refreshing={runtime.refreshing}
        reconnecting={runtime.reconnecting}
        runtimePhase={runtime.runtimePhase}
        plan={runtime.plan}
        jobs={runtime.jobs}
        onStop={runtime.stop}
      />}
    </div>
    <div className="composer">
      <div ref={workspacePickerRef} className="workspace-picker-wrap" onKeyDown={(event) => {
        if (event.key === "Escape" && workspaceEditing) {
          event.preventDefault();
          setWorkspaceEditing(false);
          workspacePickerRef.current?.querySelector("button")?.focus();
        }
      }}>
        <button type="button" className="workspace-picker" onClick={() => setWorkspaceEditing((value) => !value)} disabled={runtime.controlsDisabled || runtime.sessionRunning} aria-expanded={workspaceEditing} aria-haspopup="menu" aria-controls={workspaceEditing ? workspaceMenuId : undefined}>
          <span className="workspace-picker-icon">⌂</span><span className="workspace-picker-value">{runtime.displayedWorkspace || "选择项目"}</span>
          {runtime.workspaceSaving ? <LoaderCircle size={14} className="spin" aria-label="正在切换工作区" /> : <ChevronDown size={14} />}
        </button>
        {workspaceEditing && <div className="workspace-menu" id={workspaceMenuId} role="menu" aria-label="选择工作区" style={workspaceMenuBounds}>
          <div className="workspace-menu-list">
            {workspaceGroups.filter((group) => group.options.length > 0).map((group) => <div className="workspace-menu-group" role="group" aria-label={group.label} key={group.label}>
              <div className="workspace-menu-heading">{group.label}</div>
              {group.options.map(({ workspace }) => <button type="button" role="menuitem" className={`workspace-option ${workspace === runtime.displayedWorkspace ? "selected" : ""}`} key={workspace} onClick={() => { void runtime.saveWorkspace(workspace); setWorkspaceEditing(false); }} disabled={runtime.controlsDisabled || runtime.sessionRunning || runtime.workspaceSaving} title={workspace}>
                <Folder size={16} /><span className="workspace-option-path">{workspace.split(/[\\/]/).pop() || workspace}</span>
                {workspace === runtime.displayedWorkspace && <Check size={14} />}
              </button>)}
            </div>)}
          </div>
          <div className="workspace-menu-divider" />
          <button type="button" role="menuitem" className="workspace-new-option" onClick={() => { setWorkspaceEditing(false); void runtime.chooseNewWorkspace(); }} disabled={runtime.controlsDisabled || runtime.sessionRunning || runtime.workspaceSaving}><Plus size={16} />新建工作区</button>
          <button type="button" role="menuitem" className="workspace-new-option" onClick={() => { setWorkspaceEditing(false); void runtime.chooseScratchWorkspace(); }} disabled={runtime.controlsDisabled || runtime.sessionRunning || runtime.workspaceSaving}><Plus size={16} />临时工作区</button>
        </div>}
      </div>
      <label className="json-file-row">
        <FileJson size={16} aria-hidden="true" />
        <select className="json-picker" aria-label="选择 input 场景" value={filename} onFocus={() => setListRevision((revision) => revision + 1)} onChange={(event) => {
          setFilename(event.currentTarget.value);
          setPreview(null);
          setPreviewError("");
        }}>
          <option value="">{listError ? "场景列表读取失败" : filenames === null ? "正在读取 input/…" : filenames.length ? "选择 input/ 场景" : "input/ 暂无场景"}</option>
          {filenames?.map((name) => <option key={name} value={name}>{name}</option>)}
        </select>
      </label>
      {(listError || (filename && previewError)) && <p className="json-file-error" role="alert">{listError || previewError}</p>}
      {filename && preview && <details className="json-preview">
        <summary>预览文件{preview.truncated ? "（部分）" : ""}</summary>
        <pre tabIndex={0}>{preview.text}</pre>
      </details>}
      <div className="composer-bottom">
        <label className="model-selector" title={runtime.providers.find((option) => option.id === runtime.displayedProvider)?.model}>
          <select aria-label="选择模型" value={runtime.displayedProvider} disabled={!runtime.workspaceSelected || runtime.controlsDisabled || runtime.sessionRunning || runtime.providerSaving} onChange={(event) => runtime.changeProvider(event.target.value)}>
            {runtime.providers.map((option) => <option key={option.id} value={option.id}>{option.model}</option>)}
          </select>{runtime.providerSaving ? <LoaderCircle size={12} className="spin" aria-label="正在切换模型" /> : <ChevronDown size={12} />}
        </label>
        <div className="composer-actions">
          <ContextWindowIndicator window={runtime.contextWindow} cacheUsage={runtime.cacheUsage} />
          <button type="button" className="runtime-refresh" aria-label="重新加载运行环境" title="重新加载运行环境" disabled={refreshDisabled} onClick={runtime.refreshRuntime}><RefreshCw size={15} /></button>
          <button type="button" className="json-run" disabled={!filename || !runtime.canSubmitScene} onClick={async () => {
            if (filename && await runtime.submitScene(filename)) {
              setFilename((current) => current === filename ? "" : current);
            }
          }}>运行</button>
        </div>
      </div>
    </div>
    <div className="composer-footer">RADE · 你的项目搭档</div>
  </div>;
}
