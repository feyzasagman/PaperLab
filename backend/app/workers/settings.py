import logging

from arq import func
from arq.connections import RedisSettings
from arq.cron import cron

from app.config import settings
from app.core.evidence_cards import JOB_TIMEOUT as EVIDENCE_CARDS_JOB_TIMEOUT
from app.core.queue import INGEST_QUEUE
from app.workers.evidence_cards import MAX_TRIES as EVIDENCE_CARDS_MAX_TRIES
from app.workers.evidence_cards import generate_evidence_cards
from app.workers.harvest import (
    BIORXIV_HARVEST_JOB_TIMEOUT,
    HARVEST_JOB_TIMEOUT,
    harvest_biorxiv,
    harvest_iacr_eprint,
    harvest_medrxiv,
)
from app.workers.hit_enrichment import ENRICH_JOB_TIMEOUT, enrich_hits
from app.workers.ingest import MAX_TRIES, ingest_paper, reembed_paper
from app.workers.references import fetch_references
from app.workers.saved_searches import schedule_saved_searches
from app.workers.screening_suggest import SUGGEST_JOB_TIMEOUT, suggest_screening
from app.workers.workspace_search import SEARCH_RUN_JOB_TIMEOUT, run_workspace_search

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
# SEARCH_RUN_JOB_TIMEOUT lives in workers/workspace_search.py, not here — the worker's own wall-clock deadline
# check (C2 Important 1) is computed from that same constant, so importing it (rather than redefining a second
# copy here) means the registered job_timeout and the loop's own backstop can never drift apart.


def _to_ingest_queue(job: str):
    """Moves a paper job found on the default queue to the ingest queue. Redis keeps its queue across an update, so an
    install can hold paper jobs queued before they had their own worker.
    ponytail: drop these once no install predates the ingest queue."""

    async def forward(ctx: dict, *args) -> None:
        await ctx["redis"].enqueue_job(job, *args, _queue_name=INGEST_QUEUE)

    return func(forward, name=job)


class WorkerSettings:
    """Everything but a paper's ingest and re-embed, up to arq's default ten jobs at once."""

    # No on_startup: the search model loads on the first job that needs it, once it is downloaded (D136).
    functions = [
        _to_ingest_queue("ingest_paper"),
        _to_ingest_queue("reembed_paper"),
        fetch_references,
        func(run_workspace_search, timeout=SEARCH_RUN_JOB_TIMEOUT),
        func(enrich_hits, timeout=ENRICH_JOB_TIMEOUT),
        func(suggest_screening, timeout=SUGGEST_JOB_TIMEOUT),
        func(generate_evidence_cards, timeout=EVIDENCE_CARDS_JOB_TIMEOUT, max_tries=EVIDENCE_CARDS_MAX_TRIES),
    ]
    cron_jobs = [
        cron(
            harvest_iacr_eprint,
            name="harvest_iacr_eprint",
            hour={3},
            minute={0},
            run_at_startup=True,
            timeout=HARVEST_JOB_TIMEOUT,
        ),
        cron(
            harvest_biorxiv,
            name="harvest_biorxiv",
            hour={4},
            minute={0},
            run_at_startup=True,
            timeout=BIORXIV_HARVEST_JOB_TIMEOUT,
        ),
        cron(
            harvest_medrxiv,
            name="harvest_medrxiv",
            hour={4},
            minute={30},
            run_at_startup=True,
            timeout=BIORXIV_HARVEST_JOB_TIMEOUT,
        ),
        cron(schedule_saved_searches, name="schedule_saved_searches", second=0, run_at_startup=True),
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)


class IngestWorkerSettings:
    """A paper's extraction, chunking and embedding, one paper at a time: ten at once ran the worker out of memory
    (2026-10-13). The next queued paper starts as soon as one finishes, while search runs keep the other worker."""

    queue_name = INGEST_QUEUE
    max_jobs = 1
    max_tries = MAX_TRIES
    functions = [ingest_paper, reembed_paper]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
