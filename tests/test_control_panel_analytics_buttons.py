import re
from pathlib import Path


def test_analytics_buttons_are_wired_to_panel_handlers():
    cp = Path("control_panel.py").read_text(encoding="utf-8")
    bot = Path("bot.py").read_text(encoding="utf-8")

    analytics_block = re.search(r"def _analytics_buttons\(\):(.*?)\n\n\n_PANEL_MAP", cp, re.S)
    assert analytics_block, "analytics buttons block missing"
    btn_cmds = set(re.findall(r"cp:cmd:([a-z0-9_]+)", analytics_block.group(1)))

    pairs = re.findall(r'"([a-z0-9_]+)": _mk_panel_handler\("([a-z0-9_]+)"\)', bot)
    handlers = {k for k, v in pairs if k == v}

    missing = sorted(c for c in btn_cmds if c not in handlers)
    assert not missing, f"analytics button commands missing handlers: {missing}"


def test_main_panel_pnl_button_is_wired_to_handler():
    cp = Path("control_panel.py").read_text(encoding="utf-8")
    bot = Path("bot.py").read_text(encoding="utf-8")

    main_block = re.search(r"def _main_buttons\(handlers=None\):(.*?)\n\n\ndef _research_buttons", cp, re.S)
    assert main_block, "main buttons block missing"
    assert "cp:cmd:pnl" in main_block.group(1), "pnl button callback is not wired"

    pairs = re.findall(r'"([a-z_]+)": _mk_panel_handler\("([a-z_]+)"\)', bot)
    handlers = {k for k, v in pairs if k == v}
    assert "pnl" in handlers, "pnl panel handler missing in bot.py"
