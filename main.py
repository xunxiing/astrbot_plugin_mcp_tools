from __future__ import annotations

import asyncio
import importlib
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any

import uvicorn

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


def _now_ts() -> float:
    return time.time()


def _load_build_app():
    if __package__:
        return importlib.import_module(".gateway", package=__package__).build_app

    plugin_root = Path(__file__).resolve().parent
    if str(plugin_root) not in sys.path:
        sys.path.insert(0, str(plugin_root))
    return importlib.import_module("gateway").build_app


@register(
    "astrbot_plugin_mcp_tools",
    "OpenCode",
    "Expose AstrBot runtime capabilities through a FastAPI REST gateway.",
    "0.2.0",
)
class APIPlugin(Star):
    def __init__(self, context: Context, config: Any):
        super().__init__(context)
        self.config = config
        self._gateway_services = None
        self._astr_loop: asyncio.AbstractEventLoop | None = None
        self._asgi_app = None
        self._uvicorn_server: uvicorn.Server | None = None
        self._server_thread: threading.Thread | None = None
        self._server_thread_loop: asyncio.AbstractEventLoop | None = None
        self._server_started = threading.Event()
        self._server_stopped = threading.Event()
        self._server_error: str | None = None
        self._recent_sessions: dict[str, dict[str, Any]] = {}
        self._recent_sessions_order: deque[str] = deque()
        self._last_session: str | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def terminate(self):
        await self._cancel_background_tasks()
        self._stop_server()

    async def initialize(self):
        self._astr_loop = asyncio.get_running_loop()
        await self._ensure_server_started()

    def create_background_task(self, coro, *, name: str | None = None) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _cancel_background_tasks(self) -> None:
        tasks = [task for task in self._background_tasks if not task.done()]
        if not tasks:
            self._background_tasks.clear()
            return

        for task in tasks:
            task.cancel()

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                logger.warning(
                    "[astrbot_rest_gateway] Background task finished with error: %s",
                    result,
                )
        self._background_tasks.clear()

    def _stop_server(self) -> None:
        self._server_started.clear()

        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True

        if self._server_thread_loop is not None:
            try:
                self._server_thread_loop.call_soon_threadsafe(lambda: None)
            except Exception:
                logger.debug(
                    "Failed to wake gateway event loop during shutdown.", exc_info=True
                )

        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=10)
            if self._server_thread.is_alive():
                if self._uvicorn_server is not None:
                    self._uvicorn_server.force_exit = True
                if self._server_thread_loop is not None:
                    try:
                        self._server_thread_loop.call_soon_threadsafe(lambda: None)
                    except Exception:
                        logger.debug(
                            "Failed to force wake gateway event loop during shutdown.",
                            exc_info=True,
                        )
                self._server_thread.join(timeout=2)
            if self._server_thread.is_alive():
                logger.warning(
                    "[astrbot_rest_gateway] Server thread did not stop within timeout."
                )

        self._server_thread = None
        self._server_thread_loop = None
        self._uvicorn_server = None
        self._asgi_app = None
        self._server_error = None
        self._server_stopped.set()

    async def call_on_astr_loop(self, coro):
        if self._astr_loop is None:
            return await coro

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is self._astr_loop:
            return await coro

        future = asyncio.run_coroutine_threadsafe(coro, self._astr_loop)
        return await asyncio.wrap_future(future)

    def _remember_session(self, event: AstrMessageEvent) -> None:
        key = event.unified_msg_origin
        self._last_session = key
        self._recent_sessions[key] = {
            "session": key,
            "platform_id": event.get_platform_id(),
            "platform_name": event.get_platform_name(),
            "message_type": event.get_message_type().value,
            "session_id": event.session_id,
            "sender_id": event.get_sender_id(),
            "sender_name": event.get_sender_name(),
            "last_seen_ts": _now_ts(),
            "plain_text": event.get_message_str(),
        }

        if key in self._recent_sessions_order:
            try:
                self._recent_sessions_order.remove(key)
            except ValueError:
                pass
        self._recent_sessions_order.appendleft(key)

        max_recent_sessions = int(getattr(self.config, "max_recent_sessions", 50) or 50)
        while len(self._recent_sessions_order) > max_recent_sessions:
            expired = self._recent_sessions_order.pop()
            self._recent_sessions.pop(expired, None)

    def _server_thread_entry(self, host: str, port: int) -> None:
        self._server_stopped.clear()
        self._server_started.clear()
        self._server_error = None
        try:
            asyncio.run(self._serve(host=host, port=port))
        except Exception:
            self._server_error = traceback.format_exc()
            logger.error("REST gateway thread crashed:\n" + self._server_error)
        finally:
            self._server_started.clear()
            self._server_stopped.set()

    async def _serve(self, host: str, port: int) -> None:
        self._server_thread_loop = asyncio.get_running_loop()
        self._asgi_app = _load_build_app()(self)
        config = uvicorn.Config(
            self._asgi_app,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
        )
        self._uvicorn_server = uvicorn.Server(config)
        serve_task = asyncio.create_task(self._uvicorn_server.serve())
        try:
            while not serve_task.done():
                if getattr(self._uvicorn_server, "started", False):
                    self._server_started.set()
                    break
                await asyncio.sleep(0.05)
            await serve_task
        finally:
            self._server_started.clear()

    async def _ensure_server_started(self) -> None:
        if not bool(getattr(self.config, "enable_server", True)):
            logger.info("[astrbot_rest_gateway] REST gateway disabled by config.")
            return

        host = str(getattr(self.config, "host", "127.0.0.1") or "127.0.0.1")
        port = int(getattr(self.config, "port", 6185) or 6185)
        auth_token = str(getattr(self.config, "auth_token", "") or "")
        docs_enabled = bool(getattr(self.config, "enable_docs", True))
        docs_path = str(getattr(self.config, "docs_path", "/docs") or "/docs")

        if self._server_thread and self._server_thread.is_alive():
            return

        self._server_thread = threading.Thread(
            target=self._server_thread_entry,
            args=(host, port),
            name="astrbot-rest-gateway",
            daemon=True,
        )
        self._server_thread.start()

        deadline = time.time() + 8
        while time.time() < deadline:
            if self._server_started.wait(timeout=0.1):
                break
            if self._server_error:
                break
            if self._server_thread and not self._server_thread.is_alive():
                break

        if self._server_error:
            logger.error(
                "[astrbot_rest_gateway] REST gateway failed to start:\n"
                + self._server_error
            )
            self._stop_server()
            return

        if not self._server_started.is_set():
            logger.warning(
                f"[astrbot_rest_gateway] REST gateway startup was not confirmed within timeout for http://{host}:{port}"
            )
        else:
            logger.info(
                f"[astrbot_rest_gateway] REST gateway started at http://{host}:{port}"
            )
        if docs_enabled:
            logger.info(
                f"[astrbot_rest_gateway] OpenAPI docs: http://{host}:{port}{docs_path}"
            )
        if not auth_token:
            logger.warning(
                "[astrbot_rest_gateway] auth_token is empty; authenticated endpoints will reject requests."
            )
        elif auth_token == "change-me":
            logger.warning(
                "[astrbot_rest_gateway] auth_token is still using the default placeholder."
            )

    @filter.on_astrbot_loaded(priority=100)
    async def _on_astrbot_loaded(self):
        self._astr_loop = asyncio.get_running_loop()
        await self._ensure_server_started()

    @filter.command("api", priority=100)
    async def _cmd_api(self, event: AstrMessageEvent):
        host = str(getattr(self.config, "host", "127.0.0.1") or "127.0.0.1")
        port = int(getattr(self.config, "port", 6185) or 6185)
        docs_enabled = bool(getattr(self.config, "enable_docs", True))
        docs_path = str(getattr(self.config, "docs_path", "/docs") or "/docs")
        last_session = self._last_session or "(none)"
        lines = [
            f"REST Gateway: http://{host}:{port}",
            f"Last session: {last_session}",
        ]
        if docs_enabled:
            lines.append(f"Docs: http://{host}:{port}{docs_path}")
        yield event.plain_result("\n".join(lines))

    @filter.regex(r"(?s).*", priority=-1000)
    async def _capture_session(self, event: AstrMessageEvent):
        if not bool(getattr(self.config, "capture_recent_sessions", True)):
            return
        try:
            self._remember_session(event)
        except Exception:
            logger.debug("Failed to capture recent session.", exc_info=True)

    @filter.after_message_sent(priority=-1000)
    async def _mirror_injected_platform_response(self, event: AstrMessageEvent):
        services = self._gateway_services
        if services is None:
            return
        try:
            await services.mirror_injected_platform_response(event)
        except Exception:
            logger.debug(
                "Failed to mirror injected platform response into WebUI.",
                exc_info=True,
            )
