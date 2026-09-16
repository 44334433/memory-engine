-- 003：多模态附件层（P0 2026-09-17）——attachments 表 + down 块
-- 幂等：IF NOT EXISTS 可重复执行（runner=scripts/migrate.py --apply，按文件名序、engine_meta.migrations 防重）
-- 范围铁律：本批只增不改既有表；召回融合（recal.py 消费 description_embedding）属 P1，不在本迁移。
-- 级联语义：memory_id ON DELETE CASCADE（memories purge 硬删→行级联清）；
--   磁盘文件=内容寻址 {sha256}.{ext}，purge 记忆不主动删文件（孤儿文件无害、可由
--   DELETE /v1/memories/{id}/attachments/{aid}?purge=true 按 content_hash 引用清零时回收）。
-- 唯一性偏差说明：spec 写「content_hash text unique」；落地为部分唯一索引（state<>'deleted'）——
--   软删行不占键，同内容软删后可重新上传（完整唯一约束会永久堵死重传）。

CREATE TABLE IF NOT EXISTS attachments (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  memory_id             uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  path                  text NOT NULL,
  content_hash          text NOT NULL,
  mime                  text NOT NULL,
  bytes                 integer NOT NULL,
  description           text,
  description_embedding vector(1024),
  embed_ver             text,
  state                 text NOT NULL DEFAULT 'active'
                        CHECK (state IN ('active','no_desc','deleted')),
  created_at            timestamptz NOT NULL DEFAULT now()
);

-- 判重唯一键：仅未软删行参与（软删后同内容可重传；并发竞态由应用层捕获 UniqueViolation 兜底）
CREATE UNIQUE INDEX IF NOT EXISTS uq_att_hash ON attachments (content_hash) WHERE state <> 'deleted';
-- 列表查询（GET /v1/memories/{id}/attachments）
CREATE INDEX IF NOT EXISTS idx_att_memory ON attachments (memory_id);

-- schema_ver 登记 2→3
INSERT INTO engine_meta(key, value) VALUES ('schema_ver', '3'::jsonb)
ON CONFLICT (key) DO UPDATE SET value = '3'::jsonb;

-- >>> DOWN-BEGIN（仅手动/测试执行：逐行去掉 "-- " 后执行；migrate.py runner 不执行注释块）
-- DROP TABLE IF EXISTS attachments;
-- UPDATE engine_meta SET value = '2'::jsonb WHERE key = 'schema_ver';
-- <<< DOWN-END
