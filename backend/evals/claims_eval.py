"""Claim-verification gate (F4 spec §5): run verify_claim with the default chat model over hand-written cases on
papers already in the library, and compare each verdict with the verdicts accepted for that case. Never saves a note.

    docker compose exec api python -m evals.claims_eval [--limit 10] [--out rows.jsonl]

Exit 0 pass, 1 fail (a gate missed, or no real model answer), 2 bad input.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import claims, llm_connections
from app.core.errors import DomainError
from app.db import SessionLocal
from app.providers.llm import build_llm
from evals.run import EvalError, resolve_papers

CONFIG = Path(__file__).with_name("claims.yaml")
Verdict = Literal["supported", "partially_supported", "not_supported", "not_addressed"]
KEPT = ("supported", "partially_supported")  # verdicts that rest on a quote


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper: str = Field(min_length=1)  # title prefix; must match exactly one paper
    claim: str = Field(min_length=1)
    kind: Literal["true", "overstated", "contradicted", "unrelated"]
    accepted: list[Verdict] = Field(min_length=1)


class Gates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accuracy_min: float = Field(ge=0, le=1)
    overstated_min: float = Field(ge=0, le=1)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gates: Gates
    cases: list[Case] = Field(min_length=1)


def load_config(path: Path) -> Config:
    try:
        return Config.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise EvalError(f"{path}: {exc}") from exc


@dataclass(frozen=True)
class Metrics:
    cases: int
    accuracy: float
    per_kind: dict[str, float]
    overstated_accuracy: float | None  # None: no overstated case was run
    confusion: dict[str, dict[str, int]]  # kind -> verdict -> count
    quote_kept_rate: float | None  # among supported / partially_supported verdicts; None: there were none
    cannot_verify: int
    median_seconds: float


def _share(count: int, total: int) -> float:
    return count / total if total else 0.0


def score(rows: list[dict]) -> Metrics:
    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    per_kind = {k: _share(sum(r["correct"] for r in v), len(v)) for k, v in by_kind.items()}
    checked = [r for r in rows if r["verdict"] in KEPT]
    return Metrics(
        cases=len(rows),
        accuracy=_share(sum(r["correct"] for r in rows), len(rows)),
        per_kind=per_kind,
        overstated_accuracy=per_kind.get("overstated"),
        confusion={k: dict(Counter(r["verdict"] for r in v)) for k, v in by_kind.items()},
        quote_kept_rate=_share(sum(r["quote"] is not None for r in checked), len(checked)) if checked else None,
        cannot_verify=sum(r["verdict"] == "cannot_verify" for r in rows),
        median_seconds=statistics.median(r["seconds"] for r in rows) if rows else 0.0,
    )


def _failed_gates(metrics: Metrics, gates: Gates) -> list[str]:
    failed = []
    if metrics.accuracy < gates.accuracy_min:
        failed.append(f"accuracy {metrics.accuracy:.2f} < {gates.accuracy_min}")
    if metrics.overstated_accuracy is None:
        failed.append("no overstated cases to measure")
    elif metrics.overstated_accuracy < gates.overstated_min:
        failed.append(f"overstated accuracy {metrics.overstated_accuracy:.2f} < {gates.overstated_min}")
    return failed


def passes(metrics: Metrics, gates: Gates) -> bool:
    return not _failed_gates(metrics, gates)


async def evaluate(session: AsyncSession, cases: list[Case], llm, limit: int | None) -> tuple[Metrics, list[dict]]:
    """Check the first `limit` cases (all when None); no note is saved."""
    cases = cases[:limit]
    ids = await resolve_papers(session, cases)
    rows = []
    for case in cases:
        start = time.perf_counter()
        check = await claims.verify_claim(session, case.claim, paper_id=ids[case.paper], llm=llm, save=False)
        rows.append({
            "paper": case.paper, "claim": case.claim, "kind": case.kind, "accepted": case.accepted,
            "verdict": check.verdict, "correct": check.verdict in case.accepted, "reason": check.reason,
            "quote": check.quote, "confidence": check.confidence, "seconds": time.perf_counter() - start,
        })
    return score(rows), rows


async def execute(session, config: Config, llm, limit: int | None, out: Path | None) -> int:
    try:
        metrics, rows = await evaluate(session, config.cases, llm, limit)
    except (EvalError, DomainError) as exc:
        print(f"bad input: {exc}", file=sys.stderr)
        return 2
    if out:
        out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(metrics)
    if metrics.cannot_verify == metrics.cases:
        print("FAIL: no real model answer (every check was cannot_verify)")
        return 1
    failed = _failed_gates(metrics, config.gates)
    print("FAIL: " + "; ".join(failed) if failed else "PASS")
    return 1 if failed else 0


def _limit(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


async def _main(args) -> int:
    config = load_config(args.config)
    async with SessionLocal() as session:
        connection, model = await llm_connections.resolve(session, None)
        return await execute(session, config, build_llm(connection, model.name), args.limit, args.out)


def main() -> None:
    parser = argparse.ArgumentParser()  # a bad argument exits 2 by itself
    parser.add_argument("--config", type=Path, default=CONFIG)
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
