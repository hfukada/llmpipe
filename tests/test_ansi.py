from llmpipe.ansi import ScreenState


def test_plain_text_delta():
    s = ScreenState(cols=80, rows=24)
    s.feed(b"hello world\r\n")
    assert s.take_delta() == "hello world"
    s.feed(b"next line\r\n")
    assert s.take_delta() == "\nnext line"


def test_delta_strips_ansi_color():
    s = ScreenState(cols=80, rows=24)
    s.feed(b"\x1b[31mred\x1b[0m text\r\n")
    assert s.take_delta() == "red text"


def test_input_box_excluded_from_settled_text():
    s = ScreenState(cols=80, rows=24)
    s.feed(b"assistant reply line\r\n")
    # Draw a fake input box at the bottom.
    s.feed(b"\n\n")
    s.feed("╭──────────────╮\r\n".encode("utf-8"))
    s.feed("│ >            │\r\n".encode("utf-8"))
    s.feed("╰──────────────╯\r\n".encode("utf-8"))
    text = s.settled_text()
    assert "assistant reply line" in text
    assert ">" not in text
    assert "╭" not in text
    assert s.has_input_prompt() is True


def test_spinner_detected():
    s = ScreenState(cols=80, rows=24)
    s.feed("⠋ working...\r\n".encode("utf-8"))
    assert s.has_spinner() is True


def test_modern_input_detected_and_trimmed():
    s = ScreenState(cols=120, rows=24)
    s.feed(b"assistant reply line\r\n")
    s.feed(b"\r\n")
    s.feed(("-" * 100).encode("utf-8") + b"\r\n")
    s.feed(b'\xe2\x9d\xaf Try "how does <filepath> work?"\r\n')
    s.feed(("-" * 100).encode("utf-8") + b"\r\n")
    s.feed(b"  shift+tab to cycle\r\n")
    # Replace ascii dashes with the unicode rule the TUI actually emits.
    s2 = ScreenState(cols=120, rows=24)
    s2.feed(b"assistant reply line\r\n")
    s2.feed(b"\r\n")
    s2.feed(("─" * 100).encode("utf-8") + b"\r\n")
    s2.feed(b'\xe2\x9d\xaf placeholder\r\n')
    s2.feed(("─" * 100).encode("utf-8") + b"\r\n")
    s2.feed(b"  shift+tab to cycle\r\n")
    assert s2.has_input_prompt() is True
    text = s2.settled_text()
    assert "assistant reply line" in text
    assert "placeholder" not in text
    assert "shift+tab" not in text


def test_working_spans_filtered_but_reply_kept():
    s = ScreenState(cols=120, rows=24)
    s.feed(b"first line\r\n")
    s.feed(b"\xe2\x9c\xbb Beaming\xe2\x80\xa6\r\n")   # ✻ Beaming…
    s.feed(b"\xe2\x97\x8f Hi there, friend\r\n")       # ● Hi there
    s.feed(b"\xe2\x9c\xbb Cooked for 2s\r\n")          # ✻ Cooked for 2s
    text = s.settled_text()
    assert "Beaming" not in text
    assert "Cooked for 2s" not in text
    assert "Hi there, friend" in text
    assert "first line" in text


def test_working_span_stripped_when_concatenated_with_reply():
    s = ScreenState(cols=120, rows=24)
    s.feed(b"* Prestidigitating\xe2\x80\xa6\xe2\x97\x8f Hi there friend\r\n")
    text = s.settled_text()
    assert "Prestidigitating" not in text
    assert "Hi there friend" in text


def test_asterisk_working_span_with_ascii_only_input():
    s = ScreenState(cols=120, rows=24)
    s.feed(b"* Levitating...\r\n")
    text = s.settled_text()
    assert "Levitating" not in text


def test_bare_timer_tail_stripped():
    s = ScreenState(cols=120, rows=24)
    s.feed(b"\xe2\x97\x8f reply text\r\n")
    # "(1s · ↓ 4 tokens)" with no leading verb word.
    s.feed(b" (1s \xc2\xb7 \xe2\x86\x93 4 tokens)\r\n")
    text = s.settled_text()
    assert "tokens" not in text
    assert "reply text" in text


def test_post_turn_timer_variants_stripped():
    for line in (b"\xe2\x9c\xbb Cooked for 2s\r\n",
                 b"\xe2\x9c\xbb Baked for 1s\r\n",
                 b"\xe2\x9c\xbb Cooked for 2s \xc2\xb7 \xe2\x86\x93 12 tokens\r\n"):
        s = ScreenState(cols=120, rows=24)
        s.feed(b"\xe2\x97\x8f reply text\r\n")
        s.feed(line)
        text = s.settled_text()
        assert "for" not in text or "reply text" in text
        assert "reply text" in text
        assert "Cooked" not in text and "Baked" not in text


def test_lcp_delta_emits_replacement_content():
    s = ScreenState(cols=120, rows=24)
    s.feed(b"banner\r\n")
    s.feed(b"\xe2\x9c\xbb Beaming\xe2\x80\xa6\r\n")
    d1 = s.take_delta()
    assert "banner" in d1
    # Replace the working line with real assistant content.
    s.feed(b"\xe2\x97\x8f Hi there\r\n")
    d2 = s.take_delta()
    assert "Hi there" in d2


def test_is_working_via_status_footer():
    s = ScreenState(cols=120, rows=24)
    s.feed(b"some content\r\n")
    s.feed(b"  shift+tab to cycle, esc to interrupt\r\n")
    assert s.is_working() is True


def test_is_working_false_when_idle_footer():
    s = ScreenState(cols=120, rows=24)
    s.feed(b"some content\r\n")
    s.feed(b"  shift+tab to cycle, left for agents\r\n")
    assert s.is_working() is False


def test_trust_prompt_detected():
    s = ScreenState(cols=120, rows=24)
    s.feed(b"Quick safety check: Is this a project you trust?\r\n")
    s.feed(b"  1. Yes, I trust this folder\r\n")
    assert s.has_trust_prompt() is True


def test_anchor_to_current_suppresses_subsequent_emit():
    s = ScreenState(cols=80, rows=24)
    s.feed(b"banner line\r\n")
    s.anchor_to_current()
    assert s.take_delta() == ""


def test_delta_idempotent_after_no_new_settled_text():
    s = ScreenState(cols=80, rows=24)
    s.feed(b"line one\r\n")
    s.take_delta()
    # No new content; delta should be empty.
    s.feed(b"")
    assert s.take_delta() == ""
