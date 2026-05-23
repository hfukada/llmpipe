from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8765
    claude_binary: str = "claude"
    default_cwd: str = "~"
    default_args: tuple[str, ...] = ("--permission-mode", "dontAsk")
    allow_cwd_override: bool = True
    allow_args_override: bool = False
    idle_debounce_ms: int = 250
    pty_cols: int = 200
    pty_rows: int = 50

    @property
    def resolved_default_cwd(self) -> Path:
        return Path(os.path.expanduser(self.default_cwd)).resolve()


def _candidate_paths(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]
    return [
        Path.cwd() / "config.toml",
        Path("~/.config/llmpipe/config.toml").expanduser(),
    ]


def load(path: str | None = None) -> Config:
    for candidate in _candidate_paths(path):
        if candidate.is_file():
            with candidate.open("rb") as f:
                data = tomllib.load(f)
            return _from_dict(data)
    if path is not None:
        raise FileNotFoundError(f"config file not found: {path}")
    return Config()


def _from_dict(data: dict) -> Config:
    allowed = {f for f in Config.__dataclass_fields__}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    kwargs: dict = dict(data)
    if "default_args" in kwargs:
        kwargs["default_args"] = tuple(kwargs["default_args"])
    return Config(**kwargs)
