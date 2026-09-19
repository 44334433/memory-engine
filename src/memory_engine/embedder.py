"""嵌入器：可插拔 Provider 架构（P1 第一批拍板 2026-09-16）。

- EmbeddingProvider：抽象接口（load/warmup/embed_documents/embed_queries + loaded/device）
- Qwen3EmbeddingProvider：Qwen3-Embedding-0.6B fp16 常驻 CUDA 进程内（默认，行为与原 Embedder 逐字一致）
- OpenAICompatProvider：OpenAI 兼容 /embeddings 远程端点（LLM_GATEWAY_BASE_URL + LLM_GATEWAY_API_KEY）
- build_embedder()：按 config.EMBED_PROVIDER（qwen3 | openai_compat）工厂构造

换模型 = 换 EMBED_PROVIDER/模型目录 + scripts/reembed_backfill.py 批量重嵌（后台任务式，不阻塞服务）。
Qwen3 实现细节（保持原行为）：
- last-token pooling + L2 归一（官方模型卡实现）
- query 侧拼英文 instruction，document 侧不拼
- 线程锁串行化 GPU 调用（FastAPI 同步端点跑线程池）；锁按 chunk(EMBED_BATCH=16) 粒度
  获取/释放——整批不再独占，读路径（recall 查询嵌入）排队上限=单 chunk（2026-09-19 拍板）
- torchaudio stub：transformers 5.x Qwen3 模型链会 import torchaudio，
  本机 user-site torchaudio(cu130) 与 torch(cu132) 不匹配 → 进程内 stub 绕过（纯文本嵌入不受影响）
"""
import abc
import json
import logging
import threading
import sys
import time
import types
import urllib.request

import torch
from transformers import AutoModel, AutoTokenizer

from . import config

log = logging.getLogger("memory-engine.embedder")


def _guard_torchaudio() -> None:
    try:
        import torchaudio  # noqa: F401
    except Exception:
        sys.modules.pop("torchaudio", None)
        stub = types.ModuleType("torchaudio")
        stub.__version__ = "stub(process-local, CUDA-mismatch bypass)"
        sys.modules["torchaudio"] = stub


_guard_torchaudio()


class EmbeddingProvider(abc.ABC):
    """嵌入 Provider 抽象接口（P1 可插拔拍板）。

    契约：load() 幂等可重试；load 失败允许抛异常（app 层降级启动，daemon 不挂）；
    embed_* 失败直接抛——recall 层按「嵌入路失败→fts-only 降级」处理，禁在本层吞错。
    """

    name = "base"

    def __init__(self) -> None:
        self._loaded = False
        self.device = "remote"

    @property
    def loaded(self) -> bool:
        return self._loaded

    @abc.abstractmethod
    def load(self) -> None: ...

    @abc.abstractmethod
    def warmup(self, rounds: int = 2) -> None: ...

    @abc.abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    @abc.abstractmethod
    def embed_queries(self, queries: list[str]) -> list[list[float]]: ...


class Qwen3EmbeddingProvider(EmbeddingProvider):
    """Qwen3-Embedding-0.6B fp16 常驻 CUDA 进程内（默认 Provider，原 Embedder 实现）。"""

    name = "qwen3"

    def __init__(self) -> None:
        super().__init__()
        self.tokenizer = None
        self.model = None
        self.device = config.EMBED_DEVICE
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self.model is not None

    def load(self) -> None:
        tok = AutoTokenizer.from_pretrained(str(config.MODEL_DIR), padding_side="left")
        try:  # transformers 5.x 用 dtype，4.x 用 torch_dtype
            model = AutoModel.from_pretrained(str(config.MODEL_DIR), dtype=torch.float16)
        except TypeError:
            model = AutoModel.from_pretrained(str(config.MODEL_DIR), torch_dtype=torch.float16)
        model = model.to(self.device).eval()
        self.tokenizer, self.model = tok, model
        self._loaded = True
        log.info("embedder loaded: %s -> %s", config.MODEL_DIR, self.device)

    def warmup(self, rounds: int = 2) -> None:
        """dummy encode ×N：触发 kernel 编译与显存池预分配（蓝图 §8.2）。"""
        for _ in range(rounds):
            self.embed_documents(["预热 warmup sentence。", "warmup second sentence."])
        log.info("embedder warmup done (%d rounds)", rounds)

    @torch.no_grad()
    def _encode_chunk(self, chunk: list[str]) -> list[list[float]]:
        """单 chunk（≤EMBED_BATCH）编码——锁作用域即此粒度（零成本优化②，见 _encode）。"""
        batch = self.tokenizer(
            chunk,
            padding=True,
            truncation=True,
            max_length=config.EMBED_MAX_LEN,
            return_tensors="pt",
        ).to(self.model.device)
        hidden = self.model(**batch).last_hidden_state
        mask = batch["attention_mask"]
        if bool((mask[:, -1].sum() == mask.shape[0])):  # left padding → 末 token 即句末
            emb = hidden[:, -1]
        else:
            seq_len = mask.sum(dim=1) - 1
            emb = hidden[torch.arange(hidden.size(0), device=hidden.device), seq_len]
        emb = torch.nn.functional.normalize(emb, p=2, dim=1)
        return emb.float().cpu().tolist()

    def _encode(self, texts: list[str]) -> list[list[float]]:
        """按 chunk 持锁：GPU 串行保证不变（单 chunk 内不并发触模型），但 chunk 之间放锁——
        旧版整批一把锁（retain 20 条批量期间 recall 查询嵌入排队 ~153ms），
        现读路径排队上限 = 单个在算 chunk（研究-提取失败游标 §2.1/§2.3-C，2026-09-19 拍板）。
        模型 eval 态无状态，chunk 间交错安全；输出顺序仍按输入序拼接。
        chunk 边界 sleep(0)：强制让出 GIL，防止释放-重获零窗口被同线程 barge 掉
        （CPython 锁非 FIFO，实测竞争线程可被连续抢占整批——违背本优化初衷）。"""
        out: list[list[float]] = []
        bs = config.EMBED_BATCH
        first = True
        for i in range(0, len(texts), bs):
            if not first:
                time.sleep(0)
            first = False
            with self._lock:
                out.extend(self._encode_chunk(texts[i : i + bs]))
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts)

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        prefixed = [
            f"Instruct: {config.EMBED_QUERY_INSTRUCTION}\nQuery: {q}" for q in queries
        ]
        return self._encode(prefixed)


class OpenAICompatProvider(EmbeddingProvider):
    """OpenAI 兼容 /embeddings 远程 Provider（走 LLM_GATEWAY_API_KEY 鉴权）。

    - load() 无本地加载动作（远程就绪性在首次调用时暴露，失败由 recall 降级分支接管）
    - warmup() best-effort：失败仅告警不炸启动（远程不可达时 daemon 仍以 fts-only 降级态服务）
    """

    name = "openai_compat"

    def __init__(self) -> None:
        super().__init__()
        self.base_url = config.LLM_GATEWAY_BASE_URL.rstrip("/")
        self.api_key = config.LLM_GATEWAY_API_KEY
        self.remote_model = config.EMBED_REMOTE_MODEL
        self.timeout_s = config.EMBED_REMOTE_TIMEOUT
        if not self.api_key:
            log.warning("openai_compat: LLM_GATEWAY_API_KEY 未配置——嵌入调用将失败（recall 降级 fts-only）")

    def load(self) -> None:
        self._loaded = True   # 远程 Provider 无本地加载；就绪性由调用期探测
        log.info("embedder provider=openai_compat base=%s model=%s", self.base_url, self.remote_model)

    def warmup(self, rounds: int = 2) -> None:
        try:
            self.embed_documents(["warmup sentence."])
            log.info("openai_compat warmup ok")
        except Exception as e:  # noqa: BLE001 —— 远程预热失败不炸启动（P1 降级语义）
            log.warning("openai_compat warmup failed（recall 将降级 fts-only）: %s", e)

    def _post(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.remote_model, "input": texts}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/embeddings", data=payload, method="POST",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            data = json.loads(resp.read())
        out = [d["embedding"] for d in data.get("data", [])]
        if len(out) != len(texts):
            raise RuntimeError(f"openai_compat 返回条数不符: got {len(out)} want {len(texts)}")
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._post(texts)

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        # TODO(P2 换模实拍板时): 远程 Qwen3 端点是否需拼 query instruction 由网关模型卡定
        return self._post(queries)


# 向后兼容别名：既有引用（文档/脚本/测试）中的 Embedder 即 Qwen3 实现
Embedder = Qwen3EmbeddingProvider


def build_embedder() -> EmbeddingProvider:
    """按 config.EMBED_PROVIDER 构造嵌入 Provider（qwen3 | openai_compat）。"""
    p = (config.EMBED_PROVIDER or "qwen3").strip().lower()
    if p == Qwen3EmbeddingProvider.name:
        return Qwen3EmbeddingProvider()
    if p == OpenAICompatProvider.name:
        return OpenAICompatProvider()
    raise ValueError(f"未知 EMBED_PROVIDER: {p!r}（允许 qwen3 | openai_compat）")
