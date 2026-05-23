from __future__ import annotations

import re
from dataclasses import dataclass, field

import pyte


# Claude's TUI (v2.1+) draws the input area near the bottom as:
#   ──────────────────────...──────────────────────
#   ❯ Try "..."                                       (or user-typed text)
#   ──────────────────────...──────────────────────
#     ⏵⏵ don't ask on (shift+tab to cycle) · ← for agents
# The reliable idle signature is: a long horizontal-rule line, followed within
# a few lines by a status footer that mentions "shift+tab" / "don't ask" /
# "for agents". Older releases drew a rounded box (`╭─ ... ─╮ / │ > ... │`),
# which we still recognize for compatibility.
_LEGACY_PROMPT_RE = re.compile(r"[│|]\s*>")
_STATUS_FOOTER_RE = re.compile(
    r"shift\+tab|don't ask|for agents|to undo",
    re.IGNORECASE,
)
_RULE_RE = re.compile(r"^─{20,}$")
# Ephemeral working-indicator line, e.g. "✶ Whirlpooling…", "* Beaming…",
# "✻ Cooked for 2s · ↓ 2 tokens". These get replaced as Claude works.
# Signature: starts with a single non-alphanumeric glyph followed by a verb
# ending in "ing…", or "Cooked for Ns". Lines starting with "●" (assistant
# reply marker) are intentionally NOT matched since those are content.
# Ephemeral working-indicator substring. Matches occurrences anywhere on a
# line because the TUI's cursor-up redraws can leave a stale working span
# concatenated with real content on a single visible row.
# Examples it should match:
#   "✶ Whirlpooling…"
#   "* Prestidigitating…"
#   "✻ Cooked for 2s"
#   "✻ Cooked for 2s · ↓ 2 tokens"
_WORKING_SPAN_RE = re.compile(
    r"""
    (?<![●\w])                      # not preceded by reply marker or word char
    [^\w\s●]                        # a single dingbat/asterisk (not ●)
    \s+
    (?:
        \w+ing                      # "...ing"
        (?:[^●\n]*?(?:…|\.\.\.))    # up to an ellipsis
      |
        \w+\s+for\s+\d+\s*s         # "<verb> for Ns" — Cooked/Baked/etc.
        (?:\s*·\s*[^●\n]*?(?=[●]|$))?
    )
    """,
    re.VERBOSE,
)
# Bare timer/tokens tail without the leading verb word, e.g. "(1s · ↓ 4 tokens)".
# Appears when cursor-up has overwritten the verb but left the suffix behind.
_WORKING_TAIL_RE = re.compile(
    r"\(\s*\d+\s*s\s*·[^()●\n]*?tokens?\s*\)"
)
# Box-drawing characters that bracket the input area / status panels.
_BOX_CHARS = set("╭╮╯╰─│┌┐└┘━┃┏┓┗┛")


@dataclass
class ScreenState:
    """Wraps a pyte screen and tracks emitted assistant text."""

    cols: int = 200
    rows: int = 50
    _screen: pyte.Screen = field(init=False)
    _stream: pyte.ByteStream = field(init=False)
    _emitted: str = field(default="", init=False)

    def __post_init__(self) -> None:
        # Plain Screen (no HistoryScreen) — the Claude TUI redraws the
        # conversation area in place rather than scrolling, so scrollback only
        # accumulates spinner-frame noise.
        self._screen = pyte.Screen(self.cols, self.rows)
        self._stream = pyte.ByteStream(self._screen)

    def feed(self, data: bytes) -> None:
        self._stream.feed(data)

    def settled_text(self) -> str:
        """Visible lines above the input region, with ephemeral working lines
        filtered out.
        """
        visible_lines = [
            self._screen.display[r].rstrip()
            for r in range(self._screen.lines)
        ]
        input_start = _find_input_box_start(visible_lines)
        if input_start is None:
            input_start = _find_modern_input_start(visible_lines)
        if input_start is not None:
            visible_lines = visible_lines[:input_start]
        scrubbed = [
            _WORKING_TAIL_RE.sub("", _WORKING_SPAN_RE.sub("", line)).rstrip()
            for line in visible_lines
        ]
        while scrubbed and not scrubbed[-1]:
            scrubbed.pop()
        return "\n".join(scrubbed)

    def take_delta(self) -> str:
        """Return new text appended since last call.

        Uses longest-common-prefix between the previously emitted text and the
        current settled text. This handles the TUI replacing ephemeral lines
        (working indicators) without losing real content that follows.
        """
        full = self.settled_text()
        if full == self._emitted:
            return ""
        if full.startswith(self._emitted):
            delta = full[len(self._emitted):]
            self._emitted = full
            return delta
        # Diverged. Find longest common prefix and emit what's new after it.
        n = min(len(full), len(self._emitted))
        i = 0
        while i < n and full[i] == self._emitted[i]:
            i += 1
        delta = full[i:]
        self._emitted = full
        return delta

    def has_input_prompt(self) -> bool:
        visible_lines = [
            self._screen.display[r] for r in range(self._screen.lines)
        ]
        return _find_input_box_start(visible_lines) is not None or _has_modern_input(visible_lines)

    def anchor_to_current(self) -> None:
        """Mark all currently-settled text as already emitted.

        Used after the trust modal clears so the welcome banner doesn't get
        streamed as assistant output.
        """
        self._emitted = self.settled_text()

    def has_trust_prompt(self) -> bool:
        """Detect Claude's first-launch workspace-trust modal."""
        joined = "\n".join(
            self._screen.display[r] for r in range(self._screen.lines)
        )
        return (
            "trust this folder" in joined
            or "Yes, I trust this folder" in joined
        )

    def is_working(self) -> bool:
        """Return True when Claude is mid-turn.

        Detected via the status footer: when working, it shows "esc to
        interrupt"; when idle it shows "← for agents" / "to undo" / similar.
        Also catches the old braille-spinner indicator as a fallback.
        """
        for r in range(self._screen.lines):
            line = self._screen.display[r]
            if "esc to interrupt" in line:
                return True
            for ch in line:
                if 0x2800 <= ord(ch) <= 0x28FF:
                    return True
        return False

    # Kept for backwards-compat with existing callers/tests.
    def has_spinner(self) -> bool:
        return self.is_working()


def _find_input_box_start(lines: list[str]) -> int | None:
    """Return the row index where the input box begins, or None.

    The box is a contiguous run of box-drawing/prompt lines at the bottom.
    """
    n = len(lines)
    # Walk from bottom up while we see input-box content.
    last_box_row = None
    for r in range(n - 1, -1, -1):
        line = lines[r]
        stripped = line.strip()
        if not stripped:
            if last_box_row is None:
                continue  # trailing blanks below the box
            break
        if _LEGACY_PROMPT_RE.search(line) or _is_box_line(stripped):
            last_box_row = r
            continue
        break
    if last_box_row is None:
        return None
    # Walk further up to capture the box's top border + any contiguous box rows.
    start = last_box_row
    for r in range(last_box_row - 1, -1, -1):
        line = lines[r]
        stripped = line.strip()
        if not stripped:
            break
        if _LEGACY_PROMPT_RE.search(line) or _is_box_line(stripped):
            start = r
            continue
        break
    return start


def _has_modern_input(lines: list[str]) -> bool:
    return _find_modern_input_start(lines) is not None


def _find_modern_input_start(lines: list[str]) -> int | None:
    """Find the first row of the modern input region.

    Signature: a horizontal-rule line near the bottom, with a status footer
    (matching _STATUS_FOOTER_RE) within 5 lines below it. Returns the row
    index of the upper rule, or None if not found.
    """
    n = len(lines)
    if n < 3:
        return None
    last_nonblank: list[int] = [
        r for r in range(n) if lines[r].strip()
    ]
    if not last_nonblank:
        return None
    # The status footer should be on (or near) the last non-blank line.
    footer_row = None
    for r in reversed(last_nonblank[-4:]):
        if _STATUS_FOOTER_RE.search(lines[r]):
            footer_row = r
            break
    if footer_row is None:
        return None
    # Walk up from the footer looking for a horizontal-rule line within 6 rows.
    for r in range(footer_row - 1, max(-1, footer_row - 7), -1):
        if _RULE_RE.match(lines[r].strip()):
            # Then look for another rule line above it, which marks the top.
            for r2 in range(r - 1, max(-1, r - 6), -1):
                if _RULE_RE.match(lines[r2].strip()):
                    return r2
            # Only one rule line found; treat that one as the top.
            return r
    return None


def _is_box_line(stripped: str) -> bool:
    if not stripped:
        return False
    # Mostly box-drawing characters and spaces.
    box_count = sum(1 for c in stripped if c in _BOX_CHARS)
    return box_count >= max(3, len(stripped) // 2)
