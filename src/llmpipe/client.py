"""Interactive REPL client for llmpipe.

Connects to ws://host:port/session, prompts for input at `>`, sends each line
as a {"type":"prompt"} frame, and prints streamed text deltas inline plus
short status markers for ready/idle/error/exit.

Usage:
    llmpipe-client                       # connect to ws://127.0.0.1:8765/session
    llmpipe-client --url ws://host:port/session
    llmpipe-client --cwd /some/path      # forwarded as ?cwd= query param
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from urllib.parse import urlencode

import websockets


# ANSI for the status markers so they're easy to spot in the stream.
DIM = "\x1b[2m"
RESET = "\x1b[0m"


class Client:
    def __init__(self, url: str) -> None:
        self._url = url
        self._ws: websockets.ClientConnection | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        # Set when Claude is idle and we can accept the next prompt.
        self._can_prompt = asyncio.Event()
        # True while we are mid-turn; used to print a leading newline before
        # status markers so they don't run into streamed text.
        self._mid_turn = False

    async def run(self) -> int:
        try:
            async with websockets.connect(self._url, max_size=None) as ws:
                self._ws = ws
                self._marker(f"connected to {self._url}")
                reader = asyncio.create_task(self._read_loop())
                writer = asyncio.create_task(self._write_loop())
                done, pending = await asyncio.wait(
                    {reader, writer, asyncio.create_task(self._stop.wait())},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                for t in done:
                    exc = t.exception() if not t.cancelled() else None
                    if exc and not isinstance(exc, (EOFError, KeyboardInterrupt)):
                        self._marker(f"client error: {exc!r}")
                        return 1
            return 0
        except (OSError, websockets.exceptions.WebSocketException) as e:
            self._marker(f"connection failed: {e}")
            return 1

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    self._marker(f"bad frame: {raw!r}")
                    continue
                self._handle(msg)
        except websockets.exceptions.ConnectionClosed:
            self._marker("connection closed")
            self._stop.set()

    def _handle(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "text":
            delta = msg.get("delta", "")
            sys.stdout.write(delta)
            sys.stdout.flush()
            self._mid_turn = True
        elif t == "ready":
            self._marker("ready")
            self._can_prompt.set()
        elif t == "idle":
            self._marker("idle")
            self._can_prompt.set()
        elif t == "error":
            self._marker(f"error: {msg.get('message')}")
        elif t == "exit":
            self._marker(f"exit code={msg.get('code')}")
            self._stop.set()
        elif t == "pong":
            self._marker("pong")
        else:
            self._marker(f"unknown frame: {msg}")

    async def _write_loop(self) -> None:
        assert self._ws is not None
        while not self._stop.is_set():
            await self._can_prompt.wait()
            try:
                line = await asyncio.to_thread(self._read_line)
            except EOFError:
                self._stop.set()
                return
            if line is None:
                self._stop.set()
                return
            if not line.strip():
                continue
            self._can_prompt.clear()
            self._mid_turn = False
            await self._ws.send(json.dumps({"type": "prompt", "text": line}))

    def _read_line(self) -> str | None:
        try:
            return input("> ")
        except EOFError:
            return None

    def _marker(self, text: str) -> None:
        # Always start on a fresh line: handles both mid-stream deltas and the
        # case where input("> ") has written its prompt but is awaiting input.
        sys.stdout.write(f"\n{DIM}[{text}]{RESET}\n")
        sys.stdout.flush()
        self._mid_turn = False


def _build_url(base: str, cwd: str | None, args: str | None) -> str:
    params: dict[str, str] = {}
    if cwd is not None:
        params["cwd"] = cwd
    if args is not None:
        params["args"] = args
    if not params:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(params)}"


def main() -> None:
    parser = argparse.ArgumentParser(prog="llmpipe-client")
    parser.add_argument("--url", default="ws://127.0.0.1:8765/session")
    parser.add_argument("--cwd", default=None, help="forwarded as ?cwd= query")
    parser.add_argument("--args", default=None, help="forwarded as ?args= query")
    ns = parser.parse_args()
    url = _build_url(ns.url, ns.cwd, ns.args)
    client = Client(url)
    loop = asyncio.new_event_loop()
    # SIGINT cleanly stops the client.
    loop.add_signal_handler(signal.SIGINT, client._stop.set)
    try:
        code = loop.run_until_complete(client.run())
    finally:
        loop.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
