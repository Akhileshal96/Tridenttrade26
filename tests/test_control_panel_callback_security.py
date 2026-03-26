from pathlib import Path


def test_safe_invoke_preserves_callback_event_for_handler_sender_checks():
    src = Path("control_panel.py").read_text(encoding="utf-8")
    assert "out = fn(event)" in src
    assert "target_event = getattr(event, \"message\", event)" not in src


def test_callback_command_router_enforces_viewer_gate():
    src = Path("control_panel.py").read_text(encoding="utf-8")
    assert 'is_viewer = (handlers or {}).get("__is_viewer__")' in src
    assert "if callable(is_viewer) and not is_viewer(sender_id):" in src


def test_bot_registers_viewer_auth_hook_for_control_panel():
    src = Path("bot.py").read_text(encoding="utf-8")
    assert '"__is_viewer__": _is_viewer' in src
