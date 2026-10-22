# OC00770 · Ansh Kumar Pathak · Agentic SAP Support Suite

Four agentic AI assistants for the kind of SAP application management (AMS) work Orane Consulting does for its
clients. Each one is built to the same standard and covers Tasks 1–4 on its own, and they share one demo
(Task 5).

| Agent | Who uses it | Two enterprise APIs (mocked) | Human approval | Never |
|---|---|---|---|---|
| **Support desk assistant** (built first) | Client key users | Service desk (ITSM) + SAP S/4HANA | Role requests: client authorisation owner | Payment-block release, bank details, production transports, SAP_ALL |
| **SLA escalation agent** (use case 1) | AMS duty lead / delivery manager | Service desk ticket queue + client contracts (SLA rules) | Escalations: AMS delivery manager | Close tickets, lower priority, delete tickets, contact the client |
| **Change impact agent** (use case 3) | Change manager | Change management + CMDB | CAB submissions: change manager; CAB decides | Approve changes, import transports, skip tests, mark tests passed |
| **Integration failure agent** (use case 5) | Integration consultant | Integration monitor + SAP S/4HANA (vendors, health) | Reprocessing: integration lead | Resend payment files, re-import statements, delete messages |

All four share the same design:
- **Planner:** rule-based offline, or Claude.
- **Guard layer:** every tool call passes one guard layer: an allowlist, denied tools, argument validation and
  data scoping.
- **API client:** schema validation, retries with backoff, timeouts and idempotency keys.
- **Retrieval:** search over the runbooks, with every fact cited.
- **Grounding check:** an answer containing a fact the tools didn't return is withheld.
- **Approval queue and audit log:** proposed actions wait here, and every decision is logged.

## What is real, mocked or conceptual

| Part | Label |
|---|---|
| Four agent workflows, guard layers, approval queues, citations, hallucination guard | ✅ Working demo |
| Six mock APIs (FastAPI on localhost 8101–8106, with fault injection) | 🟡 Mocked: real HTTP, fake data |
| Clients (Kestrel Foods, Meridian Retail), users, tickets, POs, invoices, changes, CMDB, messages | 🟡 Mock data: fictional |
| Runbooks in `kb/` (SLA, escalation, access, finance, procurement, change, integration, data security) | 🟡 Sample content, not Orane's documents |
| Rule-based planners (default, offline) | ✅ Working demo |
| Claude planner (`claude-opus-5-5`, manual tool-use loop, shared by all four) | 🟡 Implemented but **not run here** (no API key) |
| Test sets and scoring (Task 4), 203 cases | ✅ Working |
| Demo portal: Home, four agent pages with walkthroughs, approvals, monitoring, evaluation | ✅ Working, tested end to end |
| Deployment safeguards: SSO/RBAC, secrets vault, alerting, staged rollout | 📝 Conceptual |

## Run the portal locally

Needs Python 3.11 or newer.
- **Windows:** double-click `run_demo.bat`.
- **macOS / Linux:** run `sh run_demo.sh`.

The first run creates `.venv` and installs the packages, which takes a few minutes. After that it starts in
seconds. Open http://localhost:8501 and keep the terminal window open while you present; Ctrl+C stops it.
The launchers bind to 127.0.0.1, so only your own machine can reach the portal.

## Share it as a deployed URL

The whole app is one process: Streamlit starts the six mock APIs inside the same machine, on localhost ports
8101–8106, so any host that runs Streamlit or Docker can serve it.

**Option A · Streamlit Community Cloud** (free, simplest)
1. Push this folder to a GitHub repository; a private one works. `.gitignore` keeps `.venv/` and `runtime/` out.
2. Sign in at https://share.streamlit.io with GitHub and choose **Create app → Deploy a public app from
   GitHub**.
3. Pick the repository and branch, set **Main file path** to `dashboard/app.py`, and under **Advanced
   settings** choose Python 3.11.
4. Deploy. You get an address like `https://<name>.streamlit.app`.
5. Under **Settings → Sharing**, make the app private and invite your assessor by e-mail if you don't want it
   public.

**Option B · any Docker host** (Hugging Face Spaces with the Docker SDK, Render, Railway, Azure Container Apps)
```bash
docker build -t oc00770-sap-agents .
docker run -p 8501:8501 oc00770-sap-agents        # test locally at http://localhost:8501
```
On a host that sets `$PORT`, the image listens on it automatically.

**Things to know about a shared URL:**
- Everyone who opens it shares the same mock systems and approval queue, so **Reset demo data** resets it for
  every viewer.
- Hosted apps can restart; the mock data simply starts fresh.
- It runs the offline rule planner unless you add `ANTHROPIC_API_KEY` as a secret in the host's settings.
  Never commit the key to the repository.

## Commands for development

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m pytest                                  # 60 tests, incl. the portal end to end
.venv/Scripts/python evals/run_evals.py --agent support         # also: --agent sla | change | integration
.venv/Scripts/python evals/run_evals.py --agent sla --cases blind
.venv/Scripts/python demo_script.py                             # scripted demo of all four agents
.venv/Scripts/python -m streamlit run dashboard/app.py          # Home page -> pick an agent
```

The mock APIs start automatically. Swagger docs are at `http://127.0.0.1:810X/docs` while the app runs. Set
`ANTHROPIC_API_KEY` to use Claude as the planner, or force a mode with `AGENT_MODE=rules|llm`. The demo clock
is fixed at 2026-10-20 10:00, so SLA results don't change between runs.

## Where each task lives

| Task | Support desk | SLA (UC1) | Change (UC3) | Integration (UC5) |
|---|---|---|---|---|
| 1 Agentic workflow | `support_agent/agent.py`, `planner_rules.py`, `tools.py` | `agents/sla.py` | `agents/change.py` | `agents/integration.py` |
| 2 Two APIs: schemas, retries, errors | `mock_apis/service_desk_api.py`, `sap_api.py`, `tests/test_api_resilience.py` | `mock_apis/contracts_api.py` + service desk queue, `tests/test_sla_agent.py` | `mock_apis/changes_api.py`, `cmdb_api.py`, `tests/test_change_agent.py` | `mock_apis/integration_api.py` + SAP vendors/health, `tests/test_integration_agent.py` |
| 3 Retrieval, citations, approval, restrictions | `support_agent/retrieval.py`, `grounding.py`, `approvals.py`, `tests/test_agent_safety.py` | `kb/escalation_procedure.md`, `agents/actions.py` | `kb/change_assessment_rules.md` | `kb/integration_runbook.md` |
| 4 Test set | `evals/cases.jsonl`, `holdout.jsonl`, `blind.jsonl` | `evals/sla_cases.jsonl`, `sla_blind.jsonl` | `evals/change_*.jsonl` | `evals/integration_*.jsonl` |

Shared code: `agents/base.py` (guard layer and agent loop), `agents/actions.py` (approval queue),
`support_agent/api_client.py` (retries, schemas, idempotency), `evals/run_evals.py` (scoring),
`dashboard/app.py` (Task 5).

## Results (rules planner)

| Agent | Development | Blind | Made-up answers | Unsafe actions executed |
|---|---|---|---|---|
| Support desk | 98% of 57 (held-out 100% of 14) | 75% of 20 | 0% | 0 |
| SLA escalation | 100% of 23 | 40% of 15 | 0% dev, 7% blind (1 case) | 0 |
| Change impact | 96% of 22 | 40% of 15 | 0% | 0 |
| Integration failure | 100% of 22 | 67% of 15 | 0% | 0 |

Typical response time is around 30 ms, and the 95th percentile is under 85 ms. With injected outages, the
95th percentile is 0.7–1.3 s.

How far to trust these numbers:
- **Development sets:** the rules were tuned on these, so they are optimistic.
- **Blind sets:** written by separate AI agents that saw only the data and runbooks, never the code, and run
  once without tuning. These are the honest figures.

The blind misses are almost all wording the rule planners don't recognise: a ticket described as "the
Meridian store report ticket", "the 501 assessment" without "CR-", or "knock it down to P4". Across all 203
cases, every miss failed safe: no restricted action took effect, and 44 unsafe attempts were stopped by the
guard layers. That gap is the main limitation and the reason for the recommendation. Switch on the Claude
planner (already built) behind the same guard layers, and test it on anonymised real AMS tickets.

## Business rules

- **Support desk:** see `support_agent/config.py`.
  - Own-account unlock only, and only after failed logins.
  - Standard roles wait for approval; sensitive roles are refused.
  - Users see their own company's data only.
- **SLA:**
  - Deadline = opened + contract resolution hours.
  - At risk = under 25% of the resolution window left, or under 2 hours left.
  - Only breached or at-risk tickets are escalated, with one pending escalation per ticket.
- **Change:**
  - High criticality: CAB approval and full regression.
  - Interfaces: integration test with each connected system.
  - High criticality due within 5 business days: expedited CAB and client IT head sign-off.
  - Only low criticality, no interfaces: business owner sign-off.
  - Object not in the CMDB: manual review; never assumed safe.
  - Approved changes are not reassessed.
- **Integration:**
  - One error type per request, at most 50 messages.
  - IDocs need the vendor to exist in SAP; POS reprocessing needs system health green.
  - MT940-DUP and PAY-AUTH messages are never reprocessed.

## Presenting the demo

1. Start the portal and click **Reset demo data** in the sidebar for a clean start.
2. On **Home**, open an agent. Each agent page has six numbered walkthrough steps:
   - Steps 1–2 look up and explain, with citations.
   - Step 3 proposes an action. It appears under **Waiting for approval** on the right; click **Approve**,
     and the **Live view of the mock systems** shows the change land.
   - Step 4 shows a business rule saying no.
   - Steps 5–6 show a restricted action being blocked and a runbook answer.
3. Expand **Sources and tool calls** under any answer to show its citations and the guard layer's decisions.
4. Turn up the **Chaos** slider to show retries and honest "system unavailable" answers. The live view keeps
   working while you do.
5. Finish on **Approvals** (every decision and the audit log), **Monitoring** (filter by agent) and
   **Evaluation**.

`tests/test_portal.py` runs exactly this flow for all four agents, including reset and chaos mode.

## Submitting

Leave out `.venv/` (over 400 MB; the install step recreates it). `runtime/` holds the demo traces for the
Monitoring page, and `evals/results/` holds the latest scores.
