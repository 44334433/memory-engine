# eval/ — 评测方法与脚本

评测在内部私有数据集（36 queries）上执行，数据集因含真实生产内容不公开；
本目录提供评测方法与脚本，供复现者用自有语料按同构方法构造评测集。

- `build_eval.py` — 评测集构建：从旧版记忆库 L1 语料按「当时需要记忆X」场景抽题
  （每场景 1 条 gold、场景多样性去重、会话在档交叉验证），产出
  `memory_recall_eval.jsonl`（queries + gold 对齐键）。数据集不随仓库分发。
- `run_compare.py` — 双引擎对照：同题集分测本引擎 vs 旧版基线服务。
  指标：P@5 / MRR@10；主口径=exact gold，次口径=同场景兄弟记录（更贴近
  「当时需要记忆X」的真实相关性）。产出 `compare_result.json`（亦不入库）。
- 切主验收闸：`engine_P@5 ≥ baseline_P@5` 且 MRR@10 差距 ≥ -0.05 → PASS。

## 运行

```bash
# 引擎 daemon 在跑（默认 127.0.0.1:8766）后：
MEMORY_EVAL_LEGACY_DB=/path/to/legacy_vectors.db \
LLM_GATEWAY_API_KEY=*** \
python3 eval/run_compare.py
```

配置项见根目录 `.env.example`（`MEMORY_EVAL_*` / `LLM_GATEWAY_API_KEY`）。
