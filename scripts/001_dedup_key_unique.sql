-- 001：判重 UNIQUE 兜底（P1 第一批 2026-09-16）——首个手写迁移脚本（P2#9 迁移机制化素材）
-- 幂等：可重复执行；同键重复执行无副作用。
-- 内容：
--   1) memories 加 dedup_key text 可空列（=sha256(body + US + context)，util.dedup_hash 同构）
--   2) 存量回填 sha256(body + US)：context 未落库，用「空 context」键——新写入必带非空 context，永不与回填键冲突
--      同 (bank, 回填键) 重复组仅保留最旧行（seq 最小）参与唯一约束，其余置 NULL（不阻断索引创建）
--   3) 部分唯一索引 uq_mem_dedup(bank, dedup_key) WHERE dedup_key IS NOT NULL AND ttl_state <> 'retired'
--      排除 retired：软删后允许同内容重建；并发竞态撞索引→应用层 catch UniqueViolation 返回既有条目

ALTER TABLE memories ADD COLUMN IF NOT EXISTS dedup_key text;

WITH cand AS (
  SELECT id,
         encode(sha256(convert_to(body || chr(31), 'UTF8')), 'hex') AS dk,
         row_number() OVER (
           PARTITION BY bank, encode(sha256(convert_to(body || chr(31), 'UTF8')), 'hex')
           ORDER BY seq) AS rn
  FROM memories
  WHERE dedup_key IS NULL
)
UPDATE memories m
SET dedup_key = c.dk
FROM cand c
WHERE m.id = c.id AND c.rn = 1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mem_dedup ON memories (bank, dedup_key)
  WHERE dedup_key IS NOT NULL AND ttl_state <> 'retired';
