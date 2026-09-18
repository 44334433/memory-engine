-- 005_memory_type.sql: W2 记忆类型分层（2026-09-18）
-- 动机：按 semantic/procedural/episodic 三类差异化检索过滤与衰减窗口
--（TYPE_DECAY_FACTORS：semantic ×2 最慢 / procedural ×1.5 / episodic ×1 基准）。
-- 兼容性铁律：NOT NULL DEFAULT 'episodic'——PG11+ 常量默认值 ADD COLUMN 为
-- 元数据级操作，不重写表、不丢任何既有列；旧版运行代码显式列 INSERT/SELECT
-- 不受影响（新列走 DEFAULT）。回填打标见 scripts/backfill_memory_type.py（独立闸）。
ALTER TABLE memories ADD COLUMN IF NOT EXISTS memory_type text NOT NULL DEFAULT 'episodic';
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_memory_type_check;
ALTER TABLE memories ADD CONSTRAINT memories_memory_type_check
  CHECK (memory_type IN ('semantic', 'procedural', 'episodic'));
-- 类型过滤索引（recall filters.memory_type / GET /v1/memories?memory_type=）
CREATE INDEX IF NOT EXISTS idx_mem_type ON memories (memory_type);
