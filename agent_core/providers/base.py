from abc import abstractmethod
from contextvars import copy_context
from queue import Empty, Full, Queue
from threading import Event, Thread
from types import GeneratorType
from typing import Callable, Iterable, Iterator

from agent_core.llm import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
)
from agent_core.session import Message
from agent_core.tools import ToolDefinition

from .reasoning import normalize_semantic_effort, validate_reasoning_parameters


class ModelProvider(LLMProvider):
    _CANCEL_POLL_SECONDS = 0.1

    @property
    def token_counting_identity(self) -> object:
        return [super().token_counting_identity, self._base_url,
                self._reasoning_effort, self._reasoning_parameters, "input-estimate-v2"]

    @property
    def model_name(self) -> str:
        return self._model

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        max_context_tokens: int | None = None,
        request_timeout_seconds: int = 300,
        max_retries: int = 2,
        reasoning_effort: str | None = None,
        reasoning_parameters: dict[str, object] | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, int)
            or request_timeout_seconds < 1
        ):
            raise ValueError("request_timeout_seconds must be a positive integer")
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries < 0
        ):
            raise ValueError("max_retries must be a non-negative integer")
        self._request_timeout_seconds = request_timeout_seconds
        self._max_retries = max_retries
        self._reasoning_effort = (
            normalize_semantic_effort(reasoning_effort)
            if reasoning_effort is not None
            else None
        )
        self._reasoning_parameters = validate_reasoning_parameters(reasoning_parameters)
        self._model_info: dict[str, object] | None = None
        self._model_info_loaded = False
        if max_context_tokens is not None:
            if (
                isinstance(max_context_tokens, bool)
                or not isinstance(max_context_tokens, int)
                or max_context_tokens < 1
            ):
                raise ValueError(
                    "max_context_tokens must be a positive integer"
                )
            self._max_context_tokens = max_context_tokens
        else:
            try:
                model_info = self._get_model_info()
            except Exception as error:
                raise ValueError(
                    f"No context limit metadata for model "
                    f"'{model}'; configure 'max_context_tokens' for this "
                    "provider"
                ) from error
            self._max_context_tokens = _get_model_max_context_tokens(
                model_info,
                model,
            )

    @property
    def max_context_tokens(self) -> int:
        return self._max_context_tokens

    @property
    def max_output_tokens(self) -> int | None:
        try:
            model_info = self._get_model_info()
        except Exception:
            return None
        value = model_info.get("max_output_tokens")
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
        ):
            return None
        return value

    def _get_model_info(self) -> dict[str, object]:
        if not self._model_info_loaded:
            self._model_info = self.model_info_for(self._model, self._base_url)
            self._model_info_loaded = True
        assert self._model_info is not None
        return self._model_info

    @classmethod
    @abstractmethod
    def model_info_for(cls, model: str, base_url: str | None = None) -> dict[str, object]:
        raise NotImplementedError

    def stream(
        self,
        request: LLMRequest,
        on_text_delta: Callable[[str], None],
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        return self._stream(
            request,
            on_text_delta,
            on_reasoning_delta,
            check_cancelled=lambda: None,
        )

    def stream_cancellable(
        self,
        request: LLMRequest,
        on_text_delta: Callable[[str], None],
        on_reasoning_delta: Callable[[str], None] | None,
        check_cancelled: Callable[[], None],
    ) -> LLMResponse:
        return self._stream(
            request,
            on_text_delta,
            on_reasoning_delta,
            check_cancelled=check_cancelled,
        )

    @abstractmethod
    def _stream(
        self,
        request: LLMRequest,
        on_text_delta: Callable[[str], None],
        on_reasoning_delta: Callable[[str], None] | None,
        *,
        check_cancelled: Callable[[], None],
    ) -> LLMResponse:
        raise NotImplementedError


_STREAM_END = object()


def _cancellable_chunks(
    create_stream: Callable[[], Iterable[object]],
    check_cancelled: Callable[[], None],
    poll_seconds: float,
) -> Iterator[object]:
    """Yield a synchronous provider stream without blocking cancellation checks."""
    queue: Queue[object | BaseException] = Queue(maxsize=1)
    stopped = Event()
    stream: Iterable[object] | None = None

    def publish(item: object | BaseException) -> bool:
        while not stopped.is_set():
            try:
                queue.put(item, timeout=poll_seconds)
                return True
            except Full:
                continue
        return False

    def read_stream() -> None:
        nonlocal stream
        try:
            stream = create_stream()
            if stopped.is_set():
                return
            for chunk in stream:
                if not publish(chunk):
                    return
        except BaseException as error:
            if not publish(error):
                return
        finally:
            try:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()
            except Exception as error:
                publish(error)
            finally:
                publish(_STREAM_END)

    check_cancelled()
    reader = Thread(
        target=copy_context().run,
        args=(read_stream,),
        name="provider-stream-reader",
        daemon=True,
    )
    reader.start()
    try:
        while True:
            check_cancelled()
            try:
                item = queue.get(timeout=poll_seconds)
            except Empty:
                continue
            check_cancelled()
            if item is _STREAM_END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stopped.set()
        close = getattr(stream, "close", None)
        # A generator can only be closed by its reader while it is executing.
        if close is not None and not isinstance(stream, GeneratorType):
            close()


def _request_messages(request: LLMRequest) -> list[dict[str, object]]:
    return [
        {"role": "system", "content": request.system_prompt},
        *(_message_to_dict(message) for message in request.messages),
    ]


def _request_tools(request: LLMRequest) -> list[dict[str, object]]:
    return [_tool_definition_to_dict(tool) for tool in request.tools]


def _get_model_max_context_tokens(
    model_info: dict[str, object],
    model: str,
) -> int:
    max_context_tokens = model_info.get("max_input_tokens")
    if (
        isinstance(max_context_tokens, bool)
        or not isinstance(max_context_tokens, int)
        or max_context_tokens < 1
    ):
        raise ValueError(
            f"No context limit metadata for model '{model}'; "
            "configure 'max_context_tokens' for this provider"
        )
    return max_context_tokens


def _message_to_dict(message: Message) -> dict[str, object]:
    data: dict[str, object] = {
        "role": message.role,
        "content": message.content,
    }
    if message.reasoning is not None:
        data["reasoning_content"] = message.reasoning
    if message.tool_calls:
        data["tool_calls"] = [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": tool_call.arguments_json,
                },
            }
            for tool_call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        data["tool_call_id"] = message.tool_call_id
    return data


def _tool_definition_to_dict(tool: ToolDefinition) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }
