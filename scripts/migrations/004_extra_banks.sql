-- 004_extra_banks.sql: 评测/隔离专用 bank 支持（MEMORY_ENGINE_EXTRA_BANKS env 与本约束保持同步）
-- 动机：评测流水线（decay_curve）需要独立 bank（eval_decay）避免污染生产检索空间；
-- 多租户/隔离场景也需要动态 bank。应用层已走 config.BANKS（env 可扩展），此迁移对齐 DB 层。
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_bank_check;
ALTER TABLE memories ADD CONSTRAINT memories_bank_check
  CHECK (bank IN ('hermes', 'hermes-sessions', 'knowledge', 'reflection', 'hermes-docs', 'eval_decay'));
