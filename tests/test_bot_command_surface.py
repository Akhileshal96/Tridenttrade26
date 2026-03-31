import re
from pathlib import Path


BOT = Path("bot.py").read_text(encoding="utf-8")


def _dispatch_commands() -> set[str]:
    cmds = set(re.findall(r'cmd_word == "(/[^"\n]+)"', BOT))
    for tup in re.findall(r'cmd_word in \(([^\)]+)\)', BOT):
        cmds.update(re.findall(r'"(/[^"\n]+)"', tup))
    return cmds


def _help_commands() -> set[str]:
    block = re.search(r'HELP_TEXT = \((.*?)\n\)', BOT, re.S)
    assert block, "HELP_TEXT missing"
    cmds = set(re.findall(r'•\s*(/\w+)', block.group(1)))
    return cmds


def _panel_commands() -> set[str]:
    return set(re.findall(r'"([a-z_]+)": _mk_panel_handler\("([a-z_]+)"\)', BOT))


def test_help_commands_have_dispatch_handlers():
    dispatch = _dispatch_commands()
    help_cmds = _help_commands()
    missing = sorted(c for c in help_cmds if c not in dispatch)
    assert not missing, f"HELP commands missing dispatcher handlers: {missing}"


def test_panel_handlers_are_self_consistent():
    pairs = _panel_commands()
    assert pairs, "panel_handlers mapping not found"
    bad = sorted(p for p in pairs if p[0] != p[1])
    assert not bad, f"panel handler key mismatch: {bad}"
