"""AI pre-screening gate (F3 spec §8): run the v2 suggestion over a workspace's already-decided hits with the default
chat model and compare with the owner's own stage-1 decisions. Never writes a suggestion or a decision.

    docker compose exec api python -m evals.screening_eval [--workspace NAME] [--limit 50] [--out rows.jsonl]

Agreement: include matches relevant or maybe, exclude matches not_relevant. An unsure suggestion counts as a
disagreement unless the owner said maybe (a hesitant model and a hesitant owner agree).
Exit 0 pass, 1 fail (too few decided hits, or a gate missed), 2 bad input.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import llm_connections, workspaces
from app.core.errors import DomainError
from app.core.screening import load_pool
from app.core.screening_suggest import suggest
from app.db import SessionLocal
from app.providers.llm import build_llm

CONFIG = Path(__file__).with_name("screening.yaml")
POSITIVE = ("relevant", "maybe")


class EvalError(Exception):
    pass


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1)
    min_decided: int = Field(ge=1)
    missed_relevant_rate_max: float = Field(ge=0, le=1)
    quote_drop_rate_max: float = Field(ge=0, le=1)


def load_config(path: Path) -> Config:
    try:
        return Config.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise EvalError(f"{path}: {exc}") from exc


@dataclass(frozen=True)
class Metrics:
    decided: int
    agreement: float
    missed_relevant_rate: float | None  # None: no relevant/maybe hits, nothing to miss
    exclude_precision: float | None
    unsure_share: float
    quote_drop_rate: float
    mean_relevance: dict[str, float]
    median_seconds: float


def _agrees(label: str, verdict: str) -> bool:
    return (
        (verdict == "include" and label in POSITIVE)
        or (verdict == "exclude" and label == "not_relevant")
        or (verdict == "unsure" and label == "maybe")
    )


def _share(count: int, total: int) -> float:
    return count / total if total else 0.0


def score(rows: list[dict]) -> Metrics:
    n = len(rows)
    positives = [r for r in rows if r["label"] in POSITIVE]
    excludes = [r for r in rows if r["verdict"] == "exclude"]
    relevance: dict[str, list[int]] = {}
    for r in rows:
        if r["relevance"] is not None:
            relevance.setdefault(r["label"], []).append(r["relevance"])
    return Metrics(
        decided=n,
        agreement=_share(sum(_agrees(r["label"], r["verdict"]) for r in rows), n),
        missed_relevant_rate=_share(sum(r["verdict"] == "exclude" for r in positives), len(positives))
        if positives else None,
        exclude_precision=_share(sum(r["label"] == "not_relevant" for r in excludes), len(excludes))
        if excludes else None,
        unsure_share=_share(sum(r["verdict"] == "unsure" for r in rows), n),
        quote_drop_rate=_share(sum("quote_dropped" in r["flags"] for r in rows), n),
        mean_relevance={label: statistics.mean(v) for label, v in relevance.items()},
        median_seconds=statistics.median(r["seconds"] for r in rows) if rows else 0.0,
    )


def passes(metrics: Metrics, config: Config) -> bool:
    return (
        metrics.decided >= config.min_decided
        and metrics.missed_relevant_rate is not None
        and metrics.missed_relevant_rate <= config.missed_relevant_rate_max
        and metrics.quote_drop_rate <= config.quote_drop_rate_max
    )


async def evaluate(session: AsyncSession, workspace_name: str, llm, limit: int | None) -> tuple[Metrics, list[dict]]:
    """Suggest for the `limit` most recently decided hits (all when None); read-only."""
    pool = await load_pool(session, await workspaces.by_name(session, workspace_name))
    ws = pool.workspace
    question, inclusion, exclusion = ws.research_question, ws.inclusion_criteria, ws.exclusion_criteria
    if not (question or inclusion or exclusion):
        inclusion = ws.screening_criteria
    if not (question or inclusion or exclusion):
        raise EvalError("This workspace has no screening criteria yet.")
    decided = sorted(
        (row for row in pool.rows if row[0].stage1_status and row[0].stage1_decided_at),
        key=lambda row: row[0].stage1_decided_at, reverse=True,
    )[:limit]
    rows = []
    for hit, ref in decided:
        title = ref.title if ref else hit.normalized_title
        start = time.perf_counter()
        s = await suggest(llm, question, inclusion, exclusion, title, ref.abstract if ref else None)
        rows.append({
            "title": title, "label": hit.stage1_status, "verdict": s.verdict, "reason": s.reason,
            "relevance": s.relevance, "confidence": s.confidence, "flags": s.flags,
            "seconds": time.perf_counter() - start,
        })
    return score(rows), rows


async def execute(session, config: Config, workspace: str, llm, limit: int | None, out: Path | None) -> int:
    try:
        metrics, rows = await evaluate(session, workspace, llm, limit)
    except (EvalError, DomainError) as exc:
        print(f"bad input: {exc}", file=sys.stderr)
        return 2
    if out:
        out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(metrics)
    ok = passes(metrics, config)
    if metrics.missed_relevant_rate is None:
        print("FAIL: no relevant hits to measure misses against")
        return 1
    print("PASS" if ok else f"FAIL: need {config.min_decided} decided, missed-relevant <= "
          f"{config.missed_relevant_rate_max}, quote-drop <= {config.quote_drop_rate_max}")
    return 0 if ok else 1


def _limit(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


async def _main(args) -> int:
    config = load_config(args.config)
    async with SessionLocal() as session:
        connection, model = await llm_connections.resolve(session, None)
        return await execute(session, config, args.workspace or config.workspace, build_llm(connection, model.name),
                             args.limit, args.out)


def main() -> None:
    parser = argparse.ArgumentParser()  # a bad argument exits 2 by itself
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--workspace")
    parser.add_argument("--limit", type=_limit)
    parser.add_argument("--out", type=Path)
    try:
        code = asyncio.run(_main(parser.parse_args()))
    except (EvalError, DomainError) as exc:
        print(f"bad input: {exc}", file=sys.stderr)
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
