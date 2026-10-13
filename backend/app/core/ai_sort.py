"""The hit list ordered by the AI's score (M39 F3): relevance high to low, then confident before unsure, then found
order. Pages by an opaque offset cursor (base64 of the offset); the found order keeps its keyset cursor."""

import base64
import binascii

from sqlalchemy import case

from app.core.errors import InvalidInput
from app.models.workspace_search import WorkspaceSearchHit as Hit

AI_ORDER = (
    Hit.suggestion_relevance.desc().nulls_last(),
    case({"high": 0, "medium": 1}, value=Hit.suggestion_confidence, else_=2),
    Hit.first_seen_at,
    Hit.id,
)


def encode_offset(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def decode_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidInput("invalid pagination cursor") from exc
    if offset < 0:
        raise InvalidInput("invalid pagination cursor")
    return offset
