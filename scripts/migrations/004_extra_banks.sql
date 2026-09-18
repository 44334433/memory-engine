-- 004_extra_banks.sql: 评测/隔离专用 bank 支持（MEMORY_ENGINE_EXTRA_BANKS env 与本约束保持同步）
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_bank_check;
ALTER TABLE memories ADD CONSTRAINT memories_bank_check
  CHECK (bank IN ('hermes', 'hermes-sessions', 'knowledge', 'reflection', 'eval_decay'));
