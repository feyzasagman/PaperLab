import logging

from arq import func
from arq.connections import RedisSettings
from arq.cron import cron

from app.config import settings
from app.workers.harvest import (
    BIORXIV_HARVEST_JOB_TIMEOUT,
    HARVEST_JOB_TIMEOUT,
    harvest_biorxiv,
    harvest_iacr_eprint,
    harvest_medrxiv,
)
from app.workers.ingest import ingest_paper, reembed_paper
from app.workers.references import fetch_references
from app.workers.screening_suggest import SUGGEST_JOB_TIMEOUT, suggest_screening
from app.workers.workspace_search import SEARCH_RUN_JOB_TIMEOUT, run_workspace_search

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
# SEARCH_RUN_JOB_TIMEOUT lives in workers/workspace_search.py, not here — the worker's own wall-clock deadline
# check (C2 Important 1) is computed from that same constant, so importing it (rather than redefining a second
# copy here) means the registered job_timeout and the loop's own backstop can never drift apart.


class WorkerSettings:
    # No on_startup: the search model loads on the first job that needs it, once it is downloaded (D136).
    functions = [
        ingest_paper,
        reembed_paper,
        fetch_references,
        func(run_workspace_search, timeout=SEARCH_RUN_JOB_TIMEOUT),
        func(suggest_screening, timeout=SUGGEST_JOB_TIMEOUT),
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
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
