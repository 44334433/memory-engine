-- memory-engine DDL v2（蓝图 §3.1 + 2026-09-16 先知拍板追加：original_date + staleness 时效字段）
-- 库：memengine @ 127.0.0.1:5433（专用实例 cluster=memengine）
-- 扩展由 postgres superuser 预建：vector / pgroonga / pg_trgm / pg_prewarm
-- schema_ver = 2（v2 = P1 第二批：双时序 + 知识网络 entities/edges + 多宿主留位，见文件尾）

CREATE TABLE IF NOT EXISTS memories (
  id            uuid PRIMARY KEY,                  -- UUIDv7（应用侧生成，时间有序）
  seq           bigint GENERATED ALWAYS AS IDENTITY UNIQUE,  -- 全局单调游标（changelog 水位对齐）
  schema_ver    int  NOT NULL DEFAULT 1,
  bank          text NOT NULL CHECK (bank IN ('hermes','hermes-sessions','knowledge','reflection')),
  domain        text NOT NULL DEFAULT 'general',
  trigger_term  text,                              -- 用户字段「trigger」；PG 保留字→列名 trigger_term
  title         text NOT NULL,
  body          text NOT NULL,                     -- 正文真源（大体积外置走 body_ptr）
  body_seg      text,                              -- 兼容占位：PGroonga 内建分词后可空置（蓝图 §22.5）
  body_ptr      text,                              -- file:// 或 obsidian:// 知识卡锚点（蓝图 §12，只读）
  tags          jsonb NOT NULL DEFAULT '[]',
  owner         text NOT NULL DEFAULT 'main',      -- main|subagent:<id>|cron:<job_id>|sumeru|user
  visibility    text NOT NULL DEFAULT 'agent'
                CHECK (visibility IN ('public','agent','private')),
  source_type   text NOT NULL,                     -- conversation|cron|doc|manual|migration
  memory_type   text NOT NULL DEFAULT 'episodic'   -- W2 分层：semantic|procedural|episodic
                CHECK (memory_type IN ('semantic','procedural','episodic')),    --（迁移 005 对齐）
  source_ref    text,                              -- 上游系统 document_id(迁移保留)/会话ID/路径
  priority      int  NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
  ttl_state     text NOT NULL DEFAULT 'candidate'
                CHECK (ttl_state IN ('candidate','trial','active','decaying','archived','retired')),
  ttl_expires_at timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  last_accessed_at timestamptz,
  access_count  bigint NOT NULL DEFAULT 0,
  adopt_count   bigint NOT NULL DEFAULT 0,
  last_verified timestamptz,
  verify_status text NOT NULL DEFAULT 'unverified'
                CHECK (verify_status IN ('verified','stale','unverified')),
  load_hint     text,
  -- —— 时效字段（2026-09-16 拍板追加，阶段3 迁移按源时间戳回填）——
  original_date timestamptz,                       -- 源条目创建时间（迁移=源系统 created_at）
  staleness     text NOT NULL DEFAULT 'fresh'
                CHECK (staleness IN ('fresh','aging','stale'))  -- fresh ≤30d / aging 30-90d / stale >90d
                ,                                  -- 降权：fresh×1.0 / aging×0.9 / stale×0.7
  embed_model   text NOT NULL,                     -- 'Qwen/Qwen3-Embedding-0.6B'
  embed_dim     int  NOT NULL,                     -- 1024
  embed_ver     int  NOT NULL DEFAULT 1,           -- 重嵌批次版本（换模唯一豁免通道）
  content_hash  text NOT NULL,                     -- sha256(bank + body) 精确判重
  search_text   text GENERATED ALWAYS AS (title || ' ' || body) STORED,  -- FTS 索引列
  embedding     vector(1024),                      -- pgvector 列
  -- —— 双时序（P1 第二批 2026-09-16；迁移 002 对存量库做同款 ALTER）——
  valid_at      timestamptz NOT NULL DEFAULT now(),  -- 事件时间（默认=created_at 同瞬 now()）
  invalid_at    timestamptz,                         -- 失效时刻（NULL=现行；矛盾更新=时间截断置位）
  is_current    boolean GENERATED ALWAYS AS (invalid_at IS NULL) STORED,
  -- —— 多宿主留位（P1 第二批：可空列，默认 NULL=单宿主不分区）——
  tenant_id     text,
  agent_id      text
);

CREATE INDEX IF NOT EXISTS idx_mem_bank_state ON memories (bank, ttl_state);
CREATE INDEX IF NOT EXISTS idx_mem_owner_vis  ON memories (owner, visibility);
CREATE INDEX IF NOT EXISTS idx_mem_updated    ON memories (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_mem_hash       ON memories (content_hash);
CREATE INDEX IF NOT EXISTS idx_mem_tags       ON memories USING gin (tags jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_mem_stale      ON memories (staleness);
CREATE INDEX IF NOT EXISTS idx_mem_type       ON memories (memory_type);  -- W2 分层：类型过滤
-- 双时序：现行版本部分索引（P1 第二批）
CREATE INDEX IF NOT EXISTS idx_mem_current    ON memories (valid_at DESC) WHERE is_current;
-- 多宿主留位：非空才入索引（单宿主全 NULL 不占空间）
CREATE INDEX IF NOT EXISTS idx_mem_tenant     ON memories (tenant_id) WHERE tenant_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mem_agent      ON memories (agent_id)  WHERE agent_id IS NOT NULL;

-- 向量路：HNSW 按 bank 部分索引（=sqlite-vec 分区语义的 PG 等价物）
CREATE INDEX IF NOT EXISTS idx_vec_knowledge ON memories USING hnsw (embedding vector_cosine_ops)
  WHERE bank = 'knowledge';
CREATE INDEX IF NOT EXISTS idx_vec_hermes    ON memories USING hnsw (embedding vector_cosine_ops)
  WHERE bank = 'hermes';
CREATE INDEX IF NOT EXISTS idx_vec_sessions  ON memories USING hnsw (embedding vector_cosine_ops)
  WHERE bank = 'hermes-sessions';
CREATE INDEX IF NOT EXISTS idx_vec_reflection ON memories USING hnsw (embedding vector_cosine_ops)
  WHERE bank = 'reflection';

-- 全文路：PGroonga 内建 CJK 分词（默认 TokenBigram，CJK bigram；tokenizer 定档留待评测集判优，蓝图 §22.5）
CREATE INDEX IF NOT EXISTS idx_mem_fts ON memories
  USING pgroonga (search_text pgroonga_text_full_text_search_ops_v2);

CREATE TABLE IF NOT EXISTS changelog (
  seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  op text NOT NULL,                  -- retain|update|lifecycle|delete|consolidate|reembed
  memory_id uuid, detail jsonb
);
CREATE TABLE IF NOT EXISTS access_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  memory_id uuid NOT NULL, ts timestamptz NOT NULL DEFAULT now(),
  kind text NOT NULL,                -- recall_hit|adopted|injected
  caller text, query text
);
CREATE INDEX IF NOT EXISTS idx_ae_mid ON access_events (memory_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ae_kind_ts ON access_events (kind, ts DESC);
CREATE TABLE IF NOT EXISTS engine_meta (key text PRIMARY KEY, value jsonb NOT NULL);

-- engine_meta 种子（幂等）
INSERT INTO engine_meta(key, value) VALUES
  ('schema_ver', '2'::jsonb),
  ('embed_model', '"Qwen/Qwen3-Embedding-0.6B"'::jsonb),
  ('embed_dim', '1024'::jsonb),
  ('embed_ver', '1'::jsonb),
  ('mode', '"normal"'::jsonb)
ON CONFLICT (key) DO NOTHING;

-- —— 投毒闸（2026-09-16 P0 批：错误语义+投毒闸）——
-- source_tier 四级来源 user/agent/web/cron，缺省=agent（存量行按不信任缺省回填）；
-- contains_pii 预留字段（本批只入库不判级，判级逻辑待 PII 闸拍板）。
ALTER TABLE memories ADD COLUMN IF NOT EXISTS source_tier text NOT NULL DEFAULT 'agent';
ALTER TABLE memories ADD COLUMN IF NOT EXISTS contains_pii boolean;

-- —— 判重 UNIQUE 兜底（2026-09-16 P1 第一批）——
-- dedup_key = sha256(body + US(0x1f) + context)（应用层 util.dedup_hash 同构）；
-- 存量回填=sha256(body + US)（context 未落库；新写入必带非空 context，永不与回填键冲突）；
-- 部分唯一索引排除 retired（软删后允许同内容重建）；并发竞态撞索引→应用层返回既有条目。
-- P1 第二批收紧：仅现行版本参与判重（AND is_current）——supersede 新旧版本共享 dedup_key。
-- 存量迁移走 scripts/migrations/001_dedup_key_unique.sql（幂等，可重复执行）。
ALTER TABLE memories ADD COLUMN IF NOT EXISTS dedup_key text;
CREATE UNIQUE INDEX IF NOT EXISTS uq_mem_dedup ON memories (bank, dedup_key)
  WHERE dedup_key IS NOT NULL AND ttl_state <> 'retired' AND is_current;

-- —— 双时序 + 知识网络 + 多宿主（2026-09-16 P1 第二批；存量库迁移走 scripts/migrations/002）——
-- G15（S1 级盲审硬约束）：矛盾检测 observe-only——contradicts 边只记录，
-- 绝不触发 memories.invalid_at 置位；升 enforce 前置=金标边集 precision>=0.7 且 30 天抽检通过。
CREATE TABLE IF NOT EXISTS entities (
  id         uuid PRIMARY KEY,
  name       text NOT NULL,
  etype      text NOT NULL CHECK (etype IN ('person','org','project','concept','tool','place','event','other')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_name ON entities (etype, lower(name));

CREATE TABLE IF NOT EXISTS edges (
  id         uuid PRIMARY KEY,
  src_mid    uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  dst_mid    uuid REFERENCES memories(id) ON DELETE CASCADE,           -- 记忆↔记忆边
  entity_id  uuid REFERENCES entities(id) ON DELETE CASCADE,           -- 记忆↔实体边（与 dst_mid 二选一）
  etype      text NOT NULL CHECK (etype IN ('related','causal','parent_child','contradicts')),
  valid_at   timestamptz NOT NULL DEFAULT now(),
  invalid_at timestamptz,                    -- NULL=现行边（双时序边）
  source     text NOT NULL,                  -- weak_graph:<dim> | llm_extract | manual | supersede
  CHECK (dst_mid IS NOT NULL OR entity_id IS NOT NULL),
  CHECK (dst_mid IS NULL OR dst_mid <> src_mid)
);
CREATE INDEX IF NOT EXISTS idx_edges_src    ON edges (src_mid)    WHERE invalid_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_dst    ON edges (dst_mid)    WHERE invalid_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_entity ON edges (entity_id)  WHERE invalid_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_edge_mem ON edges (src_mid, dst_mid, etype)
  WHERE dst_mid IS NOT NULL AND invalid_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_edge_ent ON edges (src_mid, entity_id, etype)
  WHERE entity_id IS NOT NULL AND invalid_at IS NULL;
