from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import signal
import struct
import termios
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from .ansi import ScreenState


@dataclass
class TextEvent:
    delta: str


@dataclass
class IdleEvent:
    pass


@dataclass
class ExitEvent:
    code: int


@dataclass
class ErrorEvent:
    message: str


Event = TextEvent | IdleEvent | ExitEvent | ErrorEvent


class ClaudeSession:
    """Holds open a single interactive `claude` process on a PTY.

    Pushes Events onto self.events. Consumes prompts via send_prompt().
    """

    def __init__(
        self,
        binary: str,
        args: list[str],
        cwd: Path,
        cols: int,
        rows: int,
        idle_debounce_ms: int,
    ) -> None:
        self._binary = binary
        self._args = args
        self._cwd = cwd
        self._cols = cols
        self._rows = rows
        self._idle_debounce = idle_debounce_ms / 1000.0

        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self._screen = ScreenState(cols=cols, rows=rows)
        self._master_fd: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_attached = False
        self._last_activity = 0.0
        self._was_idle = False
        self._emitted_since_idle = False
        self._first_idle_pending = True
        self._trust_confirmed = False
        self._trust_anchored = True  # true if there was no trust modal to begin with
        self._saw_trust_prompt = False
        self._idle_task: asyncio.Task | None = None
        self._wait_task: asyncio.Task | None = None
        self._closed = False
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        master_fd, slave_fd = pty.openpty()
        self._set_winsize(master_fd, self._rows, self._cols)
        # Non-blocking reads on master.
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env["COLUMNS"] = str(self._cols)
        env["LINES"] = str(self._rows)
        env.pop("CLAUDE_CODE_SIMPLE", None)

        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._binary,
                *self._args,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(self._cwd),
                env=env,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)
        self._master_fd = master_fd

        loop = asyncio.get_running_loop()
        loop.add_reader(master_fd, self._on_readable)
        self._reader_attached = True
        self._last_activity = loop.time()
        self._idle_task = asyncio.create_task(self._idle_loop())
        self._wait_task = asyncio.create_task(self._wait_for_exit())

    def _set_winsize(self, fd: int, rows: int, cols: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _on_readable(self) -> None:
        if self._master_fd is None:
            return
        try:
            data = os.read(self._master_fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            # Master closed (child exited).
            self._detach_reader()
            return
        if not data:
            self._detach_reader()
            return
        self._screen.feed(data)
        if self._screen.has_trust_prompt():
            self._saw_trust_prompt = True
            self._trust_anchored = False
            self._screen.take_delta()
        elif self._saw_trust_prompt and not self._trust_anchored:
            # Trust modal cleared but the idle loop hasn't anchored yet;
            # discard intermediate banner draws.
            self._screen.take_delta()
        else:
            delta = self._screen.take_delta()
            if delta:
                self.events.put_nowait(TextEvent(delta=delta))
                self._emitted_since_idle = True
        loop = asyncio.get_running_loop()
        self._last_activity = loop.time()
        # Output means we are no longer idle from the last turn.
        self._was_idle = False

    def _detach_reader(self) -> None:
        if self._reader_attached and self._master_fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(self._master_fd)
            except (ValueError, RuntimeError):
                pass
            self._reader_attached = False

    async def _idle_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._idle_debounce / 2)
                loop = asyncio.get_running_loop()
                quiet = loop.time() - self._last_activity >= self._idle_debounce
                if not quiet:
                    continue
                if self._screen.has_spinner():
                    continue
                if (
                    not self._trust_confirmed
                    and self._screen.has_trust_prompt()
                ):
                    self._trust_confirmed = True
                    await self._write(b"\r")
                    continue
                if (
                    self._saw_trust_prompt
                    and not self._trust_anchored
                    and not self._screen.has_trust_prompt()
                ):
                    # Banner has rendered after trust-confirm. Anchor and
                    # re-enable text emission.
                    self._screen.anchor_to_current()
                    self._trust_anchored = True
                    continue
                if self._was_idle:
                    continue
                if not self._screen.has_input_prompt():
                    continue
                # Only fire idle if there's actually been new text since the
                # last idle. The very first idle (mapped by the server to
                # `ready`) fires unconditionally so clients can start sending.
                if not self._first_idle_pending and not self._emitted_since_idle:
                    continue
                self._was_idle = True
                self._emitted_since_idle = False
                self._first_idle_pending = False
                self.events.put_nowait(IdleEvent())
        except asyncio.CancelledError:
            pass

    async def _wait_for_exit(self) -> None:
        assert self._proc is not None
        code = await self._proc.wait()
        self._detach_reader()
        if self._idle_task:
            self._idle_task.cancel()
        self.events.put_nowait(ExitEvent(code=code))

    async def send_prompt(self, text: str) -> None:
        """Type the prompt into the TUI and submit with CR."""
        if self._master_fd is None:
            raise RuntimeError("session not started")
        # Strip bare CRs/LFs from user text to avoid premature submit; the TUI
        # accepts shift+enter for newline but a plain CR submits. For now we
        # forbid embedded newlines so multi-line prompts can be added later.
        cleaned = text.replace("\r", "").replace("\n", " ")
        payload = cleaned.encode("utf-8") + b"\r"
        await self._write(payload)

    async def send_interrupt(self) -> None:
        await self._write(b"\x1b")  # ESC

    async def _write(self, data: bytes) -> None:
        if self._master_fd is None:
            raise RuntimeError("session not started")
        fd = self._master_fd
        async with self._write_lock:
            view = memoryview(data)
            while view:
                try:
                    n = os.write(fd, view)
                except BlockingIOError:
                    await asyncio.sleep(0.005)
                    continue
                view = view[n:]
        # Writing user input counts as activity (echo will arrive).
        self._last_activity = asyncio.get_running_loop().time()
        self._was_idle = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._detach_reader()
        if self._idle_task:
            self._idle_task.cancel()
        proc = self._proc
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                await proc.wait()
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    async def iter_events(self) -> AsyncIterator[Event]:
        while True:
            ev = await self.events.get()
            yield ev
            if isinstance(ev, ExitEvent):
                return
