"""Enqueue ARQ jobs from outside a FastAPI request, i.e. the MCP server (M39 F1). The API keeps app.state.arq."""

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.config import settings

_pool: ArqRedis | None = None  # tests replace it with conftest.FakeArq


async def enqueue(job: str, *args) -> None:
    # ponytail: one module-level pool for the process's life; the MCP server is one stdio process.
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    await _pool.enqueue_job(job, *args)
