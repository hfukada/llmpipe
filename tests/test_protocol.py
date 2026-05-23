import pytest

from llmpipe import protocol


def test_parse_prompt():
    msg = protocol.parse_client_message({"type": "prompt", "text": "hi"})
    assert msg == {"type": "prompt", "text": "hi"}


def test_parse_interrupt():
    assert protocol.parse_client_message({"type": "interrupt"}) == {"type": "interrupt"}


def test_parse_ping():
    assert protocol.parse_client_message({"type": "ping"}) == {"type": "ping"}


def test_parse_rejects_non_object():
    with pytest.raises(ValueError):
        protocol.parse_client_message("hello")


def test_parse_rejects_unknown_type():
    with pytest.raises(ValueError):
        protocol.parse_client_message({"type": "wat"})


def test_parse_rejects_prompt_without_text():
    with pytest.raises(ValueError):
        protocol.parse_client_message({"type": "prompt"})


def test_event_builders():
    assert protocol.text_event("hi") == {"type": "text", "delta": "hi"}
    assert protocol.idle_event() == {"type": "idle"}
    assert protocol.ready_event() == {"type": "ready"}
    assert protocol.exit_event(0) == {"type": "exit", "code": 0}
    assert protocol.error_event("x") == {"type": "error", "message": "x"}
