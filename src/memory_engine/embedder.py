"""嵌入器：Qwen3-Embedding-0.6B fp16 常驻 CUDA，进程内（蓝图 §2/研究§5.1）。
- last-token pooling + L2 归一（官方模型卡实现）
- query 侧拼英文 instruction，document 侧不拼
- 线程锁串行化 GPU 调用（FastAPI 同步端点跑线程池）
- torchaudio stub：transformers 5.x Qwen3 模型链会 import torchaudio，
  本机 user-site torchaudio(cu130) 与 torch(cu132) 不匹配 → 进程内 stub 绕过（纯文本嵌入不受影响）
"""
import threading
import logging
import sys
import types

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


class Embedder:
    def __init__(self) -> None:
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
        log.info("embedder loaded: %s -> %s", config.MODEL_DIR, self.device)

    def warmup(self, rounds: int = 2) -> None:
        """dummy encode ×N：触发 kernel 编译与显存池预分配（蓝图 §8.2）。"""
        for _ in range(rounds):
            self.embed_documents(["预热 warmup sentence。", "warmup second sentence."])
        log.info("embedder warmup done (%d rounds)", rounds)

    @torch.no_grad()
    def _encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        bs = config.EMBED_BATCH
        for i in range(0, len(texts), bs):
            chunk = texts[i : i + bs]
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
            out.extend(emb.float().cpu().tolist())
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            return self._encode(texts)

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        prefixed = [
            f"Instruct: {config.EMBED_QUERY_INSTRUCTION}\nQuery: {q}" for q in queries
        ]
        with self._lock:
            return self._encode(prefixed)
