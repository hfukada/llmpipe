from __future__ import annotations

from typing import Any, Literal, TypedDict


class PromptMsg(TypedDict):
    type: Literal["prompt"]
    text: str


class InterruptMsg(TypedDict):
    type: Literal["interrupt"]


class PingMsg(TypedDict):
    type: Literal["ping"]


ClientMessage = PromptMsg | InterruptMsg | PingMsg


def parse_client_message(raw: Any) -> ClientMessage:
    if not isinstance(raw, dict):
        raise ValueError("message must be a JSON object")
    t = raw.get("type")
    if t == "prompt":
        text = raw.get("text")
        if not isinstance(text, str):
            raise ValueError("prompt.text must be a string")
        return {"type": "prompt", "text": text}
    if t == "interrupt":
        return {"type": "interrupt"}
    if t == "ping":
        return {"type": "ping"}
    raise ValueError(f"unknown message type: {t!r}")


def text_event(delta: str) -> dict:
    return {"type": "text", "delta": delta}


def idle_event() -> dict:
    return {"type": "idle"}


def ready_event() -> dict:
    return {"type": "ready"}


def error_event(message: str) -> dict:
    return {"type": "error", "message": message}


def exit_event(code: int) -> dict:
    return {"type": "exit", "code": code}


def pong_event() -> dict:
    return {"type": "pong"}
