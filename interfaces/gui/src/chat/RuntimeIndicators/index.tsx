import { useCallback, useEffect, useState } from "react";
import { LoaderCircle, Square } from "lucide-react";
import type { PlanSnapshot, RuntimePhase } from "@rade/protocol";
import { BackgroundJobs } from "./BackgroundJobs";
import { PlanStatus } from "./PlanStatus";
import { updateRuntimeIndicatorOrder, type BackgroundJob, type RuntimeIndicator } from "../runtimeState";

export { ContextWindowIndicator } from "./ContextWindow";

export function RuntimeIndicators({
  running,
  refreshing = false,
  reconnecting,
  runtimePhase,
  plan,
  jobs,
  onStop,
}: {
  running: boolean;
  refreshing?: boolean;
  reconnecting: boolean;
  runtimePhase: RuntimePhase;
  plan: PlanSnapshot | null;
  jobs: BackgroundJob[];
  onStop: () => void;
}) {
  const [popover, setPopover] = useState<RuntimeIndicator | null>(null);
  const [order, setOrder] = useState<RuntimeIndicator[]>(() => plan ? ["plan"] : []);
  const hasJobs = jobs.length > 0;
  const setPlanOpen = useCallback((open: boolean) => setPopover(open ? "plan" : null), []);
  const setJobsOpen = useCallback((open: boolean) => setPopover(open ? "jobs" : null), []);

  useEffect(() => {
    setOrder((current) => updateRuntimeIndicatorOrder(current, plan !== null, hasJobs));
  }, [hasJobs, plan]);

  useEffect(() => {
    if (
      reconnecting
      || (popover === "plan" && plan === null)
      || (popover === "jobs" && !hasJobs)
    ) setPopover(null);
  }, [hasJobs, plan, popover, reconnecting]);

  if (!running && !plan) return null;
  return <div className="activity">
    {running && <LoaderCircle size={13} className="spin" />}
    <span className="activity-label" role="status">{running
      ? refreshing
        ? "正在重新加载运行环境…"
        : reconnecting
          ? "连接中断，任务仍在后台运行，正在重新连接…"
          : runtimePhase === "starting"
            ? "正在启动 Agent…"
            : "RADE 正在处理…"
      : "Plan 尚未完成"}</span>
    <div className="activity-controls">
      {order.map((item) => item === "plan"
        ? plan && <PlanStatus key="plan" plan={plan} open={popover === "plan"} onOpenChange={setPlanOpen} />
        : hasJobs && <BackgroundJobs key="jobs" jobs={jobs} open={popover === "jobs" && !reconnecting} disabled={reconnecting} onOpenChange={setJobsOpen} />)}
      {running && !refreshing && <button className="stop-button" aria-label="停止执行" onClick={onStop}><Square size={11} /> 停止</button>}
    </div>
  </div>;
}
