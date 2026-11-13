"""Application notifications and optional interactive capabilities."""

from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

from agent_core import AgentEvent, JobStatusEvent, PlanSnapshot
if TYPE_CHECKING:
    from agent_core.jobs import JobUpdate
    from agent_core.mcp.manager import McpServerStatus


@dataclass(frozen=True)
class RuntimeCallbacks:
    on_event: Callable[[AgentEvent, str], None] = lambda event, turn_id: None
    on_phase: Callable[[str, tuple[str, ...]], None] = lambda phase, warnings: None
    on_plan: Callable[[PlanSnapshot], None] = lambda snapshot: None
    on_mcp_status: Callable[["McpServerStatus"], None] = lambda status: None

    def on_job_update(self, update: "JobUpdate") -> None:
        self.on_event(
            JobStatusEvent(job_id=update.job_id, kind=update.kind, status=update.status, details=update.details),
            update.turn_id,
        )
