import os
import sys

sys.path.insert(0, os.getcwd())

import universe_builder as ub


def test_load_candidates_from_env(monkeypatch):
    monkeypatch.setattr(ub.CFG, "CANDIDATE_SYMBOLS", "SBIN,INFY,SBIN")
    monkeypatch.setattr(ub.CFG, "AUTO_CANDIDATE_DISCOVERY", False)
    monkeypatch.setattr(ub.CFG, "CANDIDATES_PATH", "")
    assert ub.load_nifty100_symbols() == ["SBIN", "INFY"]


def test_load_candidates_from_autodiscovery(monkeypatch, tmp_path):
    out_file = tmp_path / "cand.txt"
    monkeypatch.setattr(ub.CFG, "CANDIDATE_SYMBOLS", "")
    monkeypatch.setattr(ub.CFG, "AUTO_CANDIDATE_DISCOVERY", True)
    monkeypatch.setattr(ub.CFG, "CANDIDATES_PATH", str(out_file))
    monkeypatch.setattr(ub, "_discover_market_candidates", lambda: ["RELIANCE", "TCS"])
    syms = ub.load_nifty100_symbols()
    assert syms == ["RELIANCE", "TCS"]
    assert out_file.read_text().splitlines() == ["RELIANCE", "TCS"]


def test_load_candidates_from_file_when_discovery_empty(monkeypatch, tmp_path):
    in_file = tmp_path / "cand.txt"
    in_file.write_text("AXISBANK\nITC\n")
    monkeypatch.setattr(ub.CFG, "CANDIDATE_SYMBOLS", "")
    monkeypatch.setattr(ub.CFG, "AUTO_CANDIDATE_DISCOVERY", True)
    monkeypatch.setattr(ub.CFG, "CANDIDATES_PATH", str(in_file))
    monkeypatch.setattr(ub, "_discover_market_candidates", lambda: [])
    assert ub.load_nifty100_symbols() == ["AXISBANK", "ITC"]
