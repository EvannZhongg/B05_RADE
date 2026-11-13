from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeInstruction:
    label: str
    content: str | None


@dataclass(frozen=True)
class RuntimeInstructions:
    documents: tuple[RuntimeInstruction, ...]
    fingerprint: str

    def __bool__(self) -> bool:
        return any(document.content for document in self.documents)


__all__ = ["RuntimeInstruction", "RuntimeInstructions"]
