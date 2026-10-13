"""The hit list ordered by the AI's score (M39 F3): relevance high to low, then confident before unsure, then found
order. Pages by a keyset cursor over the four sort keys, so a hit decided between pages can't shift the next page."""

import base64
import binascii
import json
import uuid
from datetime import datetime

from sqlalchemy import case, func, tuple_

from app.core.errors import InvalidInput
from app.models.workspace_search import WorkspaceSearchHit as Hit

# Unsuggested hits have no relevance (valid scores are 1-5), so 0 sorts them last.
_RELEVANCE = -func.coalesce(Hit.suggestion_relevance, 0)
_CONFIDENCE = case({"high": 0}, value=Hit.suggestion_confidence, else_=1)
AI_ORDER = (_RELEVANCE, _CONFIDENCE, Hit.first_seen_at, Hit.id)


def encode_cursor(hit: Hit) -> str:
    key = [-(hit.suggestion_relevance or 0), 0 if hit.suggestion_confidence == "high" else 1,
           hit.first_seen_at.isoformat(), str(hit.id)]
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode()


def after_cursor(cursor: str):
    """A SQL condition selecting the hits strictly after the cursor's hit in AI order."""
    try:
        rel, conf, seen, hit_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if not (isinstance(rel, int) and conf in (0, 1)):
            raise ValueError
        key = (rel, conf, datetime.fromisoformat(seen), uuid.UUID(hit_id))
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError) as exc:
        raise InvalidInput("invalid pagination cursor") from exc
    return tuple_(*AI_ORDER) > tuple_(*key)
