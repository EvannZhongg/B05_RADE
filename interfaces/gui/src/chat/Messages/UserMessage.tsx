import { MessagePrimitive, useAuiState } from "@assistant-ui/react";
import { CopyTextButton } from "../../CopyTextButton";
import { MessageTimestamp } from "./messagePrimitives";

export function UserMessage() {
  const itemIndex = Number(useAuiState((state) => state.message.id));
  const text = useAuiState((state) => state.message.parts
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("\n"));
  return <MessagePrimitive.Root className="user-turn" data-message-id={String(itemIndex)}>
    <MessageTimestamp className="turn-timestamp" />
    <div className="user-message"><MessagePrimitive.Parts /></div>
    <div className="user-message-meta">
      <CopyTextButton className="user-message-action" label="复制消息" getText={() => text} />
    </div>
  </MessagePrimitive.Root>;
}
