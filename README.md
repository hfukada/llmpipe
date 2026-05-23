Maybe this isn't necessary



# llmpipe

A WebSocket server that holds open one or many interactive `claude` (Claude Code) sessions.
Each WebSocket connection owns a single Claude session, running under a PTY exactly as if
you launched `claude` in a terminal — no `--print`, no `--output-format`, no API key.

## Install

```sh
pip install -e .
```

Requires Python 3.11+ and the `claude` CLI on PATH (or set `claude_binary` in config).

## Run

```sh
cp config.example.toml config.toml
llmpipe --config config.toml
```

The server listens on `127.0.0.1:8765` by default.

## Wire protocol

Connect to `ws://host:port/session`. Optional query params:

- `cwd=/abs/path` — working directory for the Claude session (if `allow_cwd_override` is true).
- `args=...` — shell-quoted extra CLI args (if `allow_args_override` is true).

### Client → server (JSON text frames)

```json
{"type": "prompt", "text": "your prompt here"}
{"type": "interrupt"}
{"type": "ping"}
```

### Server → client

```json
{"type": "ready"}                  // session spawned and idle, awaiting first prompt
{"type": "text", "delta": "..."}   // streamed assistant text (ANSI-stripped, plain UTF-8)
{"type": "idle"}                   // Claude is awaiting input again (turn end)
{"type": "pong"}
{"type": "error", "message": "..."}
{"type": "exit", "code": 0}        // claude process exited
```

## Test client

A simple interactive REPL client ships with the package:

```sh
llmpipe-client                              # connects to ws://127.0.0.1:8765/session
llmpipe-client --url ws://host:port/session
llmpipe-client --cwd /some/path             # forwarded as ?cwd= query param
```

Type prompts at the `>` prompt; assistant text streams inline as it arrives,
with dim status markers (`[ready]`, `[idle]`, `[error]`, `[exit]`) for protocol
events. EOF / Ctrl-D / Ctrl-C exits cleanly.

## Configuration

See `config.example.toml`. The default args (`--permission-mode dontAsk`) suppress
interactive permission prompts. Edit `default_args` if you want different defaults
(model, agent, etc.).

## Design notes

- The `claude` TUI runs under a pseudo-terminal (`pty.openpty`). Bytes from the master
  fd are fed into a `pyte.HistoryScreen` virtual terminal.
- Assistant text is extracted by reading the settled (scrolled-off + visible-above-input-box)
  region of the virtual terminal and diffing against what we've already emitted.
- "Idle" — the moment Claude is ready for the next prompt — is detected by: no PTY
  output for `idle_debounce_ms`, no braille-spinner glyph on screen, and the input box
  visible at the bottom of the screen.

## Tests

```sh
pip install -e '.[dev]'
pytest
```
