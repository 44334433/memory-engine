-- 008_rls：多租户 RLS 预备层（2026-09-19 拍板批；配套 config.MULTI_TENANT 缺省关）
-- 拍板变更（2026-09-19，用户显式拍板）：触发条件由「真实多宿主接入事件」提前为「现在做预备层」。
-- 编号说明：与 008_superseded_by.sql 共用 008 前缀=任务书钉死的文件名（008_rls.sql）；
--   文件名序「008_rls.sql」<「008_superseded_by.sql」（r<s），两者互不依赖（本文件不引用
--   superseded_by 列），新库按序应用与存量库（008_superseded_by 已入账、本文件进 pending 队）均成立。
-- 幂等：可重复执行（ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS /
--   DROP POLICY IF EXISTS + CREATE POLICY / ENABLE|FORCE RLS 重复执行无副作用）。
--
-- 内容（memories/entities/edges 三表）：
--   1) tenant_id 列：memories 已由 002 留位；entities/edges 本迁移补列（text 可空，
--      NULL=单宿主存量/未分区）+ 非空部分索引（全 NULL 时不占索引空间，与 002 同法）。
--   2) RLS：ENABLE + FORCE ROW LEVEL SECURITY + 策略 tenant_isolation（读 USING / 写 WITH CHECK 同式）：
--        coalesce(current_setting('app.tenant_id', true), '') = ''   -- 会话未设租户=全量直通
--        OR tenant_id = current_setting('app.tenant_id', true)       -- 设了租户=只见本租户
--      语义要点：
--        a) 缺省形态（生产 daemon/CLI/迁移/备份——一切不设 app.tenant_id 的会话）= 读写全量，
--           零行为变化（FORCE 只约束表属主，属主会话同样被策略①式放行，实测见影子库验收）。
--        b) 设了 app.tenant_id 的会话：tenant_id IS NULL 行不可见（对齐应用层 recall 既有语义
--           「NULL 行仅无过滤时可见」）——启用前必须回填，见下方三步。
--        c) 超级用户/带 BYPASSRLS 角色天然豁免 RLS（FORCE 仅覆盖表属主，不覆盖超级用户）：
--           CI 用 postgres 超级用户连接时策略不生效=非破坏；生产 memengine 属主连接受 FORCE 约束。
--
-- 启用多租户三步（本迁移只是第①步的 DB 侧件；完整手册=README「多租户」节）：
--   ① 应用本迁移：python3 scripts/migrate.py --apply
--   ② 回填归属：UPDATE memories  SET tenant_id='default' WHERE tenant_id IS NULL;
--      （entities/edges 按需同法；此后新写入必须显式带 tenant_id——retain 请求字段透传）
--   ③ 开开关 + 会话设租户：每个宿主部署设 env MEMORY_ENGINE_MULTI_TENANT=1、
--      MEMORY_ENGINE_TENANT_ID=<该宿主租户名>（应用层 recall 强制过滤）；
--      需要 DB 级纵深防御时，由会话层 SET app.tenant_id='<租户>'（引擎连接池本批不注入，
--      属真实多宿主接入时再拍板的深化项——届时注意池化连接的 SET 生命周期须按事务收敛）。

-- —— 1) tenant_id 列（entities/edges 补位；memories 002 已留） ——
ALTER TABLE entities ADD COLUMN IF NOT EXISTS tenant_id text;
ALTER TABLE edges    ADD COLUMN IF NOT EXISTS tenant_id text;
CREATE INDEX IF NOT EXISTS idx_ent_tenant   ON entities (tenant_id) WHERE tenant_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_edges_tenant ON edges    (tenant_id) WHERE tenant_id IS NOT NULL;

-- —— 2) RLS 策略（三表同式；DROP+CREATE 保证幂等与策略内容收敛） ——
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON memories;
CREATE POLICY tenant_isolation ON memories
  USING      (coalesce(current_setting('app.tenant_id', true), '') = ''
              OR tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (coalesce(current_setting('app.tenant_id', true), '') = ''
              OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE entities FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON entities;
CREATE POLICY tenant_isolation ON entities
  USING      (coalesce(current_setting('app.tenant_id', true), '') = ''
              OR tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (coalesce(current_setting('app.tenant_id', true), '') = ''
              OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE edges FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON edges;
CREATE POLICY tenant_isolation ON edges
  USING      (coalesce(current_setting('app.tenant_id', true), '') = ''
              OR tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (coalesce(current_setting('app.tenant_id', true), '') = ''
              OR tenant_id = current_setting('app.tenant_id', true));
