"""集成套件（pytest 入口）：需引擎 daemon 在跑，conftest 探活不可达即 skip。

亦可脚本直跑（原生态）：python3 tests/smoke_test.py / python3 tests/stage2_test.py
"""
import pytest


def test_stage1_smoke_suite(live_server):
    import smoke_test
    assert smoke_test.main() == 0


def test_stage2_lifecycle_suite(live_server):
    import stage2_test
    if stage2_test.psycopg is None:
        pytest.skip("psycopg 未安装（stage2 末尾需直连只读对账）；pip install \"psycopg[binary]\"")
    assert stage2_test.main() == 0
