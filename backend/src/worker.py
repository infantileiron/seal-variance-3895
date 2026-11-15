"""方差分析 worker，可水平扩多副本并发运行。

协作模型（PostgreSQL 为事实源，Redis 仅作唤醒通知）：
1. 启动时把遗留 queued 任务重新通知，并修复已完成任务的缺失结果文件；
2. 每轮先回收心跳超时的 running 任务（崩溃/僵死恢复，记录恢复原因），
   再用 FOR UPDATE SKIP LOCKED 行锁认领 queued 任务，多 worker 互不重复；
3. 计算期间心跳线程按 HEARTBEAT_INTERVAL 续租；
4. 结果先原子写文件、再带归属条件提交记录；旧 worker 迟到结果被 fencing 拒绝；
5. 全程只使用任务提交时冻结的 snapshot。

测试钩子（仅测试环境通过环境变量启用，生产不设置即无副作用）：
- SEAL_CRASH_AFTER=<task_id>:file      写结果文件后、保存评估记录前退出；
- SEAL_CRASH_AFTER=<task_id>:record    保存评估记录后退出；
- SEAL_STALL_TASK=<task_id|*>          认领后停止心跳并挂起，模拟僵死等待接手；
- SEAL_DELAY_TASK=<task_id>            写文件后停心跳并延迟，模拟旧进程迟到回写；
- SEAL_SLOW_TASK=<task_id|*>           拉长计算耗时（保持心跳），制造并行窗口。
"""
import logging
import os
import socket
import threading
import time
import uuid
from pathlib import Path

from src.config.database import SessionLocal
from src.services import queue_bus, task_lifecycle
from src.services.statistics import anova

WORKER_ID = f'{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}'
HEARTBEAT_INTERVAL = float(os.environ.get('HEARTBEAT_INTERVAL', '5'))
CRASH_HOOK = os.environ.get('SEAL_CRASH_AFTER', '')
STALL_TASK = os.environ.get('SEAL_STALL_TASK', '')
DELAY_TASK = os.environ.get('SEAL_DELAY_TASK', '')
DELAY_SECONDS = float(os.environ.get('SEAL_DELAY_SECONDS', '8'))
SLOW_TASK = os.environ.get('SEAL_SLOW_TASK', '')
SLOW_SECONDS = float(os.environ.get('SEAL_SLOW_SECONDS', '2'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('worker')


class LeaseWorker:
    def __init__(self, worker_id: str):
        self.worker_id = worker_id
        self._stop = threading.Event()
        self._hb_thread = None

    def _heartbeat_loop(self, task_id: int):
        while not self._stop.is_set():
            if self._stop.wait(HEARTBEAT_INTERVAL):
                return
            try:
                with SessionLocal() as db:
                    ok = task_lifecycle.heartbeat(db, task_id, self.worker_id)
                if not ok:
                    log.warning('assessment=%s heartbeat lost ownership; stopping lease', task_id)
                    self._stop.set()
                    return
            except Exception:
                log.exception('heartbeat failed for assessment=%s', task_id)

    def _start_heartbeat(self, task_id: int):
        self._stop.clear()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, args=(task_id,), daemon=True)
        self._hb_thread.start()

    def _stop_heartbeat(self):
        self._stop.set()
        if self._hb_thread:
            self._hb_thread.join(timeout=HEARTBEAT_INTERVAL + 2)
        self._hb_thread = None

    def _maybe_crash(self, task_id: int, point: str):
        if CRASH_HOOK == f'{task_id}:{point}':
            log.error('test crash hook fired: assessment=%s point=%s worker=%s',
                      task_id, point, self.worker_id)
            os._exit(27)

    def process_once(self, task) -> None:
        task_id = task.id
        self._start_heartbeat(task_id)
        try:
            if SLOW_TASK == '*' or str(task_id) == SLOW_TASK:
                # 测试钩子：拉长单任务耗时（保持心跳），制造多 worker 并行窗口。
                log.info('assessment=%s slow-task hook sleeping %ss', task_id, SLOW_SECONDS)
                time.sleep(SLOW_SECONDS)
            if STALL_TASK == '*' or str(task_id) == STALL_TASK:
                # 模拟 worker 僵死：停止心跳并挂起，等待其他 worker 按租约超时接手。
                self._stop.set()
                log.warning('assessment=%s stalled by test hook; worker=%s frozen', task_id, self.worker_id)
                while True:
                    time.sleep(3600)
            # 计算仅依赖冻结快照，与当前数据库中的样本修订完全隔离。
            result = anova(task.snapshot)
            with SessionLocal() as db:
                if task_lifecycle.ensure_owner(db, task_id, self.worker_id) is None:
                    log.warning('assessment=%s result discarded: ownership moved to another worker', task_id)
                    return
            task_lifecycle.write_artifact(task_id, result)
            if str(task_id) == DELAY_TASK:
                # 模拟旧进程写文件后卡住且停止续租：租约超时后由其他 worker 接手，
                # 旧进程醒来再回写时必须被 fencing 拒绝，不能覆盖接手者的结果。
                self._stop.set()
                log.warning('assessment=%s delayed after artifact write; lease frozen for %ss',
                            task_id, DELAY_SECONDS)
                time.sleep(DELAY_SECONDS)
            self._maybe_crash(task_id, 'file')
            with SessionLocal() as db:
                saved = task_lifecycle.complete_record(db, task_id, self.worker_id, result)
            if not saved:
                log.warning('assessment=%s late write-back rejected by fencing; worker=%s',
                            task_id, self.worker_id)
                return
            self._maybe_crash(task_id, 'record')
            log.info('assessment=%s status=completed worker=%s attempts=%s',
                     task_id, self.worker_id, task.attempts)
        except Exception as exc:
            log.exception('assessment=%s computation failed', task_id)
            try:
                with SessionLocal() as db:
                    task_lifecycle.fail_task(db, task_id, self.worker_id, str(exc))
            except Exception:
                log.exception('assessment=%s failure persistence failed', task_id)
        finally:
            self._stop_heartbeat()

    def run(self):
        Path('/tmp/worker-heartbeat').touch()
        with SessionLocal() as db:
            queue_bus.requeue_pending(db)
            repaired = task_lifecycle.repair_completed_artifacts(db)
        if repaired:
            log.info('rebuilt missing artifacts on startup: %s', repaired)
        log.info('seal variance worker started id=%s stale_after=%ss',
                 self.worker_id, task_lifecycle.STALE_SECONDS)
        while True:
            Path('/tmp/worker-heartbeat').touch()
            try:
                queue_bus.wait_notification(timeout=2)
                with SessionLocal() as db:
                    # claim_task 内部先回收超时租约，再用行锁认领，天然支持多副本并发。
                    task = task_lifecycle.claim_task(db, self.worker_id)
                if task is not None:
                    self.process_once(task)
            except Exception:
                log.exception('worker loop error')
                time.sleep(2)


if __name__ == '__main__':
    LeaseWorker(WORKER_ID).run()
