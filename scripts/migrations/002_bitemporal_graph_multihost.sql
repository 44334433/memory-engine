-- 002：双时序 + 知识网络 + 多宿主留位（P1 第二批 2026-09-16）
-- 幂等：可重复执行；同键重复执行无副作用（ADD COLUMN IF NOT EXISTS / CREATE ... IF NOT EXISTS / UPDATE WHERE NULL）
-- 内容：
--   1) memories 双时序列：valid_at（事件时间，NOT NULL，默认=created_at 同瞬 now()）/
--      invalid_at（失效时刻，NULL=现行）/ is_current 生成列（STORED，invalid_at IS NULL）+
--      部分索引 idx_mem_current（WHERE is_current）
--   2) 多宿主留位：tenant_id/agent_id text 可空（默认 NULL=单宿主不分区，不传不受影响）+ 部分索引
--   3) 知识网络：entities + edges（related/causal/parent_child/contradicts 四类边）
--      G15（S1 级盲审硬约束）：矛盾检测 observe-only——contradicts 边只记录，
--      绝不触发 memories.invalid_at 置位；升 enforce 前置 = 金标边集 precision>=0.7
--      且 30 天抽检通过（未达前置前，任何代码路径不得由 contradicts 边改写 memories）。
--   4) uq_mem_dedup 判重唯一索引收紧为「仅现行版本参与判重」（AND is_current）——
--      supersede 新旧两版本共享 dedup_key，历史版本不再阻塞新版本写入

-- —— 1) 双时序 ——
ALTER TABLE memories ADD COLUMN IF NOT EXISTS valid_at timestamptz;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS invalid_at timestamptz;

-- 回填：存量行事件时间缺省=created_at；仅填 NULL 行（重跑零副作用）
UPDATE memories SET valid_at = created_at WHERE valid_at IS NULL;

ALTER TABLE memories ALTER COLUMN valid_at SET DEFAULT now();
ALTER TABLE memories ALTER COLUMN valid_at SET NOT NULL;

-- is_current 生成列：invalid_at IS NULL（STORED，随 invalid_at 写入自动维护）
ALTER TABLE memories ADD COLUMN IF NOT EXISTS is_current boolean
  GENERATED ALWAYS AS (invalid_at IS NULL) STORED;

CREATE INDEX IF NOT EXISTS idx_mem_current ON memories (valid_at DESC) WHERE is_current;

-- —— 2) 多宿主留位（可空列 + 非空部分索引：单宿主全 NULL 时不占索引空间） ——
ALTER TABLE memories ADD COLUMN IF NOT EXISTS tenant_id text;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS agent_id text;
CREATE INDEX IF NOT EXISTS idx_mem_tenant ON memories (tenant_id) WHERE tenant_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mem_agent  ON memories (agent_id)  WHERE agent_id IS NOT NULL;

-- —— 3) 知识网络 ——
CREATE TABLE IF NOT EXISTS entities (
  id         uuid PRIMARY KEY,
  name       text NOT NULL,
  etype      text NOT NULL CHECK (etype IN ('person','org','project','concept','tool','place','event','other')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_name ON entities (etype, lower(name));

CREATE TABLE IF NOT EXISTS edges (
  id         uuid PRIMARY KEY,
  src_mid    uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,  -- purge 硬删级联清边
  dst_mid    uuid REFERENCES memories(id) ON DELETE CASCADE,           -- 记忆↔记忆边
  entity_id  uuid REFERENCES entities(id) ON DELETE CASCADE,           -- 记忆↔实体边（与 dst_mid 二选一）
  etype      text NOT NULL CHECK (etype IN ('related','causal','parent_child','contradicts')),
  valid_at   timestamptz NOT NULL DEFAULT now(),
  invalid_at timestamptz,                    -- NULL=现行边（双时序边，P2 阶段矛盾自动失效用）
  source     text NOT NULL,                  -- weak_graph:<dim> | llm_extract | manual | supersede
  CHECK (dst_mid IS NOT NULL OR entity_id IS NOT NULL),   -- 边必须有指向
  CHECK (dst_mid IS NULL OR dst_mid <> src_mid)           -- 禁记忆自环
);
CREATE INDEX IF NOT EXISTS idx_edges_src    ON edges (src_mid)    WHERE invalid_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_dst    ON edges (dst_mid)    WHERE invalid_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_entity ON edges (entity_id)  WHERE invalid_at IS NULL;
-- 现行三元组唯一去重（失效边不占键，同三元组可再建新边）
CREATE UNIQUE INDEX IF NOT EXISTS uq_edge_mem ON edges (src_mid, dst_mid, etype)
  WHERE dst_mid IS NOT NULL AND invalid_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_edge_ent ON edges (src_mid, entity_id, etype)
  WHERE entity_id IS NOT NULL AND invalid_at IS NULL;

-- —— 4) 判重唯一索引收紧：仅现行版本参与判重（supersede 新旧版本共享 dedup_key） ——
DROP INDEX IF EXISTS uq_mem_dedup;
CREATE UNIQUE INDEX IF NOT EXISTS uq_mem_dedup ON memories (bank, dedup_key)
  WHERE dedup_key IS NOT NULL AND ttl_state <> 'retired' AND is_current;

-- schema_ver 登记 1→2
INSERT INTO engine_meta(key, value) VALUES ('schema_ver', '2'::jsonb)
ON CONFLICT (key) DO UPDATE SET value = '2'::jsonb;
