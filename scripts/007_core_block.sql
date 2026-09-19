-- 007_core_block.sql: W1 核心记忆块（core block，2026-09-18 拍板顺序 W2 之后）
-- memories 加一列 pinned：显式钉住标记（人工 PATCH /v1/memories/{id} {pinned:true}），
-- 是 GET /v1/core-block 常驻注入区的快速通道；自动精选轨消费的是既有列
-- （memory_type/polarity/adopt_count/access_count），不新增信号列。
-- 兼容性铁律（#6 存量零变化）：NOT NULL DEFAULT false——PG11+ 常量默认值为元数据级操作，
-- 不重写存量 39k 行；旧代码显式列 INSERT/SELECT 不受影响；检索评分公式不消费本列
-- （recall 三路/RRF/衰减全部零触碰），core block 是新增可选消费方。
ALTER TABLE memories ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false;
-- 部分索引：pinned 行预期占比极低（人工显式标记，个位数~几十条），只给 pinned=true 建索引；
-- 自动精选轨走 seq/bank_state 既有扫描面（39k 行谓词过滤毫秒级，实测 P95 见执行文档），不建索引。
CREATE INDEX IF NOT EXISTS idx_mem_pinned ON memories (seq DESC) WHERE pinned;
COMMENT ON COLUMN memories.pinned IS 'W1 核心记忆块：人工钉住标记，常驻注入快速通道（默认 false=存量零影响）';
-- 回滚（成对）：
--   DROP INDEX IF EXISTS idx_mem_pinned;
--   ALTER TABLE memories DROP COLUMN IF EXISTS pinned;
