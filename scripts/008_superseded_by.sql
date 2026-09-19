-- 008_superseded_by.sql: 取代链谱系列 + changelog 回填（图谱深度批 2026-09-19，backlog supersede-chain-ashof ①）
-- memories.superseded_by：supersede 后继版本指针（NULL=链上最新版/未被取代）。
--   此前谱系只活在 changelog(op='supersede') 账本里，链查询每次都要现翻 JSONB——
--   升格为一等列后，取代链多跳走 WITH RECURSIVE 沿指针直走（GET /v1/memories/{id}/chain）。
-- 设计取舍：
--   * FK 自引用 ON DELETE SET NULL：TTL purge / DELETE?purge=true 会硬删旧行（api_memories.delete_memory），
--     SET NULL 把断链点显式保留为 NULL，绝不因 FK 拒绝删除；链断由 chain 端点 truncated 旗标显式承认，
--     不做假缝合。
--   * 部分索引 WHERE superseded_by IS NOT NULL：反查「谁指向我」=链回放回溯入口；谱系行占比极低（稀疏）。
--   * 回填真源=changelog（账本为真源、列是缓存——memory-engine-ops 铁律）；目标行已被 purge 的链尾跳过，
--     重跑幂等（仅补 superseded_by IS NULL 的行）。
ALTER TABLE memories ADD COLUMN IF NOT EXISTS superseded_by uuid
  REFERENCES memories(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_mem_superseded_by ON memories (superseded_by)
  WHERE superseded_by IS NOT NULL;

-- 回填：每个旧 id 取最后一条 supersede 账（同旧条只会被取代一次，DISTINCT ON 纯防御）
UPDATE memories m SET superseded_by = s.new_id
FROM (
  SELECT DISTINCT ON (c.memory_id) c.memory_id AS old_id,
         (c.detail->>'new_id')::uuid AS new_id
  FROM changelog c
  WHERE c.op = 'supersede' AND c.detail ? 'new_id'
    AND (c.detail->>'new_id') ~* '^[0-9a-f-]{36}$'
  ORDER BY c.memory_id, c.seq DESC
) s
WHERE m.id = s.old_id AND m.superseded_by IS NULL
  AND EXISTS (SELECT 1 FROM memories n WHERE n.id = s.new_id);

-- 运维对账（人工，只读）：失效行应有谱系出边（changelog 缺账/目标被 purge 的除外）：
--   SELECT count(*) FROM memories WHERE invalid_at IS NOT NULL AND superseded_by IS NULL;
-- 回滚（成对）：
--   DROP INDEX IF EXISTS idx_mem_superseded_by;
--   ALTER TABLE memories DROP COLUMN IF EXISTS superseded_by;
