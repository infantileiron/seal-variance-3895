import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import text

from src.config.database import Base, SessionLocal, engine
from src.models import entities
from src.routes import auth, processes, samples, assessments
from src.middlewares.errors import ErrorMiddleware
from src.middlewares.audit import AuditMiddleware
from src.services import queue_bus


def _run_sql_migrations():
    """对已有数据库幂等执行 migrations 目录下的增量 DDL（create_all 不会改既有表）。"""
    migrations = sorted((Path(__file__).resolve().parents[2] / 'migrations').glob('*.sql'))
    with engine.begin() as conn:
        for path in migrations:
            if path.name == '001_initial.sql':
                continue
            conn.execute(text(path.read_text(encoding='utf-8')))
            logging.info('migration applied: %s', path.name)


@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    try:
        _run_sql_migrations()
    except Exception:
        logging.exception('sql migration failed; server remains available')
    if os.getenv('SEED_DEMO', 'true').lower() == 'true':
        try:
            from scripts.seed_demo import seed
            seed()
        except Exception:
            logging.exception('seed failed; server remains available')
    yield


app = FastAPI(title='包装工艺方差评估', lifespan=lifespan)
app.add_middleware(AuditMiddleware)
app.add_middleware(ErrorMiddleware)
for router in [auth.router, processes.router, samples.router, assessments.router]:
    app.include_router(router)


@app.get('/health')
def health():
    with SessionLocal() as db:
        db.execute(text('select 1'))
    queue_bus.client().ping()
    return {'status': 'healthy'}
