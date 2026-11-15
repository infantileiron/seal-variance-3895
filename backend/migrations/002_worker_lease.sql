-- 002: 多 worker 并发认领、心跳租约与崩溃恢复字段（对已有库幂等执行）。
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS worker_id VARCHAR(80);
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS leased_at VARCHAR(40);
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS heartbeat_at VARCHAR(40);
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS recovery_note TEXT NOT NULL DEFAULT '';
-- 旧库中遗留的 running 任务没有有效租约，恢复为待计算后由 worker 重新认领。
UPDATE assessments SET status='queued', worker_id=NULL, recovery_note=recovery_note
  WHERE status='running' AND heartbeat_at IS NULL;
