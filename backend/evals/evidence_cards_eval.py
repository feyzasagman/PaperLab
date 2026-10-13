"""Evidence-card gate (F5 spec §7): build a card with the default chat model for each paper of hand-made cases and
compare it with the expected card. Never writes (record=False).

    docker compose exec api python -m evals.evidence_cards_eval [--limit 10] [--out rows.jsonl]

Exit 0 pass, 1 fail (a gate missed, or no card at all), 2 bad input.
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

from app.core import evidence_cards, llm_connections, numbers
from app.core.errors import DomainError
from app.db import SessionLocal
from app.providers.base import LLMError, LLMUnavailable
from app.providers.llm import build_llm
from evals.run import EvalError, resolve_papers

CONFIG = Path(__file__).with_name("evidence_cards.yaml")
TEXT_FIELDS = ("research_question", "design", "limitation")


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper: str = Field(min_length=1)  # title prefix; must match exactly one paper
    n: int | None
    effect_sizes: list[str] = Field(min_length=1, max_length=3)
    research_question: list[str] = Field(min_length=1, max_length=3)
    design: list[str] = Field(min_length=1, max_length=3)
    limitation: list[str] = Field(max_length=2)  # empty: not scored


class Gates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_hit_min: float = Field(ge=0, le=1)
    n_exact_min: float = Field(ge=0, le=1)


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
    cards: int  # cards that were built (not failed)
    field_hit: float
    n_exact: float
    per_field: dict[str, float]
    quote_kept_rate: float | None  # kept / claimed items; None: the model claimed nothing
    number_drops: int
    unsupported_rate: float | None
    median_seconds: float


def _share(count: int, total: int) -> float:
    return count / total if total else 0.0


def _kept(items: list[dict]) -> list[dict]:
    return [i for i in items if not i["flags"] and i["quote"]]


def _texts(card_fields: dict, name: str) -> str:
    items = card_fields.get(name) or []
    items = items if isinstance(items, list) else [items]
    return " ".join(str(i.get("value") or "") for i in _kept(items)).lower()


def _stem(word: str) -> str:
    """Lowercase, minus a trailing "s" then a trailing "e": "creative" is found in "creativity"."""
    return word.lower().removesuffix("s").removesuffix("e")


def _words_hit(words: list[str], text: str) -> bool:
    return sum(_stem(w) in text for w in words) * 2 >= len(words)


def _effect_hit(expected: list[str], fields: dict) -> bool:
    shown = [set(numbers.tokens(str(i.get("value") or ""))) for i in _kept(fields.get("effect_sizes") or [])]
    return any(set(numbers.tokens(e)) <= have for e in expected for have in shown if have)


def _n_hit(expected: int | None, fields: dict) -> bool:
    return (fields.get("sample") or {}).get("n") == expected  # a dropped or empty sample has n None


def judge(case: Case, fields: dict) -> dict[str, bool]:
    """Which scored fields of the case the card hit."""
    hits = {
        "n": _n_hit(case.n, fields),
        "effect_sizes": _effect_hit(case.effect_sizes, fields),
        "research_question": _words_hit(case.research_question, _texts(fields, "research_question")),
        "design": _words_hit(case.design, _texts(fields, "design")),
    }
    if case.limitation:
        hits["limitation"] = _words_hit(case.limitation, _texts(fields, "limitations"))
    return hits


def _claimed(fields: dict) -> list[dict]:
    items = []
    for value in fields.values():
        items.extend(value if isinstance(value, list) else [value])
    return [i for i in items if i.get("flags") or i.get("quote")]


def score(rows: list[dict]) -> Metrics:
    hits = [h for r in rows for h in r["hits"].values()]
    per_field: dict[str, list[bool]] = {}
    for r in rows:
        for name, hit in r["hits"].items():
            per_field.setdefault(name, []).append(hit)
    claimed = [i for r in rows for i in r["claimed"]]
    kept = [i for i in claimed if not i["flags"]]
    return Metrics(
        cases=len(rows),
        cards=sum(r["status"] == "ready" for r in rows),
        field_hit=_share(sum(hits), len(hits)),
        n_exact=_share(sum(r["hits"]["n"] for r in rows), len(rows)),
        per_field={k: _share(sum(v), len(v)) for k, v in per_field.items()},
        quote_kept_rate=_share(len(kept), len(claimed)) if claimed else None,
        number_drops=sum("number_not_in_source" in i["flags"] for i in claimed),
        unsupported_rate=_share(sum("unsupported" in i["flags"] for i in claimed), len(claimed)) if claimed else None,
        median_seconds=statistics.median(r["seconds"] for r in rows) if rows else 0.0,
    )


def _failed_gates(metrics: Metrics, gates: Gates) -> list[str]:
    failed = []
    if metrics.field_hit < gates.field_hit_min:
        failed.append(f"field hit {metrics.field_hit:.2f} < {gates.field_hit_min}")
    if metrics.n_exact < gates.n_exact_min:
        failed.append(f"N exact {metrics.n_exact:.2f} < {gates.n_exact_min}")
    return failed


def passes(metrics: Metrics, gates: Gates) -> bool:
    return not _failed_gates(metrics, gates)


async def _build(session, case: Case, paper_id, llm) -> dict:
    start = time.perf_counter()
    row = {"paper": case.paper}
    try:
        card = await evidence_cards.build_card(session, paper_id, llm=llm, record=False)
    except (LLMError, LLMUnavailable) as exc:
        card = None
        row["error"] = str(exc)
    fields = card.fields if card and card.status == "ready" else {}
    return {
        **row, "status": card.status if card else "failed", "error": row.get("error") or (card.error if card else None),
        "hits": judge(case, fields), "claimed": _claimed(fields), "fields": fields,
        "seconds": time.perf_counter() - start,
    }


async def evaluate(session: AsyncSession, cases: list[Case], llm, limit: int | None) -> tuple[Metrics, list[dict]]:
    """Build a card for the first `limit` cases (all when None). Nothing is written."""
    cases = cases[:limit]
    ids = await resolve_papers(session, cases)
    rows = [await _build(session, case, ids[case.paper], llm) for case in cases]
    return score(rows), rows


async def execute(session, config: Config, llm, limit: int | None, out: Path | None) -> int:
    try:
        metrics, rows = await evaluate(session, config.cases, llm, limit)
    except (EvalError, DomainError) as exc:
        print(f"bad input: {exc}", file=sys.stderr)
        return 2
    if out:
        out.write_text("".join(json.dumps({k: v for k, v in r.items() if k != "claimed"}) + "\n" for r in rows))
    print(metrics)
    if metrics.cards == 0:
        print("FAIL: no card was built (every paper failed)")
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
