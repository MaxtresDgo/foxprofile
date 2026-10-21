import pytest

pytest.importorskip("tkinter")

from src import agent_logs  # noqa: E402


def test_read_new_follows_appends_and_restarts(tmp_path, monkeypatch):
    log = tmp_path / "a.log"
    log.write_text("one\ntwo\n", encoding="utf-8")
    text, pos = agent_logs.read_new(str(log), 0)
    assert text == "one\ntwo\n"
    assert agent_logs.read_new(str(log), pos) == ("", pos)
    with open(log, "a", encoding="utf-8") as f:
        f.write("tres ñ\n")
    text, pos = agent_logs.read_new(str(log), pos)
    assert text == "tres ñ\n"
    log.write_text("new\n", encoding="utf-8")  # rewritten: shorter than pos
    assert agent_logs.read_new(str(log), pos) == ("new\n", log.stat().st_size)
    assert agent_logs.read_new(str(tmp_path / "missing.log"), 0) == ("", 0)


def test_read_new_opens_big_files_at_a_whole_line(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_logs, "TAIL", 10)
    log = tmp_path / "a.log"
    log.write_text("aaaaaaaa\nbbbb\ncccc\n", encoding="utf-8")
    text, pos = agent_logs.read_new(str(log), 0)
    assert text == "cccc\n"
    assert pos == log.stat().st_size
