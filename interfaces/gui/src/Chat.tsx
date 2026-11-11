import { useRef } from "react";
import { AssistantRuntimeProvider, ThreadPrimitive } from "@assistant-ui/react";
import type { ContextWindow } from "@rade/protocol";
import type { ModelOption, Session, WorkspaceOption } from "./api";
import { Composer } from "./chat/Composer";
import { SessionIdContext } from "./chat/Messages";
import { Transcript } from "./chat/Transcript";
import { useSessionRuntime } from "./chat/useSessionRuntime";

export function Chat({
  session,
  selected,
  searchTarget,
  contextWindow,
  workspaceOptions = [],
  backgroundActive = false,
  providers,
  provider,
  onProviderChange,
  onBusyChange,
  onContextWindowChange,
  onSessionAvailable,
  onTurnEnd,
  onWorkspaceChange,
}: {
  session: Session;
  selected: boolean;
  searchTarget?: { itemIndex: number; requestId: string };
  contextWindow: ContextWindow | null;
  workspaceOptions?: WorkspaceOption[];
  backgroundActive?: boolean;
  providers: ModelOption[];
  provider?: string;
  onProviderChange: (provider: string) => void;
  onBusyChange: (busy: boolean) => void;
  onContextWindowChange: (window: ContextWindow | null) => void;
  onSessionAvailable: () => void;
  onTurnEnd: () => void;
  onWorkspaceChange?: (workspace: string) => void;
}) {
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const runtime = useSessionRuntime({
    session,
    selected,
    contextWindow,
    workspaceOptions,
    backgroundActive,
    providers,
    provider,
    onProviderChange,
    onBusyChange,
    onContextWindowChange,
    onSessionAvailable,
    onTurnEnd,
    onWorkspaceChange,
  });
  const targetMessage = searchTarget && runtime.messages.find((message) => {
    const indexes = message.metadata?.custom?.itemIndexes;
    return Array.isArray(indexes) && indexes.includes(searchTarget.itemIndex);
  });

  return <SessionIdContext.Provider value={session.session_id}>
    <AssistantRuntimeProvider runtime={runtime.assistantRuntime}>
      <ThreadPrimitive.Root className="thread">
        <Transcript previews={runtime.turnPreviews} viewportRef={viewportRef} active={selected} navigation={searchTarget && { requestId: searchTarget.requestId, messageId: targetMessage?.id }} />
        <Composer runtime={runtime} />
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  </SessionIdContext.Provider>;
}
