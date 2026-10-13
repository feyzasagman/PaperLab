"""The shared quote rule (F3 abstracts, F4 passages): a kept quote is at least 3 words, at most 300 characters and a
notes.fold substring of one of its sources. Anything else proves nothing and is dropped."""

from app.core import notes

QUOTE_MIN_WORDS = 3  # one or two words match any source and prove nothing
QUOTE_MAX_CHARS = 300  # a whole-source copy proves nothing either


def fold(text: str) -> str:
    # A hyphen broken across a line folds to "cost- effective"; rejoin it on both sides.
    return notes.fold(text).replace("- ", "-")


def check_quotes(quotes: list[str], sources: list[str], max_kept: int) -> tuple[list[tuple[str, int]], bool]:
    """(kept, dropped): each kept quote with the index of the first source holding it, at most `max_kept` of them
    (verify first, then cap). `dropped` is True when any asked quote failed the rule."""
    folded = [fold(s) for s in sources]
    asked = [q.strip() for q in quotes if q.strip()]
    valid = []
    for quote in asked:
        f = fold(quote)
        if len(f.split()) < QUOTE_MIN_WORDS or len(f) > QUOTE_MAX_CHARS:
            continue
        index = next((i for i, source in enumerate(folded) if f in source), None)
        if index is not None:
            valid.append((quote, index))
    return valid[:max_kept], len(valid) < len(asked)
