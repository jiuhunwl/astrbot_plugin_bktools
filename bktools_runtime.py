from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional

import aiohttp


class TaskState(str, Enum):
    WAITING = "waiting"
    PARSING = "parsing"
    BUILDING = "building"
    SENDING = "sending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class TaskRecord:
    scope: str
    state: TaskState = TaskState.WAITING
    started_at: float = 0.0
    updated_at: float = 0.0
    last_error: str = ""


@dataclass
class CircuitState:
    failures: int = 0
    opened_until: float = 0.0
    last_error: str = ""


class CircuitOpenError(RuntimeError):
    pass


# 上游 5xx 响应体中常见的业务错误字段，用于透传真实错误信息
_SERVER_ERROR_MSG_FIELDS = (
    "msg", "message", "error", "error_msg", "errmsg", "detail", "info", "reason",
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _trim_server_error(text: str, limit: int = 300) -> str:
    """提取并精简上游错误正文，供透传给用户/日志。"""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    else:
        # 优先取业务字段，避免把整段响应原文透传
        if isinstance(payload, dict):
            for key in _SERVER_ERROR_MSG_FIELDS:
                value = payload.get(key) or payload.get(key.lower())
                if isinstance(value, list):
                    value = value[0] if value else ""
                if isinstance(value, str) and value.strip():
                    return _trim_server_error(value, limit)
                if isinstance(value, dict):
                    nested = _trim_server_error(json.dumps(value, ensure_ascii=False), limit)
                    if nested:
                        return nested
                elif isinstance(value, (int, float, bool)):
                    # 数字/布尔可以转字符串透传，但 0 / False 视为无意义跳过
                    text_form = str(value)
                    if text_form.strip() and text_form not in ("0", "False"):
                        return _trim_server_error(text_form, limit)
                # 空字符串 / None / 空 list / 空 dict 都跳过，继续尝试下一个字段
            # 无业务字段时保留原始 JSON，但压缩空白
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    text = _HTML_TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


class RuntimeManager:
    """Tracks task state, idempotent sends, temporary files and endpoint health."""

    def __init__(self) -> None:
        self.tasks: Dict[asyncio.Task, TaskRecord] = {}
        self.sent_keys: Dict[str, float] = {}
        self.temp_files: Dict[str, set[str]] = {}
        self.circuits: Dict[str, CircuitState] = {}

    def register_task(self, scope: str) -> Optional[asyncio.Task]:
        task = asyncio.current_task()
        if task is not None:
            now = time.time()
            self.tasks[task] = TaskRecord(scope, TaskState.WAITING, now, now)
        return task

    def set_state(self, state: TaskState, error: str = "") -> None:
        task = asyncio.current_task()
        record = self.tasks.get(task) if task else None
        if record:
            record.state = state
            record.updated_at = time.time()
            if error:
                record.last_error = error[:300]

    def finish_task(self, task: Optional[asyncio.Task], state: TaskState) -> None:
        record = self.tasks.get(task) if task else None
        if record:
            record.state = state
            record.updated_at = time.time()

    def remove_task(self, task: Optional[asyncio.Task]) -> None:
        if task:
            self.tasks.pop(task, None)

    def state_counts(self, scope: Optional[str] = None) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for record in self.tasks.values():
            if scope is not None and record.scope != scope:
                continue
            counts[record.state.value] = counts.get(record.state.value, 0) + 1
        return counts

    def claim_send(self, key: str, ttl_sec: int = 600) -> bool:
        now = time.time()
        self.sent_keys = {
            item: ts for item, ts in self.sent_keys.items() if now - ts <= ttl_sec
        }
        if key in self.sent_keys:
            return False
        self.sent_keys[key] = now
        return True

    def release_send(self, key: str) -> None:
        self.sent_keys.pop(key, None)

    def register_temp(self, scope: str, path: str) -> None:
        if path:
            self.temp_files.setdefault(scope, set()).add(path)

    def cleanup_scope(self, scope: str) -> tuple[int, int]:
        count = size = 0
        for path in list(self.temp_files.pop(scope, set())):
            try:
                if os.path.isfile(path):
                    size += os.path.getsize(path)
                    os.remove(path)
                    count += 1
            except OSError:
                continue
        return count, size

    def cleanup_all(self) -> tuple[int, int]:
        count = size = 0
        for scope in list(self.temp_files):
            c, s = self.cleanup_scope(scope)
            count += c
            size += s
        return count, size

    def circuit(self, endpoint: str) -> CircuitState:
        return self.circuits.setdefault(endpoint, CircuitState())

    def assert_circuit_closed(self, endpoint: str) -> None:
        state = self.circuit(endpoint)
        if state.opened_until > time.time():
            remaining = max(1, int(state.opened_until - time.time()))
            raise CircuitOpenError(f"接口暂时熔断，请 {remaining} 秒后重试")
        if state.opened_until:
            state.opened_until = 0.0
            state.failures = 0

    def record_success(self, endpoint: str) -> None:
        self.circuits[endpoint] = CircuitState()

    def record_failure(
        self, endpoint: str, error: BaseException, threshold: int, recovery_sec: int
    ) -> None:
        state = self.circuit(endpoint)
        state.failures += 1
        state.last_error = str(error)[:300]
        if state.failures >= max(1, threshold):
            state.opened_until = time.time() + max(1, recovery_sec)

    def prune(self, ttl_sec: int, max_entries: int) -> None:
        now = time.time()
        self.sent_keys = {
            key: ts for key, ts in self.sent_keys.items() if now - ts <= ttl_sec
        }
        if len(self.sent_keys) > max_entries:
            keep = sorted(self.sent_keys.items(), key=lambda item: item[1], reverse=True)
            self.sent_keys = dict(keep[:max_entries])


class SafeHttpClient:
    """Reusable aiohttp session with bounded responses, retries and circuit breaking."""

    def __init__(
        self,
        runtime: RuntimeManager,
        trace_factory: Callable[[], aiohttp.TraceConfig],
    ) -> None:
        self.runtime = runtime
        self.trace_factory = trace_factory
        self.sessions: Dict[tuple[int, str], aiohttp.ClientSession] = {}

    @property
    def session(self) -> Optional[aiohttp.ClientSession]:
        return next((item for item in self.sessions.values() if not item.closed), None)

    async def get_session(self, timeout_sec: int, user_agent: str) -> aiohttp.ClientSession:
        signature = (timeout_sec, user_agent)
        session = self.sessions.get(signature)
        if session is None or session.closed:
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout_sec),
                headers={"User-Agent": user_agent},
                trace_configs=[self.trace_factory()],
            )
            self.sessions[signature] = session
        return session

    async def request_bytes(
        self,
        method: str,
        url: str,
        *,
        timeout_sec: int,
        user_agent: str,
        max_response_bytes: int,
        retries: int,
        backoff_sec: float,
        circuit_threshold: int,
        circuit_recovery_sec: int,
        **kwargs: Any,
    ) -> bytes:
        self.runtime.assert_circuit_closed(url)
        attempts = max(1, retries + 1)
        last_error: Optional[BaseException] = None
        last_retryable = False
        for attempt in range(attempts):
            try:
                session = await self.get_session(timeout_sec, user_agent)
                async with session.request(method, url, **kwargs) as response:
                    if response.status >= 500:
                        # 尽力读取上游错误正文并提取 msg，透传真实原因
                        upstream = ""
                        try:
                            body = await response.content.read(64 * 1024)
                            upstream = _trim_server_error(body.decode("utf-8", errors="replace"))
                        except Exception:
                            pass
                        detail = f" (upstream: {upstream})" if upstream else ""
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                            message=f"upstream server error{detail}",
                            headers=response.headers,
                        )
                    response.raise_for_status()
                    declared = int(response.headers.get("Content-Length", "0") or 0)
                    if declared > max_response_bytes:
                        raise ValueError("接口响应超过配置的大小限制")
                    chunks = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        chunks.extend(chunk)
                        if len(chunks) > max_response_bytes:
                            raise ValueError("接口响应超过配置的大小限制")
                    self.runtime.record_success(url)
                    return bytes(chunks)
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError, asyncio.TimeoutError, aiohttp.ClientResponseError) as ex:
                last_error = ex
                retryable = not isinstance(ex, aiohttp.ClientResponseError) or ex.status >= 500
                last_retryable = retryable
                if not retryable or attempt + 1 >= attempts:
                    break
                if backoff_sec > 0:
                    await asyncio.sleep(backoff_sec * (2**attempt))
            except Exception:
                raise
        assert last_error is not None
        if last_retryable:
            self.runtime.record_failure(
                url, last_error, circuit_threshold, circuit_recovery_sec
            )
        raise last_error

    async def close(self) -> None:
        sessions = list(self.sessions.values())
        self.sessions.clear()
        if sessions:
            await asyncio.gather(
                *(session.close() for session in sessions if not session.closed),
                return_exceptions=True,
            )


def create_json_temp(payload: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix="bktools_json_", suffix=".json")
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
    return path
