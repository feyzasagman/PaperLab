import pytest

from app.core.numbers import NumberCandidate, all_in, find_numbers, parse_number, tokens


@pytest.mark.parametrize(
    ("raw", "value", "error"),
    [
        ("88.5", 88.5, None),
        ("−3", -3.0, None),  # U+2212, the minus sign typeset papers use
        ("-3", -3.0, None),
        ("1,234", 1234.0, None),
        ("88.5 ± 0.3", 88.5, 0.3),
        ("88.5±0.3", 88.5, 0.3),
        ("34%", 34.0, None),
        ("110M", 110_000_000.0, None),
        ("1.2B", 1_200_000_000.0, None),
        ("340K", 340_000.0, None),
        ("1.2e-3", 0.0012, None),
        ("90.9*", 90.9, None),
        ("90.9†", 90.9, None),
        ("**90.9**", 90.9, None),
        ("  4.92\n", 4.92, None),
        (".5", 0.5, None),
    ],
)
def test_numbers_parse_to_value_and_error(raw, value, error):
    parsed = parse_number(raw)
    assert parsed.value == pytest.approx(value)
    assert parsed.error == (None if error is None else pytest.approx(error))


LABELS = ["", "   ", "—", "–", "-", "n/a", "N/A", "BERT-L", "(A)", "×106", "88.5 F1", "1,5", "±0.3"]


@pytest.mark.parametrize("raw", LABELS)
def test_labels_and_empty_cells_have_no_value(raw):
    parsed = parse_number(raw)
    assert (parsed.value, parsed.error) == (None, None)


def test_every_number_in_a_selection_is_a_candidate_with_the_word_after_it_as_unit():
    text = "BERT-L reaches 90.9 F1 on SQuAD 1.1 and 88.5 ± 0.3 EM, up 34% with 340M parameters."
    assert find_numbers(text) == [
        NumberCandidate(raw="90.9", value=90.9, error=None, unit_hint="F1"),
        NumberCandidate(raw="1.1", value=1.1, error=None, unit_hint=None),  # "and" is not a unit
        NumberCandidate(raw="88.5 ± 0.3", value=88.5, error=0.3, unit_hint="EM"),
        NumberCandidate(raw="34%", value=34.0, error=None, unit_hint="%"),
        NumberCandidate(raw="340M", value=340_000_000.0, error=None, unit_hint="parameters"),
    ]


def test_digits_inside_words_are_not_candidates():
    assert find_numbers("GPT-2 and v2 of word2vec, 3x faster") == []


@pytest.mark.parametrize(
    ("raw", "value"),
    [("14k", 14_000.0), ("110m", 110_000_000.0), ("1.2b", 1_200_000_000.0)],
)
def test_a_lowercase_scale_suffix_reads_like_its_uppercase(raw, value):
    assert parse_number(raw).value == pytest.approx(value)


# "310 K" is 310 kelvin, not 310 thousand: a scale suffix has to touch its number. The unit belongs to the
# column, so the cell reads as a label and the owner sees it, instead of a silently multiplied number.
@pytest.mark.parametrize("raw", ["310 K", "2.5 B", "14 M"])
def test_a_letter_after_a_space_is_a_unit_so_the_cell_is_a_label(raw):
    assert (parse_number(raw).value, parse_number(raw).error) == (None, None)


def test_a_unit_after_a_number_is_a_hint_not_a_thousand_multiplier():
    assert find_numbers("annealed at 310 K for 2 h") == [
        NumberCandidate(raw="310", value=310.0, error=None, unit_hint="K"),
        NumberCandidate(raw="2", value=2.0, error=None, unit_hint="h"),
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("d = 0.45, 95% CI [0.21, 0.69], p < .001", ["0.45", "95", "0.21", "0.69", "0.001"]),
        ("0.21, 0.69", ["0.21", "0.69"]),
        ("[0.21,0.69]", ["0.21", "0.69"]),
        ("1,234 people", ["1234"]),
        ("1,234,567.5", ["1234567.5"]),
        ("1,2345", ["1", "2345"]),
        ("12 345 and 7 890", ["12345", "7890"]),
        ("r = −0.3 and –0.4", ["-0.3", "-0.4"]),
        ("-.05 and +.5", ["-0.05", "+0.5"]),
        ("2019-2020", ["2019", "2020"]),
        ("5%", ["5"]),
        ("no numbers here", []),
    ],
)
def test_tokens(text, expected):
    assert tokens(text) == expected


def test_all_in_normalises_both_sides():
    assert all_in("p = 0.05", "p < .05")
    assert all_in("-0.3", "r = −0.3")
    assert all_in("1234", "N = 1,234")
    assert all_in("5", "5% of them")


def test_all_in_rejects_a_missing_or_changed_number():
    assert not all_in("0.54", "d = 0.45")
    assert not all_in("0.3", "r = −0.3")  # the sign matters
    assert not all_in("d = 0.45, p = .01", "d = 0.45, p = .001")


def test_a_value_without_numbers_passes():
    assert all_in("Cohen's d", "anything")
