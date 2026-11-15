"""评估任务的并发认领、心跳租约、崩溃恢复与结果落盘。

正确性边界：
- 认领由 PostgreSQL 行锁（FOR UPDATE SKIP LOCKED）裁决，多个 worker 不会拿到同一行；
- 心跳超时的 running 任务可被其他 worker 接手，attempts 递增并留痕恢复原因；
- 回写结果带 worker 身份与状态条件（fencing），旧 worker 的迟到结果无法覆盖接手者；
- 结果文件先落盘（同目录临时文件 + os.replace 原子替换），再提交数据库 completed，
  任一断点中断后都可按原 snapshot 幂等重算恢复，不产生半截文件；
- 计算始终使用任务提交时冻结的 snapshot，与后续样本修订无关。
"""
import json
import logging
import os
import tempfile
from datetime import datetime, timezone

from sqlalchemy import select, update

from src.models.entities import Assessment

ARTIFACT_DIR = os.environ.get('ARTIFACT_DIR', '/app/artifacts')
STALE_SECONDS = float(os.environ.get('TASK_STALE_SECONDS', '30'))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def artifact_path(task_id: int) -> str:
    return os.path.join(ARTIFACT_DIR, f'{task_id}.json')


def _is_stale(heartbeat_at: str | None, now_ts: float) -> bool:
    if not heartbeat_at:
        return True
    try:
        return now_ts - datetime.fromisoformat(heartbeat_at).timestamp() > STALE_SECONDS
    except ValueError:
        return True


def requeue_stale(db, worker_id: str) -> list[int]:
    """把心跳超时的 running 任务标记为可重新认领，返回被回收的任务 id。"""
    now_ts = datetime.now(timezone.utc).timestamp()
    # skip_locked：多个 worker 同时回收时，同一行只由一个回收者处理，避免重复留痕。
    rows = db.scalars(
        select(Assessment).where(Assessment.status == 'running').with_for_update(skip_locked=True)
    ).all()
    reclaimed = []
    for task in rows:
        if _is_stale(task.heartbeat_at, now_ts):
            previous = task.worker_id or 'unknown'
            note = (f'worker {previous} 心跳超时（{STALE_SECONDS:g}s 未续租），'
                    f'由 {worker_id} 接手重试（第 {task.attempts + 1} 次执行）')
            task.status = 'queued'
            task.worker_id = None
            task.recovery_note = note
            reclaimed.append(task.id)
            logging.warning('assessment=%s requeued: %s', task.id, note)
    if reclaimed:
        db.commit()
    return reclaimed


def claim_task(db, worker_id: str) -> Assessment | None:
    """认领一个待处理任务；并发下靠行锁保证唯一归属，返回 None 表示队列暂空。"""
    requeue_stale(db, worker_id)
    task = db.scalar(
        select(Assessment)
        .where(Assessment.status == 'queued')
        .order_by(Assessment.id)
        .with_for_update(skip_locked=True)
    )
    if task is None:
        return None
    now = utcnow()
    note = task.recovery_note or f'{worker_id} 首次认领（第 1 次执行）'
    task.status = 'running'
    task.worker_id = worker_id
    task.leased_at = now
    task.heartbeat_at = now
    task.attempts = (task.attempts or 0) + 1
    task.error = ''
    if not task.recovery_note:
        task.recovery_note = note
    db.commit()
    db.refresh(task)
    return task


def heartbeat(db, task_id: int, worker_id: str) -> bool:
    """续租。仅当任务仍由本 worker 持有时生效，接手后旧 worker 的续租自动失败。"""
    result = db.execute(
        update(Assessment)
        .where(Assessment.id == task_id,
               Assessment.worker_id == worker_id,
               Assessment.status == 'running')
        .values(heartbeat_at=utcnow())
    )
    db.commit()
    return result.rowcount == 1


def write_artifact(task_id: int, result: dict) -> str:
    """原子写入结果文件：同目录临时文件 + fsync + os.replace，目录中永不出现半截 JSON。"""
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    final_path = artifact_path(task_id)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    fd, tmp_path = tempfile.mkstemp(prefix=f'.{task_id}.', suffix='.tmp', dir=ARTIFACT_DIR)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, final_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return final_path


def _artifact_intact(task_id: int, result: dict) -> bool:
    try:
        with open(artifact_path(task_id), encoding='utf-8') as fh:
            return json.load(fh) == result
    except (OSError, ValueError):
        return False


def ensure_owner(db, task_id: int, worker_id: str) -> Assessment | None:
    """返回仍由本 worker 持有的任务，否则 None（已被接手或结束）。"""
    task = db.get(Assessment, task_id)
    if task is None or task.status != 'running' or task.worker_id != worker_id:
        logging.warning('ownership lost: assessment=%s owner=%s caller=%s status=%s',
                        task_id, task.worker_id if task else None, worker_id,
                        task.status if task else None)
        return None
    return task


def complete_record(db, task_id: int, worker_id: str, result: dict) -> bool:
    """把成功结果写入评估记录。fencing：归属失效时拒绝，旧 worker 迟到结果无法覆盖接手者。"""
    task = ensure_owner(db, task_id, worker_id)
    if task is None:
        logging.warning('late result rejected: assessment=%s caller=%s', task_id, worker_id)
        return False
    task.result = result
    task.status = 'completed'
    task.heartbeat_at = utcnow()
    db.commit()
    return True


def finish_task(db, task_id: int, worker_id: str, result: dict) -> bool:
    """完整成功路径：先原子落盘文件，再 fencing 提交数据库记录。"""
    if ensure_owner(db, task_id, worker_id) is None:
        return False
    # 先落盘文件，再提交数据库；任一步中断，恢复流程都能用同一 snapshot 幂等重做。
    path = write_artifact(task_id, result)
    if not complete_record(db, task_id, worker_id, result):
        return False
    # 提交后再校验一次文件；卷异常导致缺失时立即用已持久化的结果补齐。
    if not _artifact_intact(task_id, result):
        write_artifact(task_id, result)
    logging.info('assessment=%s completed by %s artifact=%s', task_id, worker_id, path)
    return True


def fail_task(db, task_id: int, worker_id: str, message: str) -> bool:
    """记录业务失败（如样本不足、组内方差为零）；同样做归属 fencing。"""
    task = db.get(Assessment, task_id)
    if task is None or task.status != 'running' or task.worker_id != worker_id:
        logging.warning('late failure rejected: assessment=%s caller=%s', task_id, worker_id)
        return False
    task.status = 'failed'
    task.error = message[:2000]
    task.heartbeat_at = utcnow()
    db.commit()
    return True


def repair_completed_artifacts(db) -> list[int]:
    """已完成但结果文件缺失/损坏（写文件后中断、卷临时不可用）的任务，用库内结果补盘。"""
    repaired = []
    rows = db.scalars(select(Assessment).where(Assessment.status == 'completed')).all()
    for task in rows:
        if not task.result or _artifact_intact(task.id, task.result):
            continue
        write_artifact(task.id, task.result)
        repaired.append(task.id)
        logging.warning('assessment=%s artifact rebuilt from database result', task.id)
    return repaired
