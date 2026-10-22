"""Start the mock enterprise APIs as real HTTP servers on localhost, in background threads.

Running them over real HTTP (instead of calling functions directly) means the
client's timeouts, retries and status-code handling are exercised for real.

  8101 service desk (ITSM)       support desk + SLA agent
  8102 SAP S/4HANA               support desk + integration agent
  8103 client contracts / SLAs   SLA agent
  8104 change management         change impact agent
  8105 CMDB (dependency catalog) change impact agent
  8106 integration monitor       integration failure agent
"""
import threading
import time

import httpx
import uvicorn

SERVICE_DESK_URL = "http://127.0.0.1:8101"
SAP_URL = "http://127.0.0.1:8102"
CONTRACTS_URL = "http://127.0.0.1:8103"
CHANGES_URL = "http://127.0.0.1:8104"
CMDB_URL = "http://127.0.0.1:8105"
INTEGRATION_URL = "http://127.0.0.1:8106"

APPS = {
    "service_desk": ("mock_apis.service_desk_api:app", SERVICE_DESK_URL),
    "sap": ("mock_apis.sap_api:app", SAP_URL),
    "contracts": ("mock_apis.contracts_api:app", CONTRACTS_URL),
    "changes": ("mock_apis.changes_api:app", CHANGES_URL),
    "cmdb": ("mock_apis.cmdb_api:app", CMDB_URL),
    "integration": ("mock_apis.integration_api:app", INTEGRATION_URL),
}
URLS = {name: url for name, (_, url) in APPS.items()}

_started = False
_lock = threading.Lock()


def _is_up(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=0.5).status_code == 200
    except httpx.HTTPError:
        return False


def _serve(app_path: str, port: int) -> None:
    config = uvicorn.Config(app_path, host="127.0.0.1", port=port, log_level="warning")
    uvicorn.Server(config).run()


def ensure_running() -> None:
    global _started
    with _lock:
        if _started:
            return
        for app_path, url in APPS.values():
            if not _is_up(url):
                port = int(url.rsplit(":", 1)[1])
                threading.Thread(target=_serve, args=(app_path, port), daemon=True).start()
        deadline = time.time() + 15
        while time.time() < deadline and not all(_is_up(u) for u in URLS.values()):
            time.sleep(0.1)
        down = [n for n, u in URLS.items() if not _is_up(u)]
        if down:
            raise RuntimeError(f"mock APIs did not start: {', '.join(down)}")
        _started = True


def set_faults(url: str, script: list[str] | None = None, random_rate: float = 0.0) -> None:
    httpx.post(f"{url}/_admin/faults", json={"script": script or [], "random_rate": random_rate}, timeout=2)


def reset_all() -> None:
    for url in URLS.values():
        httpx.post(f"{url}/_admin/reset", timeout=2)


if __name__ == "__main__":
    ensure_running()
    for name, url in URLS.items():
        print(f"{name:13} {url}/docs")
    print("Ctrl+C to stop.")
    while True:
        time.sleep(3600)
