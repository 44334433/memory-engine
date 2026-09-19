"""MemoryEngineProvider —— 自建记忆引擎 memory provider（阶段3切主，2026-09-16）。

对齐 Hindsight provider 能力面（参照 plugins/memory/hindsight/__init__.py + ABC
agent/memory_provider.py）：

- prefetch(query)          每轮 recall → 注入 <memory> 段（同步直查，引擎本地 pgvector 毫秒级）
                           + W1 核心记忆块：core_block=true 时追加拉取 GET /v1/core-block →
                           <core-memory> 段（宿主拉取式，同通道尾部注入；默认 false=零变化）
- queue_prefetch(query)    后台预热下一轮（recall_sync=false 时启用）
- sync_turn(u, a)          会话轮缓冲 → 每 20 轮批量 retain 到 hermes-sessions bank（writer 线程，不阻塞回复路径）
- on_pre_compress(msgs)    压缩前：摘录 retain（tags=compression-preflush）+ recall 注入摘要 prompt（≤1200 字符）
- on_session_switch(...)   flush-on-switch：旧会话缓冲落库 + 轮换 session 状态
- on_session_end(msgs)     会话结束 flush 缓冲（防丢末段）
- engine_recall/engine_retain 两个工具（tools 能力面）
- auto outcome 上报（自进化粮草通道，2026-09-19）：auto_outcome=true 时宿主侧推断
                           recall 命中被后续工具/回复引用→adopted、用户经内置 memory 工具
                           纠正/删除召回内容→corrected，POST /v1/feedback 上报；
                           默认关=存量零行为变化；同 (id,outcome) 防抖去重；useless 不推
                           （推断噪声会污染 EMA 与 L3 困难样本池，见执行文档论证）
- shutdown()               flush + writer 排空

所有网络/解析路径 fail-open：引擎不可达 → 注入空串/静默，绝不阻塞 agent。
bank 映射：会话轮→hermes-sessions；手动/工具 retain→hermes；recall=跨库（bank=null）。
配置：MEMORY_ENGINE_URL（默认 http://localhost:8766），~/.hermes/memory-engine/provider.json 可覆盖。
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, RecallStatus

__all__ = ["MemoryEngineProvider"]

logger = logging.getLogger("memory-engine")

_DEFAULT_URL = "http://localhost:8766"
_GLYPH = "🧠"


def _load_provider_config() -> dict:
    """provider.json > env > 默认。"""
    cfg: dict = {"url": _DEFAULT_URL, "auto_recall": True, "recall_sync": True,
                 "retain_every_n_turns": 20, "recall_top_k": 15,
                 "session_bank": "hermes-sessions", "manual_bank": "hermes",
                 "recall_max_chars": 2000,
                 "recall_drop_gate": 1.10,  # 落差闸：相邻分数最大落差≥此值 → 注入落差点之上整群（纯相对，跨库漂移免疫）
                 "recall_floor": 2,         # 无显著落差时兜底注入条数（防漏关键）
                 "core_block": False,       # W1 核心记忆块：默认关=存量注入行为零变化（拍板纪律#6）
                 "core_block_budget_chars": 1500,  # 引擎侧预算闸同参透传（freshness 注入闸同值）
                 "auto_outcome": False}     # 宿主自动 outcome 推断上报：默认关=零行为变化
                                           # （provider.json 置 true 持久开启；env
                                           #   MEMORY_ENGINE_AUTO_OUTCOME=1/0 即时覆盖）
    try:
        p = os.path.join(os.path.expanduser("~/.hermes/memory-engine"), "provider.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                cfg.update({k: v for k, v in json.load(f).items() if k in cfg})
    except Exception:
        pass
    cfg["url"] = str(os.environ.get("MEMORY_ENGINE_URL") or cfg["url"]).rstrip("/")
    raw = os.environ.get("MEMORY_ENGINE_AUTO_OUTCOME")
    if raw is not None and str(raw).strip() != "":
        cfg["auto_outcome"] = str(raw).strip().lower() not in ("0", "false", "no", "off")
    return cfg


class MemoryEngineProvider(MemoryProvider):
    def __init__(self) -> None:
        self._cfg = _load_provider_config()
        self._session_id = ""
        self._parent_session_id = ""
        self._turn_buffer: List[str] = []          # 每元素=一轮 user+assistant 文本
        self._turn_counter = 0
        self._buf_lock = threading.Lock()
        self._writer_queue: "queue.Queue[object]" = queue.Queue()
        self._writer_thread: Optional[threading.Thread] = None
        self._prefetch_thread: Optional[threading.Thread] = None
        self._prefetch_lock = threading.Lock()
        self._prefetch_result = ""
        self._prefetch_count = 0
        self._last_recall_returned = False
        self._last_recall_count = 0
        self._shutting_down = threading.Event()
        # auto outcome 状态（默认关，开启后才写入；全部进程内，重启丢防抖窗可接受——
        # 引擎侧 EMA 幂等收敛 + changelog 全留痕，重复上报代价极小）
        self._pending_recalls: Dict[str, dict] = {}   # memory_id -> {q,b,t,ts}
        self._outcome_sent: Dict[tuple, float] = {}   # (memory_id,outcome) -> 上次上报 ts
        self._outcome_lock = threading.Lock()
        self._last_injected_text = ""                 # 上轮 recall 注入原文（回声剔除用）

    # ---------- 基础 ----------
    @property
    def name(self) -> str:
        return "memory-engine"

    def is_available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self._cfg['url']}/v1/health", timeout=2.5) as resp:
                return json.loads(resp.read().decode()).get("status") == "ok"
        except Exception:
            return False

    def unavailable_reason(self) -> str:
        return f"memory-engine daemon 不可达（{self._cfg['url']}/v1/health）"

    def backup_paths(self) -> List[str]:
        return [os.path.expanduser("~/.hermes/memory-engine/backups")]

    # ---------- 生命周期 ----------
    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = str(session_id or "").strip()
        logger.debug("memory-engine provider init session=%s url=%s", self._session_id, self._cfg["url"])

    def _post(self, path: str, payload: dict, timeout: float = 15.0) -> Optional[dict]:
        try:
            req = urllib.request.Request(
                f"{self._cfg['url']}{path}",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            logger.debug("memory-engine POST %s failed: %s", path, e)
            return None

    def _get(self, path: str, timeout: float = 5.0) -> Optional[dict]:
        try:
            with urllib.request.urlopen(f"{self._cfg['url']}{path}", timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            logger.debug("memory-engine GET %s failed: %s", path, e)
            return None

    # ---------- W1 核心记忆块（core block）注入通道 ----------
    def _core_block_segment(self) -> str:
        """宿主拉取式（复用 freshness-protocol 注入通道模式）：每轮 prefetch 组装时
        GET /v1/core-block，text 拼进注入段（随宿主注入走 user message 尾部；
        **禁入 system prompt**——前缀缓存铁律，引擎侧绝不强推）。

        与 recall 段正交：recall=按查询的相关记忆（落差闸），core block=pinned/精选
        的常驻条目级原文（不经查询、每轮恒定）。fail-open：引擎不可达/关闭 → 空串。
        """
        if not self._cfg.get("core_block"):
            return ""
        resp = self._get(f"/v1/core-block?budget_chars={int(self._cfg['core_block_budget_chars'])}")
        text = (resp or {}).get("text") or ""
        if not text.strip():
            return ""
        return f"<core-memory engine=memory-engine>\n{text}\n</core-memory>"

    # ---------- recall / prefetch ----------
    def _gate_results(self, results: List[dict]) -> List[dict]:
        """落差闸（用户 2026-09-16 拍板 A，替代比值闸+θ 双参数）：纯结构判断。

        - 相邻分数最大落差 ≥ drop_gate → 注入落差点之上整群（相关带与噪声带天然分离）
        - 无显著落差（全平/纯噪声）→ floor 兜底，噪声不再固定满 k 注入
        - 注入条数 0..top_k 完全逐轮自适应；唯一参数为相对比值，跨库漂移免疫
        - score 解析失败：放行原序（fail-open，不因闸门丢召回）
        """
        if not results:
            return []
        try:
            ordered = sorted(results, key=lambda r: -float(r.get("score") or 0.0))
        except (TypeError, ValueError):
            return results
        if len(ordered) <= 1:
            return ordered
        s = [float(r.get("score") or 0.0) for r in ordered]
        drops = [(s[i] / s[i + 1] if s[i + 1] > 0 else 1.0, i) for i in range(len(s) - 1)]
        max_drop, cut = max(drops)
        if max_drop >= self._cfg["recall_drop_gate"]:
            return ordered[:cut + 1]
        return ordered[:max(1, int(self._cfg["recall_floor"]))]

    def _do_recall(self, query: str) -> tuple[str, int]:
        # caller="main"：与主 agent 同身份（引擎可见性闸下 main 全见；migration 行 owner≠provider）
        resp = self._post("/v1/recall", {"query": query, "bank": None,
                                         "caller": "main",
                                         "top_k": self._cfg["recall_top_k"]})
        if not resp:
            return "", 0
        results = resp.get("results") or []
        gated = self._gate_results(results)
        logger.debug("memory-engine recall gate: %d -> %d (query=%s)",
                     len(results), len(gated), query[:40])
        results = gated
        lines: List[str] = []
        used = 0
        cap = self._cfg["recall_max_chars"]
        track = bool(self._cfg.get("auto_outcome"))
        if track:
            self._last_injected_text = ""   # 本轮注入组装中；防残留旧注入参与回声剔除
        for i, r in enumerate(results, 1):
            title = (r.get("title") or "").strip()
            body = (r.get("body") or "").strip()
            meta = f"[{i}] ({r.get('bank', '?')}/{r.get('staleness', '?')}) {title}"
            room = max(60, cap - used - len(meta) - 4)
            seg = meta + "\n" + body[:room]
            lines.append(seg)
            used += len(seg) + 2
            if track:
                self._track_recall_hit(r, query)
            if used >= cap:
                break
        text = "\n\n".join(lines)
        if track:
            self._last_injected_text = text
        return text, len(results)

    def _format_recall(self, text: str) -> str:
        if not text:
            return ""
        return (f"<memory engine=memory-engine>\n{text}\n</memory>")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if session_id:
            self._session_id = str(session_id).strip()
        if not self._cfg["auto_recall"] or self._shutting_down.is_set():
            self._record_recall_indicator(returned=False, count=0)
            return self._core_block_segment()   # W1：core block 独立于 recall 开关（常驻区不经查询）
        if self._cfg["recall_sync"]:
            text, count = self._do_recall(query)
            self._record_recall_indicator(returned=bool(text), count=count)
            return "\n\n".join(x for x in (self._format_recall(text),
                                           self._core_block_segment()) if x)
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            result, count = self._prefetch_result, self._prefetch_count
            self._prefetch_result, self._prefetch_count = "", 0
        self._record_recall_indicator(returned=bool(result), count=count)
        return "\n\n".join(x for x in (self._format_recall(result),
                                       self._core_block_segment()) if x)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._cfg["recall_sync"] or not self._cfg["auto_recall"]:
            return
        if self._shutting_down.is_set():
            return

        def _run():
            text, count = self._do_recall(query)
            if text:
                with self._prefetch_lock:
                    self._prefetch_result = text
                    self._prefetch_count = count

        self._prefetch_thread = threading.Thread(
            target=contextvars.copy_context().run, args=(_run,),
            daemon=True, name="memory-engine-prefetch")
        self._prefetch_thread.start()

    def recall_status(self) -> Optional[RecallStatus]:
        if not self._last_recall_returned:
            return None
        return RecallStatus(provider_label="MemoryEngine", count=self._last_recall_count, glyph=_GLYPH)

    def _record_recall_indicator(self, *, returned: bool, count: int) -> None:
        self._last_recall_returned = returned
        self._last_recall_count = count

    # ---------- 写路径 ----------
    def _ensure_writer(self) -> None:
        if self._writer_thread and self._writer_thread.is_alive():
            return
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True,
                                               name="memory-engine-writer")
        self._writer_thread.start()

    def _writer_loop(self) -> None:
        while True:
            job = self._writer_queue.get()
            if job is None:
                return
            try:
                job()
            except Exception as e:
                logger.debug("memory-engine writer job failed: %s", e)

    def _build_turn_text(self, user_content: str, assistant_content: str) -> str:
        u = (user_content or "").strip()[:4000]
        a = (assistant_content or "").strip()[:4000]
        return f"[user] {u}\n[assistant] {a}"

    def _flush_locked(self, bank: str, tags: List[str], context_prefix: str) -> None:
        """把当前缓冲作为批量 retain 提交到 writer 队列（每轮对话=1 条 item）。"""
        items = []
        for idx, turn_text in enumerate(self._turn_buffer, 1):
            items.append({
                "content": turn_text,
                "context": f"{context_prefix} turn:{self._turn_counter - len(self._turn_buffer) + idx}",
                "tags": tags,
                "source_type": "session",
            })
        self._turn_buffer = []
        if not items:
            return
        bank_snap, sid_snap = bank, self._session_id

        def _do():
            self._post("/v1/retain", {"bank": bank_snap, "caller": "provider-session",
                                      "items": items, "dedup": True}, timeout=60)

        self._ensure_writer()
        self._writer_queue.put(_do)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "",
                  messages: Optional[List[Dict[str, Any]]] = None) -> None:
        if self._shutting_down.is_set():
            return
        if session_id:
            self._session_id = str(session_id).strip()
        if self._cfg.get("auto_outcome"):
            try:
                self._infer_adopted(user_content, assistant_content, messages)
            except Exception as e:
                logger.debug("auto-outcome adopt infer failed: %s", e)
        with self._buf_lock:
            self._turn_buffer.append(self._build_turn_text(user_content, assistant_content))
            self._turn_counter += 1
            if self._turn_counter % self._cfg["retain_every_n_turns"] != 0:
                return
            tags = ["session"] + ([f"session:{self._session_id}"] if self._session_id else [])
            self._flush_locked(self._cfg["session_bank"], tags,
                               f"session {self._session_id}")

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        try:
            with self._buf_lock:
                if not self._turn_buffer:
                    return
                tags = ["session", "session-end"] + ([f"session:{self._session_id}"] if self._session_id else [])
                self._flush_locked(self._cfg["session_bank"], tags,
                                   f"session {self._session_id} end")
        except Exception as e:
            logger.debug("on_session_end flush failed: %s", e)

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, **kwargs: Any) -> None:
        new_id = str(new_session_id or "").strip()
        if not new_id:
            return
        try:
            with self._buf_lock:
                if self._turn_buffer:
                    old_sid = self._session_id
                    tags = ["session", "flush-on-switch"] + ([f"session:{old_sid}"] if old_sid else [])
                    self._flush_locked(self._cfg["session_bank"], tags,
                                       f"session {old_sid} pre-switch")
            if self._prefetch_thread and self._prefetch_thread.is_alive():
                self._prefetch_thread.join(timeout=3.0)
            with self._prefetch_lock:
                self._prefetch_result = ""
            if parent_session_id:
                self._parent_session_id = str(parent_session_id).strip()
            self._session_id = new_id
            logger.debug("memory-engine on_session_switch new=%s parent=%s",
                         new_id, self._parent_session_id)
        except Exception as e:
            logger.debug("on_session_switch failed: %s", e)

    # ---------- 压缩 ----------
    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """压缩前：窗口摘录 retain + recall 注入摘要 prompt（失败静默返回 ""）。"""
        try:
            window = messages[-6:] if messages else []
            excerpt_parts = []
            for m in window:
                role = m.get("role", "?")
                content = str(m.get("content") or "")[:600]
                if content.strip():
                    excerpt_parts.append(f"[{role}] {content.strip()}")
            excerpt = "\n".join(excerpt_parts)[:1500]
            sid = self._session_id
            if excerpt:
                tags = ["compression-preflush"] + ([f"session:{sid}"] if sid else [])
                self._post("/v1/retain", {
                    "bank": self._cfg["session_bank"], "caller": "provider-compress",
                    "items": [{"content": excerpt,
                               "context": f"compression preflush {sid} "
                                          f"{datetime.now().strftime('%Y-%m-%d %H:%M')}",
                               "tags": tags, "source_type": "compression"}],
                    "dedup": True}, timeout=30)
            anchor = ""
            for m in reversed(messages or []):
                if m.get("role") == "user":
                    anchor = str(m.get("content") or "")[:120]
                    break
            text, count = self._do_recall(f"会话关键状态 计划 决策 待办 {anchor}")
            self._record_recall_indicator(returned=bool(text), count=count)
            return text[:1200]
        except Exception as e:
            logger.debug("on_pre_compress failed: %s", e)
            return ""

    # ---------- 工具面 ----------
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {"name": "engine_recall", "description": "语义检索记忆引擎（跨库）",
             "input_schema": {"type": "object",
                              "properties": {"query": {"type": "string"},
                                             "top_k": {"type": "integer", "default": 6}},
                              "required": ["query"]}},
            {"name": "engine_retain", "description": "写入一条长期记忆到引擎 hermes 库",
             "input_schema": {"type": "object",
                              "properties": {"content": {"type": "string"},
                                             "context": {"type": "string"},
                                             "tags": {"type": "array", "items": {"type": "string"}}},
                              "required": ["content", "context"]}},
        ]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs: Any) -> str:
        args = args or {}
        if tool_name == "engine_recall":
            top_k = args.get("top_k") or self._cfg["recall_top_k"]
            resp = self._post("/v1/recall", {"query": str(args.get("query", "")), "bank": None,
                                             "caller": "main", "top_k": max(1, min(int(top_k), 20))})
            if not resp:
                return "记忆引擎不可达"
            out = []
            for i, r in enumerate(resp.get("results") or [], 1):
                out.append(f"[{i}] ({r.get('bank', '?')}/{r.get('staleness', '?')}) "
                           f"{(r.get('title') or '')[:80]}\n{(r.get('body') or '')[:500]}")
            return "\n\n".join(out) or "（无命中）"
        if tool_name == "engine_retain":
            resp = self._post("/v1/retain", {
                "bank": self._cfg["manual_bank"], "caller": "tool",
                "items": [{"content": str(args.get("content", "")),
                           "context": str(args.get("context", "manual retain")),
                           "tags": list(args.get("tags") or []), "source_type": "manual"}],
                "dedup": True})
            if not resp:
                return "写入失败：记忆引擎不可达"
            return f"已写入 {len(resp.get('ids') or [])} 条（dedup 跳过 {resp.get('dedup_skipped', 0)}）"
        return f"未知工具: {tool_name}"

    # ---------- auto outcome（自进化粮草通道：宿主推断 → POST /v1/feedback） ----------
    # 契约真源=引擎 outcome.py/api_feedback.py：adopted → L2 access_events(kind=adopted)
    # + adopt_count+1 + EMA(+1 目标)；corrected → EMA(−1) + L3 hard_queries 落池
    # （query 取本上报携带值，缺省引擎回退该条最近 recall_hit.query）。
    # useless 同映射 −1 并落 L3 池——宿主侧「召回未被引证」推断precision 不足（注入经
    # 落差闸+模型常转述，逐字引证≠唯一使用形态），推 useless 会以假阴性烧穿 L2 护盾
    # 并往困难样本池灌噪（自进化反馈回路被假信号学习），故本批只推 adopted/corrected
    # 两态，useless 待引证检测金标 precision 达标后另批拍板。
    _AUTO_OUTCOME_CALLER = "host-auto-outcome"
    _PENDING_TTL = 600.0      # 召回条目可归因窗口（秒）
    _SENT_TTL = 86400.0       # 同 (id,outcome) 防抖去重窗（秒）
    _PENDING_CAP = 64         # 在跟踪召回条目上限（超限驱逐最旧）
    _MIN_BODY_WIN = 16        # body 匹配窗最小可用长度（去空白后）
    _MIN_TITLE_WIN = 10
    _MIN_CORR_TEXT = 16       # corrected 触发文本最小长度

    @staticmethod
    def _norm_text(s: Any) -> str:
        """匹配规整化：去全部空白 + 小写（中文场景空白噪声/换行截断免疫）。"""
        return re.sub(r"\s+", "", str(s if s is not None else "")).lower()

    def _track_recall_hit(self, r: dict, query: str) -> None:
        """_do_recall 注入成功即登记候选（只记真正进注入文本的条目，被 cap 截断的不记）。"""
        mid = str(r.get("id") or "")
        if not mid:
            return
        now = time.time()
        e = {"q": (query or "")[:200],
             "b": self._norm_text(r.get("body"))[:400],
             "t": self._norm_text(r.get("title"))[:120],
             "ts": now}
        with self._outcome_lock:
            self._pending_recalls[mid] = e
            for stale in [m for m, v in self._pending_recalls.items()
                          if now - v["ts"] > self._PENDING_TTL]:
                self._pending_recalls.pop(stale, None)
            while len(self._pending_recalls) > self._PENDING_CAP:
                oldest = min(self._pending_recalls, key=lambda m: self._pending_recalls[m]["ts"])
                self._pending_recalls.pop(oldest, None)

    def _adopt_windows(self, e: dict) -> List[str]:
        """adopted 判定窗：body 头部/中段两个 32 字窗 + title 32 字窗（宁缺毋滥）。"""
        b, t = e.get("b", ""), e.get("t", "")
        wins = set()
        if len(b) >= self._MIN_BODY_WIN:
            wins.add(b[:32])
            if len(b) >= 56:
                wins.add(b[24:56])
        if len(t) >= self._MIN_TITLE_WIN:
            wins.add(t[:32])
        return list(wins)

    def _turn_blob(self, user_content: str, assistant_content: str,
                   messages: Optional[List[dict]]) -> str:
        """本轮可归因文本 = 最终回复 + 用户消息（剔除引擎注入回声）+ 本轮工具
        调用参数与工具结果（messages 尾部、截到最后一条 user 消息为止）。"""
        parts = [assistant_content or ""]
        uc = user_content or ""
        inj = self._last_injected_text
        if inj:
            uc = uc.replace(inj, "")
        parts.append(uc)
        for m in reversed(messages or []):
            role = m.get("role")
            if role == "user":
                break
            if role == "assistant":
                c = m.get("content")
                if isinstance(c, str):
                    parts.append(c)
                for tc in (m.get("tool_calls") or []):
                    fn = (tc or {}).get("function") or {}
                    parts.append(str(fn.get("arguments") or ""))
            elif role == "tool":
                parts.append(str(m.get("content") or ""))
        return self._norm_text("\n".join(parts))

    def _infer_adopted(self, user_content: str, assistant_content: str,
                       messages: Optional[List[dict]]) -> None:
        """recall 命中被后续工具/回复逐字引用 → adopted（高精度逐字窗匹配）。"""
        now = time.time()
        with self._outcome_lock:
            pend = [(mid, dict(e)) for mid, e in self._pending_recalls.items()
                    if now - e["ts"] <= self._PENDING_TTL]
        if not pend:
            return
        blob = self._turn_blob(user_content, assistant_content, messages)
        if not blob:
            return
        for mid, e in pend:
            if any(w in blob for w in self._adopt_windows(e) if w):
                self._submit_outcome(mid, "adopted", e.get("q", ""))
                with self._outcome_lock:
                    self._pending_recalls.pop(mid, None)

    def _match_corrected(self, norm: str) -> List[str]:
        """纠正/删除文本与最近召回条目的高重叠匹配（24 字窗双向包含）。"""
        now = time.time()
        with self._outcome_lock:
            items = [(mid, dict(e)) for mid, e in self._pending_recalls.items()
                     if now - e["ts"] <= self._PENDING_TTL]
        head = norm[:24]
        hits: List[str] = []
        for mid, e in items:
            b, t = e.get("b", ""), e.get("t", "")
            wb, wt = b[:24], t[:24]
            if ((len(wb) >= self._MIN_BODY_WIN and (wb in norm or (len(head) >= 16 and head in b)))
                    or (len(wt) >= self._MIN_TITLE_WIN and (wt in norm or (len(head) >= 16 and head in t)))):
                hits.append(mid)
        return hits

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        """内置 memory 工具 remove/replace → 若命中最近召回条目 → corrected。
        replace 优先取 metadata.old_text（被纠正的旧文本才是纠错对象）。"""
        if not self._cfg.get("auto_outcome"):
            return
        if action not in ("replace", "remove"):
            return
        try:
            text = ""
            if action == "replace" and metadata:
                text = str(metadata.get("old_text") or "")
            if not text:
                text = content or ""
            norm = self._norm_text(text)
            if len(norm) < self._MIN_CORR_TEXT:
                return
            for mid in self._match_corrected(norm):
                with self._outcome_lock:
                    e = self._pending_recalls.pop(mid, None)
                self._submit_outcome(mid, "corrected", (e or {}).get("q", ""))
        except Exception as e:
            logger.debug("auto-outcome on_memory_write failed: %s", e)

    def _submit_outcome(self, memory_id: str, outcome: str, query: str = "") -> bool:
        """防抖去重（同 id+outcome 24h 窗内一次）+ writer 队列异步上报，fail-open。
        上报失败回滚防抖键，下轮匹配可重试（至多一次语义可接受，引擎端 EMA 收敛幂等）。"""
        now = time.time()
        key = (memory_id, outcome)
        with self._outcome_lock:
            last = self._outcome_sent.get(key)
            if last is not None and now - last < self._SENT_TTL:
                return False
            if len(self._outcome_sent) > 1024:
                for k in sorted(self._outcome_sent, key=lambda kk: self._outcome_sent[kk])[:256]:
                    self._outcome_sent.pop(k, None)
            self._outcome_sent[key] = now
        payload: Dict[str, Any] = {"memory_id": memory_id, "outcome": outcome,
                                   "caller": self._AUTO_OUTCOME_CALLER}
        if query:
            payload["query"] = query[:200]

        def _job():
            if self._post("/v1/feedback", payload, timeout=5.0) is None:
                with self._outcome_lock:
                    self._outcome_sent.pop(key, None)

        self._ensure_writer()
        self._writer_queue.put(_job)
        logger.info("memory-engine auto-outcome: %s -> %s", outcome, memory_id)
        return True

    # ---------- 收尾 ----------
    def shutdown(self) -> None:
        logger.debug("memory-engine shutdown: flush + drain")
        try:
            self.on_session_end([])
        except Exception:
            pass
        try:
            self._shutting_down.set()
            self._writer_queue.put(None)
            if self._writer_thread and self._writer_thread.is_alive():
                self._writer_thread.join(timeout=5.0)
        except Exception:
            pass

    # ---------- 配置面板 ----------
    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "url", "label": "Engine URL", "type": "string", "value": self._cfg["url"]},
            {"key": "auto_recall", "label": "每轮自动 recall 注入", "type": "bool", "value": self._cfg["auto_recall"]},
            {"key": "retain_every_n_turns", "label": "每 N 轮落库", "type": "int", "value": self._cfg["retain_every_n_turns"]},
            {"key": "recall_top_k", "label": "recall 条数", "type": "int", "value": self._cfg["recall_top_k"]},
            {"key": "core_block", "label": "W1 核心记忆块常驻注入（宿主拉取式）", "type": "bool", "value": self._cfg["core_block"]},
            {"key": "auto_outcome", "label": "宿主自动 outcome 推断上报（adopted/corrected，默认关）", "type": "bool", "value": self._cfg["auto_outcome"]},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        cfg_dir = os.path.join(hermes_home, "memory-engine")
        os.makedirs(cfg_dir, exist_ok=True)
        path = os.path.join(cfg_dir, "provider.json")
        current: dict = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    current = json.load(f)
            except Exception:
                current = {}
        current.update({k: v for k, v in values.items() if k in self._cfg})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=1)
        self._cfg = _load_provider_config()
