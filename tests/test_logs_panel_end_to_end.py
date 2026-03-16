import re
from pathlib import Path


def test_logs_panel_buttons_are_mapped_to_handlers():
    cp = Path("control_panel.py").read_text(encoding="utf-8")
    bot_src = Path("bot.py").read_text(encoding="utf-8")

    block = re.search(r"def _logs_buttons\(\):\n(.*?)\n\ndef _help_buttons", cp, re.S)
    assert block, "_logs_buttons block missing"
    log_cmds = set(re.findall(r"cp:cmd:([a-z_]+)", block.group(1)))

    pairs = re.findall(r'"([a-z_]+)": _mk_panel_handler\("([a-z_]+)"\)', bot_src)
    mapped = {k for k, v in pairs if k == v}

    missing = sorted(c for c in log_cmds if c not in mapped)
    assert not missing, f"Logs panel commands missing handler wiring: {missing}"
