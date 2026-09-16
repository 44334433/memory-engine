# 贡献指南（CONTRIBUTING）

## 开发环境

1. Python ≥ 3.12；数据库为 PostgreSQL 18 + pgvector + PGroonga（+ pg_trgm / pg_prewarm）：
   - 快速方式：`docker compose up -d db`（自动建扩展并应用 `schema.sql`）
   - 裸机方式：`sudo bash deploy/pg/stage1_install.sh`（Ubuntu noble，PGDG+PGroonga 源）
2. 嵌入模型：Qwen/Qwen3-Embedding-0.6B 权重不随仓库分发，自行下载到
   `MEMORY_ENGINE_MODEL_DIR`（默认 `<数据目录>/models/qwen3-embedding-0.6b`）。
3. 配置一律走环境变量，样例见 `.env.example`（复制为 `.env` 按需修改）。

## 启动引擎

```bash
deploy/memory-engine.sh serve          # 或：PYTHONPATH=src python3 -m memory_engine.cli serve
curl -s http://127.0.0.1:8766/v1/health
```

## 运行测试

```bash
pytest tests/ -v
```

- 纯单元测试（`tests/test_unit_pure.py`）离线可跑；
- 集成套件（smoke / stage2）需要引擎 daemon + 数据库在跑，daemon 不可达时自动 skip，
  不会伪绿。手动直跑：`python3 tests/smoke_test.py`、`python3 tests/stage2_test.py`。

## 提交纪律

- 消费方全走 HTTP API，禁止 import 旁路（`tests/` 同样遵守，仅末尾只读对账可直查）；
- 任何非平凡改动先跑 `pytest`，集成测试 skip 数应为预期值；
- 数据文件（`*.jsonl` / `*.dump` / 模型权重 / 备份）绝不提交，`.gitignore` 已兜底；
- 改动面向「模式」而非个案：先查同类调用面再动手，保持手术式最小 diff。
