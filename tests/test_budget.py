"""Every request has a step and a time budget; past either, tool calls are refused, not run."""
from agents.sla import SlaAgent
from support_agent import config
from support_agent.tools import ToolExecutor
from support_agent.tracing import Trace


def test_tool_calls_past_the_step_budget_are_refused(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOOL_STEPS", 2)
    ex = ToolExecutor("U001", Trace(user_id="U001", question="who am I", mode="rules"))
    results = [ex.execute("get_my_profile") for _ in range(3)]
    assert [r.status for r in results[:2]] == ["ok", "ok"]
    assert results[2].status == "error" and "step budget" in results[2].message
    assert len(ex.trace.tool_calls) == 3                  # the refusal is traced too


def test_time_budget_stops_the_agent_without_guessing(monkeypatch):
    monkeypatch.setattr(config, "TURN_BUDGET_S", 0.0)
    resp = SlaAgent().ask("duty.lead", "Which tickets are at risk of breaching SLA?")
    assert resp.status == "error"
    assert resp.tool_calls and all("budget_exceeded" in c["message"] for c in resp.tool_calls)
    assert resp.raw_grounded and not resp.citations


def test_default_budget_leaves_the_walkthroughs_untouched():
    assert config.MAX_TOOL_STEPS >= 8 and config.TURN_BUDGET_S >= 10
