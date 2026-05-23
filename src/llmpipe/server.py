from __future__ import annotations

import asyncio
import json
import logging
import shlex
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect

from . import protocol
from .config import Config
from .session import (
    ClaudeSession,
    ErrorEvent,
    ExitEvent,
    IdleEvent,
    TextEvent,
)

log = logging.getLogger("llmpipe")


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="llmpipe", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.websocket("/session")
    async def session_ws(
        ws: WebSocket,
        cwd: str | None = Query(default=None),
        args: str | None = Query(default=None),
    ) -> None:
        await ws.accept()
        try:
            resolved_cwd, resolved_args = _resolve_overrides(config, cwd, args)
        except ValueError as e:
            await ws.send_json(protocol.error_event(str(e)))
            await ws.close()
            return

        session = ClaudeSession(
            binary=config.claude_binary,
            args=list(resolved_args),
            cwd=resolved_cwd,
            cols=config.pty_cols,
            rows=config.pty_rows,
            idle_debounce_ms=config.idle_debounce_ms,
        )
        try:
            await session.start()
        except FileNotFoundError as e:
            await ws.send_json(protocol.error_event(f"claude binary not found: {e}"))
            await ws.close()
            return
        except Exception as e:  # noqa: BLE001
            await ws.send_json(protocol.error_event(f"failed to start session: {e}"))
            await ws.close()
            return

        await _run_session(ws, session)

    return app


def _resolve_overrides(
    config: Config, cwd_override: str | None, args_override: str | None
) -> tuple[Path, tuple[str, ...]]:
    if cwd_override is not None:
        if not config.allow_cwd_override:
            raise ValueError("cwd override not permitted by server config")
        cwd = Path(cwd_override).expanduser().resolve()
    else:
        cwd = config.resolved_default_cwd
    if not cwd.is_dir():
        raise ValueError(f"cwd does not exist or is not a directory: {cwd}")

    if args_override is not None:
        if not config.allow_args_override:
            raise ValueError("args override not permitted by server config")
        args = tuple(shlex.split(args_override))
    else:
        args = config.default_args
    return cwd, args


async def _run_session(ws: WebSocket, session: ClaudeSession) -> None:
    sent_ready = False

    async def pump_events() -> None:
        nonlocal sent_ready
        async for ev in session.iter_events():
            if isinstance(ev, TextEvent):
                await ws.send_json(protocol.text_event(ev.delta))
            elif isinstance(ev, IdleEvent):
                if not sent_ready:
                    await ws.send_json(protocol.ready_event())
                    sent_ready = True
                else:
                    await ws.send_json(protocol.idle_event())
            elif isinstance(ev, ErrorEvent):
                await ws.send_json(protocol.error_event(ev.message))
            elif isinstance(ev, ExitEvent):
                await ws.send_json(protocol.exit_event(ev.code))
                return

    async def pump_client() -> None:
        while True:
            raw = await ws.receive_text()
            try:
                msg = protocol.parse_client_message(json.loads(raw))
            except (ValueError, json.JSONDecodeError) as e:
                await ws.send_json(protocol.error_event(str(e)))
                continue
            if msg["type"] == "prompt":
                await session.send_prompt(msg["text"])
            elif msg["type"] == "interrupt":
                await session.send_interrupt()
            elif msg["type"] == "ping":
                await ws.send_json(protocol.pong_event())

    events_task = asyncio.create_task(pump_events())
    client_task = asyncio.create_task(pump_client())
    try:
        done, pending = await asyncio.wait(
            {events_task, client_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        # Surface exceptions from the completed task.
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                log.exception("session task crashed", exc_info=exc)
                try:
                    await ws.send_json(protocol.error_event(f"server error: {exc}"))
                except Exception:  # noqa: BLE001
                    pass
    finally:
        await session.close()
        try:
            await ws.close()
        except RuntimeError:
            pass
