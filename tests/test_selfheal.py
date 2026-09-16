"""embedder 自愈线程单测：mock 失败 1 次后成功 → model_loaded 必须被拉回 True（2026-09-16 保活批）。"""
import time


def test_selfheal_pulls_back_to_loaded():
    import importlib
    app = importlib.import_module("memory_engine.app")

    class FakeEmbedder:
        def __init__(self):
            self.calls = 0

        def load(self):
            self.calls += 1
            if self.calls <= 1:
                raise RuntimeError("simulated transient failure")

        def warmup(self):
            pass

    class FakeEng:
        def __init__(self):
            self.embedder = FakeEmbedder()
            self.model_loaded = False

    eng = FakeEng()
    # 用 monkeypatch 时间加速：直接把 sleep 换成 0.01s
    import memory_engine.app as appmod
    orig_sleep = time.sleep
    time.sleep = lambda s: orig_sleep(0.01)
    try:
        appmod._spawn_embedder_selfheal(eng)
        deadline = time.time() + 10
        while time.time() < deadline and eng.model_loaded is False:
            orig_sleep(0.05)
    finally:
        time.sleep = orig_sleep
    assert eng.model_loaded is True, "自愈线程未把 model_loaded 拉回 True"
    assert eng.embedder.calls >= 2, f"重试次数异常: {eng.embedder.calls}"
