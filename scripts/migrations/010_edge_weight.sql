-- 010_edge_weight.sql —— 多跳图谱批（2026-09-21 拍板插队）：持久边权 + as_of 遍历伴生索引
--
-- ① edges.weight：边权乘子列（NOT NULL DEFAULT 1.0 CHECK >0）。
--    语义：召回图遍历按路径边权乘积传播；实体消歧等离线通道「结果落边权重而非改实体名」
--    的持久化落点（本批召回侧消歧为请求内乘子，不写库；列先立，写通道=后续离线批）。
--    PG11+ 元数据级加列（非 volatile 默认值）不重写存量行——13,788 边秒级完成。
-- ② 非部分伴生索引：as_of 历史遍历谓词（valid_at<=t AND invalid_at IS NULL OR >t）
--    不命中 invalid_at IS NULL 部分索引，逐跳 BFS 的每步扩展需要 src/dst/entity 三向索引。
--    （13k 行规模 seq scan 也可接受，索引为 3 万→百万边规模预置；#23 复杂度择优。）
-- 幂等：全部 IF NOT EXISTS / DROP-then-ADD 形态，可重复执行。

ALTER TABLE edges ADD COLUMN IF NOT EXISTS weight double precision NOT NULL DEFAULT 1.0;
ALTER TABLE edges DROP CONSTRAINT IF EXISTS edges_weight_positive_check;
ALTER TABLE edges ADD CONSTRAINT edges_weight_positive_check CHECK (weight > 0);

CREATE INDEX IF NOT EXISTS idx_edges_src_all    ON edges (src_mid);
CREATE INDEX IF NOT EXISTS idx_edges_dst_all    ON edges (dst_mid);
CREATE INDEX IF NOT EXISTS idx_edges_entity_all ON edges (entity_id);
