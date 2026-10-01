import json
import types

import pytest

from mentor import agent, cost, db, importer


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.seed_gaps(c)
    return c


def test_import_is_idempotent_and_skips_empty(conn, tmp_path):
    f = tmp_path / "n.md"
    f.write_text("# Arch\n\n## Q: Design X?\nType: real\nCategory: architecture\nMy answer: a\n b\nNotes: n\n\n"
                 "## Q:\nType:\nCategory:\n")
    assert importer.import_file(conn, f) == 1
    assert importer.import_file(conn, f) == 0
    row = conn.execute("SELECT * FROM scenario").fetchone()
    assert row["source"] == "real" and row["my_answer"] == "a b"


def test_pick_scenario_rotates(conn):
    for i in range(2):
        conn.execute("INSERT INTO scenario(category, prompt) VALUES ('leadership', ?)", (f"q{i}",))
    a, b = db.pick_scenario(conn, "leadership"), db.pick_scenario(conn, "leadership")
    assert a["id"] != b["id"]


def test_spaced_repetition(conn):
    g = db.due_items(conn)[0]["id"]
    assert db.record_quiz(conn, g, True) == 2
    assert db.record_quiz(conn, g, True) == 4
    assert db.record_quiz(conn, g, False) == 1
    assert db.due_items(conn, 10) == db.due_items(conn, 10)  # stable
    assert all(i["id"] != g for i in db.due_items(conn, 10))  # pushed to the future


def test_cost_and_budget(conn):
    u = types.SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000,
                              cache_read_input_tokens=0, cache_creation_input_tokens=0)
    assert cost.log_usage(conn, "claude-sonnet-5-5", u) == pytest.approx(3.0)
    assert cost.check_budget(conn) == (True, "")
    for _ in range(3):
        cost.log_usage(conn, "claude-sonnet-5-5", u)  # now $12 > $10
    ok, msg = cost.check_budget(conn)
    assert not ok and "budget" in msg.lower()


class FakeClient:
    """Scripted responses; stands in for anthropic.Anthropic()."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = self
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def R(blocks, stop):
    u = types.SimpleNamespace(input_tokens=100, output_tokens=50,
                              cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return types.SimpleNamespace(content=blocks, stop_reason=stop, usage=u)


def test_agent_loop_runs_tool_then_returns_text(conn):
    tool = types.SimpleNamespace(type="tool_use", id="t1", name="get_due_items", input={})
    text = types.SimpleNamespace(type="text", text="Quiz time!")
    client = FakeClient([R([tool], "tool_use"), R([text], "end_turn")])
    msgs = [{"role": "user", "content": "go"}]
    assert agent.agent_turn(client, conn, msgs) == "Quiz time!"
    result_msg = msgs[2]["content"][0]
    assert result_msg["tool_use_id"] == "t1" and "Kafka" in result_msg["content"]


def test_save_assessment_and_bad_gap_id(conn):
    out = agent.run_tool(None, conn, "save_assessment",
                         {"scenario_id": 1, "scores": {"clarity": 4}, "strengths": ["x"], "gaps": []})
    assert out["session_id"] == 1
    assert "error" in agent.run_tool(None, conn, "record_quiz_result", {"gap_id": 999, "correct": True})


def test_quiz_shuffles_and_survives_bad_json(conn):
    good = types.SimpleNamespace(type="text", text='{"question":"Q?","correct":"A","wrong":["B","C","D"]}')
    out = agent.generate_quiz(FakeClient([R([good], "end_turn")]), conn, "t", "c")
    assert sorted(out["options"]) == ["A", "B", "C", "D"] and out["answer"] == "A"
    bad = types.SimpleNamespace(type="text", text="no json")
    assert "error" in agent.generate_quiz(FakeClient([R([bad], "end_turn")]), conn, "t", "c")


def test_budget_blocks_api_call(conn):
    conn.execute("INSERT INTO usage(at, model, cost_usd) VALUES (datetime('now','localtime'), 'm', 11)")
    client = FakeClient([])
    assert "budget" in agent.agent_turn(client, conn, [{"role": "user", "content": "x"}]).lower()
    assert client.calls == []


@pytest.mark.parametrize("content", ["sk-test\n", "ANTHROPIC_API_KEY=sk-test\n", "# c\nexport ANTHROPIC_API_KEY=\"sk-test\"\n"])
def test_load_api_key_formats(tmp_path, monkeypatch, content):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    f = tmp_path / ".env"
    f.write_text(content)
    agent.load_api_key(f)
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test"


def test_load_api_key_does_not_override_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "already")
    f = tmp_path / ".env"
    f.write_text("other\n")
    agent.load_api_key(f)
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "already"
