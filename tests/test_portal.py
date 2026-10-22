"""End-to-end test of the demo portal (Task 5), driven through Streamlit's app tester.

For each agent it clicks the six walkthrough steps in order, checks the outcome
of each, approves step 3 on the agent's own page and confirms the change landed
in the mock system. It also checks Home, Reset, Approvals, Monitoring and
Evaluation render without errors.
"""
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from mock_apis import launcher
from support_agent.tracing import load_traces

APP = str(Path(__file__).resolve().parent.parent / "dashboard" / "app.py")

EXPECTED = {
    "Support desk assistant": ("support", ["answered", "answered", "pending_approval", "blocked", "blocked", "blocked"]),
    "SLA escalation agent": ("sla", ["answered", "answered", "pending_approval", "denied", "blocked", "answered"]),
    "Change impact agent": ("change", ["answered", "answered", "pending_approval", "answered", "blocked", "denied"]),
    "Integration failure agent": ("integration",
                                  ["answered", "answered", "pending_approval", "pending_approval", "blocked", "answered"]),
}


def landed(key: str) -> bool:
    """Did the approved step-3 action actually change the mock system?"""
    get = lambda name, path: httpx.get(f"{launcher.URLS[name]}{path}").json()   # noqa: E731
    if key == "support":
        return "Z_MM_DISPLAY" in get("sap", "/sap-users/ASHAV")["roles"]
    if key == "sla":
        return len(get("service_desk", "/queue/escalations")) == 1
    if key == "change":
        return get("changes", "/changes/CR-502")["status"] == "assessed"
    return get("integration", "/messages/MSG-5004")["status"] == "reprocessed"


def nothing_landed(key: str) -> bool:
    get = lambda name, path: httpx.get(f"{launcher.URLS[name]}{path}").json()   # noqa: E731
    return {"support": lambda: "Z_MM_DISPLAY" not in get("sap", "/sap-users/ASHAV")["roles"],
            "sla": lambda: get("service_desk", "/queue/escalations") == [],
            "change": lambda: get("changes", "/changes/CR-502")["status"] == "awaiting_assessment",
            "integration": lambda: get("integration", "/reprocess-log") == []}[key]()


@pytest.fixture()
def app():
    at = AppTest.from_file(APP, default_timeout=120).run()
    assert not at.exception, at.exception
    return at


def test_home_lists_four_agents_and_opens_each(app):
    opens = [b for b in app.button if b.key and b.key.startswith("open_")]
    assert len(opens) == 4
    opens[1].click().run()
    assert not app.exception and app.sidebar.radio[0].value == "SLA escalation agent"


@pytest.mark.parametrize("page", list(EXPECTED))
def test_walkthrough_end_to_end(app, page):
    key, statuses = EXPECTED[page]
    app.sidebar.radio[0].set_value(page).run()
    assert not app.exception, app.exception
    for step, want in enumerate(statuses, start=1):
        before = len(load_traces())
        app.button(key=f"step_{key}_{step}").click().run()
        assert not app.exception, (page, step, app.exception)
        assert len(load_traces()) == before + 1, f"step {step} did not reach the agent"
        assert load_traces()[-1]["status"] == want, (page, step, load_traces()[-1]["answer"])
        if step == 3:
            assert nothing_landed(key), "the action took effect before approval"
            approve = [b for b in app.button if b.key and b.key.startswith(f"{key}_ok")]
            assert approve, "no approval card on the agent page"
            approve[0].click().run()
            assert not app.exception, app.exception
            assert landed(key), "approved action did not reach the mock system"
    assert len(app.chat_message) == 12                     # six questions, six answers
    leaked = [e.value for e in list(app.markdown) + list(app.code) if "pd.DataFrame" in str(e.value)]
    assert not leaked, f"source code rendered on the page: {leaked[:1]}"   # Streamlit 'magic' regression


def test_reset_restores_the_starting_data(app):
    app.sidebar.radio[0].set_value("SLA escalation agent").run()
    app.button(key="step_sla_3").click().run()
    [b for b in app.button if b.key and b.key.startswith("sla_ok")][0].click().run()
    assert landed("sla")
    app.sidebar.button[0].click().run()                    # Reset demo data
    assert not app.exception and nothing_landed("sla")
    assert len(app.chat_message) == 0


def test_chaos_slider_gives_answers_or_honest_errors(app):
    app.sidebar.radio[0].set_value("Integration failure agent").run()
    app.slider(key="chaos_integration").set_value(0.8).run()
    for _ in range(4):
        app.button(key="step_integration_1").click().run()
        assert not app.exception, app.exception
        assert load_traces()[-1]["status"] in ("answered", "error")
        assert load_traces()[-1]["grounded"]
    assert not [w for w in app.warning if "Couldn't read" in str(w.value)]   # live view ignores chaos
    app.slider(key="chaos_integration").set_value(0.0).run()


def test_api_condition_applies_to_one_request_at_a_time(app):
    app.sidebar.radio[0].set_value("SLA escalation agent").run()
    # "normal" last: whatever a failing request didn't consume must not leak into the next one.
    # A 404 from the ticket-queue list is a broken system, not a missing ticket: the agent stops.
    for cond, want, retried in [("unavailable", "error", True), ("transient", "answered", True),
                                ("not_found", "error", False), ("normal", "answered", False)]:
        app.selectbox(key="cond_sla").set_value(cond).run()
        app.button(key="step_sla_1").click().run()
        assert not app.exception, app.exception
        trace = load_traces()[-1]
        assert trace["status"] == want, (cond, trace["answer"])
        assert (sum(c["api_retries"] for c in trace["tool_calls"]) > 0) == retried, cond
        assert trace["grounded"]
    assert any("API condition: Unavailable" in str(c.value) for c in app.caption)


def test_answers_show_their_source_records_and_budget(app):
    app.sidebar.radio[0].set_value("SLA escalation agent").run()
    app.button(key="step_sla_1").click().run()
    assert any(e.label.startswith("Source records behind the citations") for e in app.expander)
    assert any('"INC-2008"' in str(c.value) for c in app.code)
    assert any("Budget used: 1 of 8 tool calls" in str(c.value) for c in app.caption)


@pytest.mark.parametrize("page", list(EXPECTED))
def test_task_tabs_and_edge_cases(app, page):
    key = EXPECTED[page][0]
    app.sidebar.radio[0].set_value(page).run()
    assert [t.label for t in app.tabs] == ["Task 1 · Workflow", "Task 2 · Two APIs",
                                           "Task 3 · Evidence and approval", "Task 4 · Tests"]
    for i, want in ((1, "answered"), (2, "denied")):     # healthy record, then a missing one
        app.button(key=f"edge_{key}_{i}").click().run()
        assert not app.exception, app.exception
        assert load_traces()[-1]["status"] == want, (page, i, load_traces()[-1]["answer"])


def test_documents_can_be_downloaded(app):
    docs = app.sidebar.get("download_button")
    assert len(docs) >= 3, "expected the brief, the guide and the deck in the sidebar"


@pytest.mark.parametrize("page", ["Approvals", "Monitoring", "Evaluation"])
def test_other_pages_render(app, page):
    app.sidebar.radio[0].set_value("Integration failure agent").run()
    app.button(key="step_integration_3").click().run()      # something to approve and monitor
    app.sidebar.radio[0].set_value(page).run()
    assert not app.exception, (page, app.exception)
    if page == "Approvals":
        assert any(b.label == "Approve" for b in app.button)
