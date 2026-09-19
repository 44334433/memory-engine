"""W3 可插拔重排：Qwen3-Reranker-0.6B 二段精排（2026-09-19，设计-外部反馈裁决全景 W3 项）。

Why：0.122 批评根因=四路 RRF 融合下异构记忆「竞争」而非「甄别」——cross-encoder 对
query×候选联合编码逐条打相关性分，把「谁挤进 top-k」变成「谁真的答上了查询」。

契约（与 embedder.py 同构，P1 降级语义）：
- load() 幂等可重试；失败允许抛异常（app 层降级启动 + self-heal 拉回，daemon 不挂）
- score() 失败直接抛——recall 层按「rerank 路失败→保留 RRF 顺序 + degraded+failed_routes.rerank
  显式降级」处理（增强路不是主路，永不参与 503 判定，与 graph 路同语义），禁在本层吞错
- 设备选择=SOUL#25③ 总量预算闸：auto 模式先 torch.cuda.mem_get_info，free ≥ RERANK_GPU_MIN_FREE_MIB
  才上卡（现有 embedder 1.14GB 常驻已体现在 free 真实余量里），不足退 CPU——禁擦线分配（实证：11.5/12.2 双 OOM）
- 打分=官方模型卡实现（README「Using Transformers」节，逐字核对 2026-09-19）：
  causal-LM 末 token 的 yes/no 双 logit log_softmax → P(yes) ∈ [0,1]
- 线程锁串行化 GPU 调用（与 embedder 同因：FastAPI 同步端点跑线程池）
"""
import logging
import threading

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from . import config
from .embedder import _guard_torchaudio

log = logging.getLogger("memory-engine.reranker")

_guard_torchaudio()   # Qwen3 模型链 import torchaudio（本机 cu130/cu132 不匹配，进程内 stub 绕过——embedder 同款）

# 官方模板（模型卡 README）：system 判定指令 + <Instruct>/<Query>/<Document> 三段 + assistant 前缀
_OFFICIAL_PREFIX = ('<|im_start|>system\nJudge whether the Document meets the requirements based on '
                    'the Query and the Instruct provided. Note that the answer can only be "yes" or '
                    '"no".<|im_end|>\n<|im_start|>user\n')
_OFFICIAL_SUFFIX = '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'


class Qwen3Reranker:
    """Qwen3-Reranker-0.6B fp16 常驻进程内 cross-encoder（开关在 config.RERANK_ENABLED，缺省关）。"""

    name = "qwen3-reranker"

    def __init__(self) -> None:
        self.model = None
        self.tokenizer = None
        self.device: str | None = None      # load() 时按预算闸定档（auto）；load 前为 None
        self._lock = threading.Lock()
        self._ids: tuple[int, int] | None = None    # (no_id, yes_id)
        self._prefix_tokens: list[int] = []
        self._suffix_tokens: list[int] = []

    @property
    def loaded(self) -> bool:
        return self.model is not None

    # —— 设备：GPU 总量预算闸（SOUL#25③） ——
    def _resolve_device(self) -> str:
        want = (config.RERANK_DEVICE or "auto").strip().lower()
        if want in ("cuda", "cpu"):
            return want                     # 显式指定直从（评测隔离实例常钉 cpu）
        if not torch.cuda.is_available():
            log.info("rerank device=auto → cpu（无 CUDA）")
            return "cpu"
        try:
            free_b, _total = torch.cuda.mem_get_info()
        except Exception as e:  # noqa: BLE001 —— 查询失败宁保守走 CPU，不冒 OOM 险
            log.warning("rerank mem_get_info failed → cpu: %s", e)
            return "cpu"
        need = config.RERANK_GPU_MIN_FREE_MIB * 1024 * 1024
        if free_b >= need:
            log.info("rerank device=auto → cuda（free=%.0fMiB ≥ need=%dMiB，#25③ 总量闸）",
                     free_b / 1048576, config.RERANK_GPU_MIN_FREE_MIB)
            return "cuda"
        log.warning("rerank device=auto → cpu（free=%.0fMiB < need=%dMiB，GPU 总量预算闸拦截，禁擦线）",
                    free_b / 1048576, config.RERANK_GPU_MIN_FREE_MIB)
        return "cpu"

    def load(self) -> None:
        """幂等可重试；失败上抛（app 层降级启动 + self-heal，与 embedder 同语义）。"""
        if self.loaded:
            return
        device = self._resolve_device()
        tok = AutoTokenizer.from_pretrained(str(config.RERANK_MODEL_DIR), padding_side="left")
        try:  # transformers 5.x 用 dtype，4.x 用 torch_dtype（embedder 同款兼容）
            model = AutoModelForCausalLM.from_pretrained(str(config.RERANK_MODEL_DIR), dtype=torch.float16)
        except TypeError:
            model = AutoModelForCausalLM.from_pretrained(str(config.RERANK_MODEL_DIR), torch_dtype=torch.float16)
        model = model.to(device).eval()
        no_id = tok.convert_tokens_to_ids("no")
        yes_id = tok.convert_tokens_to_ids("yes")
        if no_id is None or yes_id is None or no_id == yes_id:
            raise RuntimeError(f"reranker yes/no token ids 异常: no={no_id} yes={yes_id}")
        self._prefix_tokens = tok.encode(_OFFICIAL_PREFIX, add_special_tokens=False)
        self._suffix_tokens = tok.encode(_OFFICIAL_SUFFIX, add_special_tokens=False)
        self._ids = (no_id, yes_id)
        self.tokenizer, self.model, self.device = tok, model, device
        log.info("reranker loaded: %s -> %s", config.RERANK_MODEL_DIR, device)

    def warmup(self) -> None:
        """dummy 打分：触发 kernel 编译与显存池预分配（蓝图 §8.2 同因）。"""
        self.score("warmup 预热查询", ["warmup document 预热文档。"])
        log.info("reranker warmup done")

    def _format_pair(self, query: str, doc: str) -> str:
        return (f"<Instruct>: {config.RERANK_INSTRUCTION}\n"
                f"<Query>: {query}\n<Document>: {doc}")

    @torch.no_grad()
    def _score_batch(self, pairs: list[str]) -> list[float]:
        assert self._ids is not None
        no_id, yes_id = self._ids
        budget = config.RERANK_MAX_LEN - len(self._prefix_tokens) - len(self._suffix_tokens)
        enc = self.tokenizer(
            pairs, padding=False, truncation="longest_first",
            return_attention_mask=False, max_length=budget)
        enc["input_ids"] = [self._prefix_tokens + ids + self._suffix_tokens for ids in enc["input_ids"]]
        inputs = self.tokenizer.pad(enc, padding="longest", return_tensors="pt")
        # 注：不传 max_length——pad(padding=True, max_length=N) 会 pad 到全局 N(1024) 而非
        # 批内最长（transformers 实测警告），lm_head(B×L×151936) 随之爆显存/爆延迟（09-19 A/B 教训）。
        # 截断已由上方 per-pair budget 完成，这里只做批内 padding。
        inputs = {k: v.to(self.model.device) for k, v in inputs.items()}
        # logits_to_keep=1：只算末位 logits（左 padding 下末位=句尾），省掉 B×(L−1)×词表 的
        # lm_head 计算（09-19 实测 1185→930ms / B×L=20×512；老 transformers 不支持则回退全位）。
        # 显存关键步：全词表 logits（B×151936）先按 fp16 切出 yes/no 两列再升 fp32——
        # 若先 .float() 整表（batch8 → 8×151936×4B≈4.9GiB）单算子即打爆 12G 卡（2026-09-19 A/B 实测教训）。
        try:
            logits = self.model(**inputs, logits_to_keep=1).logits[:, -1, :]
        except TypeError:
            logits = self.model(**inputs).logits[:, -1, :]    # left padding → 末位置即句末
        pair = torch.stack([logits[:, no_id], logits[:, yes_id]], dim=1).float()
        pair = torch.nn.functional.log_softmax(pair, dim=1)
        return pair[:, 1].exp().cpu().tolist()                 # P(yes) ∈ [0,1]

    def score(self, query: str, documents: list[str]) -> list[float]:
        """query 对候选逐条打相关性分。返回 P(yes) 列表，与 documents 等长同序。

        失败直接抛（未加载/推理故障均上抛）——recall 层登记 failed_routes.rerank 显式降级，禁静默。
        打包=token 预算制（#23）：批大小按批内最长 L 反推，使 B×L ≤ RERANK_BATCH_TOKEN_BUDGET——
        本机 12G 卡 forward 吞吐实证 ~1.0-1.2 tok/ms（10×~500tok=607ms / 10×320cap=303ms），
        固定 B=4 会让短文档批白付 padding、长文档批爆延迟；预算制下长短自动适配。
        """
        if not self.loaded:
            raise RuntimeError("reranker not loaded（加载失败待 self-heal，或从未 load）")
        if not documents:
            return []
        out: list[float] = []
        budget = max(256, config.RERANK_BATCH_TOKEN_BUDGET)
        # 预算=token 数（前缀/后缀固定部分并入单条估算）
        lens = [len(self._prefix_tokens) + len(self.tokenizer.encode(d)) + len(self._suffix_tokens)
                for d in documents]
        i, m = 0, len(documents)
        with self._lock:                                     # GPU 调用串行（与 embedder 同因）
            while i < m:
                # 贪心取尽量多候选，使 数量×批内最长 不超预算（至少 1 条，超长条单批直处理）
                n = 1
                while i + n < m and n < config.RERANK_BATCH:
                    ln = max(lens[i:i + n + 1])
                    if ln > budget or ln * (n + 1) > budget:
                        break
                    n += 1
                chunk = [self._format_pair(query, d) for d in documents[i:i + n]]
                out.extend(self._score_batch(chunk))
                i += n
        return out


def build_reranker() -> Qwen3Reranker | None:
    """开关工厂：RERANK_ENABLED=关时返回 None（recall 收 None=完全跳过重排段，零开销）。"""
    return Qwen3Reranker() if config.RERANK_ENABLED else None
