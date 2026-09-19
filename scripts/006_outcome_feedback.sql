-- 006_outcome_feedback.sql: 自进化专项 #1 outcome 反馈信号列（2026-09-18 拍板 a）
-- memories 加三列：outcome（最近一次反馈三值）/ polarity（EMA 演化态 ±1 内）/ outcome_at（最近信号时刻）。
-- 选型=新列而非独立表（论证见 src/memory_engine/outcome.py 模块 docstring：加列同构先例×6、
-- recall hydrate 单表零 JOIN、事件全历史已由 changelog(op='feedback')+access_events 承载）。
-- 兼容性铁律：三列全部可空且无 DEFAULT 表达式负担（NULL=无信号）——PG11+ ADD COLUMN
-- 为元数据级操作，不重写存量行；旧运行代码显式列 INSERT/SELECT 不受影响；
-- 检索评分公式不消费这三列（本批仅透出+接线），存量召回分数逐位不变。
ALTER TABLE memories ADD COLUMN IF NOT EXISTS outcome text;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS polarity double precision;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS outcome_at timestamptz;
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_outcome_check;
ALTER TABLE memories ADD CONSTRAINT memories_outcome_check
  CHECK (outcome IN ('adopted', 'corrected', 'useless'));   -- NULL 过 CHECK（三值逻辑=通过）
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_polarity_range_check;
ALTER TABLE memories ADD CONSTRAINT memories_polarity_range_check
  CHECK (polarity BETWEEN -1 AND 1);   -- EMA 递推数学上恒收敛于开区间，闭区间兜底舍入
-- 不加索引：反馈行占比极低（信号稀疏），按 outcome 筛走 seq 扫描即可；出现规模化消费方再补
-- （部分索引 WHERE outcome IS NOT NULL 是登记的升级路径，见执行文档回滚/演进节）。
-- 回滚（成对）：
--   ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_outcome_check;
--   ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_polarity_range_check;
--   ALTER TABLE memories DROP COLUMN IF EXISTS outcome, DROP COLUMN IF EXISTS polarity, DROP COLUMN IF EXISTS outcome_at;
