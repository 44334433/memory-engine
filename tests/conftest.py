"""pytest 全局配置：src 入 sys.path + 集成套件的 daemon 探活。"""
import http.client
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

PORT = int(os.environ.get("MEMORY_ENGINE_PORT", "8766"))


def _daemon_alive() -> bool:
    try:
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=1.5)
        c.request("GET", "/v1/health")
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status == 200 and json.loads(body or b"{}").get("db") is True
    except Exception:
        return False


LIVE_DAEMON = _daemon_alive()


@pytest.fixture(scope="session")
def live_server():
    """引擎 daemon 不可达时 skip（不伪绿），可达时返回 BASE URL。"""
    if not LIVE_DAEMON:
        pytest.skip(f"memory-engine daemon 未运行（127.0.0.1:{PORT}）；"
                    f"先启动引擎再跑集成套件：deploy/memory-engine.sh serve")
    yield f"http://127.0.0.1:{PORT}"
