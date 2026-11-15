"""Redis 通知队列。

队列只是唤醒信号，任务事实以 PostgreSQL 为准：
- 发布失败（Redis 短暂不可用）不影响已落库的 queued 任务，worker 轮询数据库兜底；
- 重复通知不会导致重复处理，真正的认领由数据库行锁决定；
- 重启/清空队列后，requeue_orphans 可按数据库状态重新发布通知。
"""
import logging
import os
import time

import redis

QUEUE_KEY = 'seal:assessments'

_client = None


def client():
    global _client
    if _client is None:
        _client = redis.Redis.from_url(os.environ['REDIS_URL'], socket_timeout=3, socket_connect_timeout=3)
    return _client


def publish(task_id: int) -> bool:
    """尽力发布一次任务通知；失败返回 False，由调用方记录并依赖 DB 轮询兜底。"""
    try:
        client().lpush(QUEUE_KEY, int(task_id))
        return True
    except redis.RedisError:
        logging.exception('redis publish failed for assessment=%s; DB polling will pick it up', task_id)
        return False


def wait_notification(timeout: int = 2) -> None:
    """阻塞等待一次唤醒信号；Redis 不可用时退化为短轮询，绝不抛出。"""
    try:
        client().brpop(QUEUE_KEY, timeout=timeout)
    except redis.RedisError:
        time.sleep(min(timeout, 2))


def requeue_pending(db_session) -> int:
    """把数据库中所有 queued 任务重新发布到队列，返回成功发布的条数。

    用于 worker 启动以及 Redis 恢复后补齐丢失的通知；重复通知由行锁去重。
    """
    from src.models.entities import Assessment
    from sqlalchemy import select

    ids = db_session.scalars(select(Assessment.id).where(Assessment.status == 'queued')).all()
    published = 0
    for task_id in ids:
        if publish(task_id):
            published += 1
    return published
