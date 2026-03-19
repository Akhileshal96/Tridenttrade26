import os
import sys

sys.path.insert(0, os.getcwd())

import universe_builder as ub


def test_load_candidates_prefers_nifty100_when_env_too_small(monkeypatch):
    monkeypatch.setattr(ub.CFG, "CANDIDATE_SYMBOLS", "SBIN,INFY,SBIN")
    monkeypatch.setattr(ub.CFG, "CANDIDATES_PATH", "")
    syms = ub.load_nifty100_symbols()
    assert len(syms) >= 90


def test_load_candidates_from_env_when_broad(monkeypatch):
    broad = ",".join([f"SYM{i}" for i in range(60)])
    monkeypatch.setattr(ub.CFG, "CANDIDATE_SYMBOLS", broad)
    syms = ub.load_nifty100_symbols()
    assert len(syms) == 60


def test_load_candidates_from_file_when_broad(monkeypatch, tmp_path):
    in_file = tmp_path / "cand.txt"
    in_file.write_text("\n".join([f"FILE{i}" for i in range(55)]))
    monkeypatch.setattr(ub.CFG, "CANDIDATE_SYMBOLS", "")
    monkeypatch.setattr(ub.CFG, "CANDIDATES_PATH", str(in_file))
    syms = ub.load_nifty100_symbols()
    assert len(syms) == 55
