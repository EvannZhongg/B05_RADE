import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { AlertTriangle, ChevronDown, FileJson, LoaderCircle, RefreshCw, X } from "lucide-react";
import type { SceneMode } from "@rade/protocol";
import { get } from "../api";
import { ContextWindowIndicator, RuntimeIndicators } from "./RuntimeIndicators";
import type { SessionRuntime } from "./useSessionRuntime";

type ScenePreview = { text: string; truncated: boolean };

export function Composer({ runtime }: { runtime: SessionRuntime }) {
  const refreshDisabled = runtime.controlsDisabled || runtime.sessionRunning
    || runtime.attaching || runtime.providerSaving || runtime.refreshing;
  const feedbackRef = useRef<HTMLDivElement | null>(null);
  const [filenames, setFilenames] = useState<string[] | null>(null);
  const [listRevision, setListRevision] = useState(0);
  const [listError, setListError] = useState("");
  const [filename, setFilename] = useState("");
  const [mode, setMode] = useState<SceneMode>("rethink");
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
      {runtime.sceneTask?.status === "completed" && <p role="status">{runtime.sceneTask.output_path} · 期望值 {runtime.sceneTask.expected_value.toPrecision(8)}</p>}
      {runtime.sceneTask?.status === "interrupted" && <p role="status">{runtime.sceneTask.error.message}</p>}
      <div className="json-file-row">
        <FileJson size={16} aria-hidden="true" />
        <select className="json-picker" aria-label="选择 input 场景" value={filename} onFocus={() => setListRevision((revision) => revision + 1)} onChange={(event) => {
          setFilename(event.currentTarget.value);
          setPreview(null);
          setPreviewError("");
        }}>
          <option value="">{listError ? "场景列表读取失败" : filenames === null ? "正在读取 input/…" : filenames.length ? "选择 input/ 场景" : "input/ 暂无场景"}</option>
          {filenames?.map((name) => <option key={name} value={name}>{name}</option>)}
        </select>
        <select className="json-picker" aria-label="反思模式" value={mode} onChange={(event) =>
          setMode(event.currentTarget.value === "norethink" ? "norethink" : "rethink")
        }>
          <option value="rethink">开启反思</option>
          <option value="norethink">关闭反思</option>
        </select>
      </div>
      {(listError || (filename && previewError)) && <p className="json-file-error" role="alert">{listError || previewError}</p>}
      {filename && preview && <details className="json-preview">
        <summary>预览文件{preview.truncated ? "（部分）" : ""}</summary>
        <pre tabIndex={0}>{preview.text}</pre>
      </details>}
      <div className="composer-bottom">
        <label className="model-selector" title={runtime.providers.find((option) => option.id === runtime.displayedProvider)?.model}>
          <select aria-label="选择模型" value={runtime.displayedProvider} disabled={runtime.controlsDisabled || runtime.sessionRunning || runtime.providerSaving} onChange={(event) => runtime.changeProvider(event.target.value)}>
            {runtime.providers.map((option) => <option key={option.id} value={option.id}>{option.model}</option>)}
          </select>{runtime.providerSaving ? <LoaderCircle size={12} className="spin" aria-label="正在切换模型" /> : <ChevronDown size={12} />}
        </label>
        <div className="composer-actions">
          <ContextWindowIndicator window={runtime.contextWindow} cacheUsage={runtime.cacheUsage} />
          <button type="button" className="runtime-refresh" aria-label="重新加载运行环境" title="重新加载运行环境" disabled={refreshDisabled} onClick={runtime.refreshRuntime}><RefreshCw size={15} /></button>
          <button type="button" className="json-run" disabled={!filename || !runtime.canSubmitScene} onClick={async () => {
            if (filename && await runtime.submitScene(filename, mode)) {
              setFilename((current) => current === filename ? "" : current);
            }
          }}>运行</button>
        </div>
      </div>
    </div>
    <div className="composer-footer">RADE · 你的项目搭档</div>
  </div>;
}
