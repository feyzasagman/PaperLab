from pathlib import Path

import pytest

from app.core.claim_text import Block, blocks, inline, reference_dois, split_references

FIXTURES = Path(__file__).parent / "fixtures" / "claims"


def test_headings_list_markers_and_emphasis_go_and_each_item_is_its_own_block():
    note = "# Title\n\n## Section\n\nA paragraph that\nwraps.\n\n- **Label.** First item.\n- Second *item*.\n1. Third."
    assert blocks(note) == [
        Block("A paragraph that wraps."),
        Block("Label. First item.", lead="Label."),
        Block("Second item."),
        Block("Third."),
    ]


def test_only_a_block_that_opens_with_a_bold_span_has_a_lead():
    note = "- **Smith (2020).** Finding.\n- __Lee (2021).__ Other.\n- Plain **bold** later.\n\n**Para** text."
    assert [b.lead for b in blocks(note)] == ["Smith (2020).", "Lee (2021).", "", "Para"]


def test_a_non_list_paragraph_is_only_interrupted_by_a_numbered_one():
    assert [b.text for b in blocks("Published in\n2024. The authors found X (Smith, 2020).")] == [
        "Published in 2024. The authors found X (Smith, 2020)."
    ]
    assert [b.text for b in blocks("1. a (Smith, 2020)\n2. b (Lee, 2021)")] == ["a (Smith, 2020)", "b (Lee, 2021)"]
    assert [b.text for b in blocks("Intro line\n1. first (Smith, 2020)")] == ["Intro line", "first (Smith, 2020)"]
    assert [b.text for b in blocks("- a (Smith, 2020)\n- b (Lee, 2021)")] == ["a (Smith, 2020)", "b (Lee, 2021)"]


def test_a_table_row_is_one_block_with_its_cells_and_the_header_row_is_skipped():
    note = (
        "| Study | Venue |\n|---|---|\n| Anderson, Shah & Kreminski (2024) | C&C '24 |\n"
        "| Doshi \\| Hauser | *Sci. Adv.* |"
    )
    assert blocks(note) == [
        Block("Anderson, Shah & Kreminski (2024) C&C '24", ("Anderson, Shah & Kreminski (2024)", "C&C '24")),
        Block("Doshi | Hauser Sci. Adv.", ("Doshi | Hauser", "Sci. Adv.")),
    ]


def test_front_matter_code_links_and_underscores_inside_words():
    note = (
        "---\ntags: [x]\n---\nUses `snake_case` and [a link](http://x.y) and _emphasis_.\n\n"
        "```\nSmith (2020) code\n```\n"
    )
    assert blocks(split_references(note)[0]) == [Block("Uses snake_case and a link and emphasis.")]


def test_wikilinks_stay_for_the_claim_builder():
    assert inline("See [[four-c-model]] and **bold**.") == "See [[four-c-model]] and bold."


def test_the_reference_list_is_split_off_under_any_usual_heading():
    for heading in ("## References", "# Bibliography", "### Works cited", "## Reference list"):
        body, refs = split_references(f"Body (Smith, 2020).\n\n{heading}\n\n- Smith, J. (2020). T.")
        assert (body.strip(), refs.strip()) == ("Body (Smith, 2020).", "- Smith, J. (2020). T.")
    assert split_references("No list here.") == ("No list here.", "")



@pytest.mark.parametrize(
    "heading",
    [
        "References", "References:", "**References**", "**References:**", "__References__", "## **References**",
        "### Reference list", "Bibliography", "# Works cited", "Sources", "## Références", "## Bibliografía",
        "# Literatur", "## Literaturverzeichnis", "REFERENCES", "## References ##",
    ],
)
def test_reference_heading_variants_split_the_list(heading):
    body, refs = split_references(f"Body (Smith, 2020).\n\n{heading}\n\n- Smith, J. (2020). T.")
    assert (body.strip(), refs.strip()) == ("Body (Smith, 2020).", "- Smith, J. (2020). T.")


def test_a_sentence_that_merely_mentions_references_does_not_split():
    text = "These references (Smith, 2020) matter.\nSee the sources above.\n**References are fun** too."
    assert split_references(text) == (text, "")


def test_no_pilot_block_keeps_markdown_syntax():
    for name in ("definition.md", "ai-creativity-benchmarks.md"):
        body, _ = split_references((FIXTURES / name).read_text())
        for block in blocks(body):
            assert not any(mark in block.text for mark in ("**", "## ", "|---")), (name, block.text)
            assert not block.text.startswith(("#", "- ", "* ")), (name, block.text)


def test_reference_dois_by_first_author_and_year_skipping_shared_keys():
    refs = (
        "- Lu, X., & Ho, Y. (2025). One, with no DOI.\n"
        "- Lu, Q. (2025). Two. https://doi.org/10.1/two\n"
        "- Doshi, A. R., & Hauser, O. P. (2024). Gen AI. *Sci. Adv.*"
        " https://doi.org/10.1126/sciadv.adn5290 — verified\n"
        "- Gómez-Rodríguez, C., & Williams, P. (2023). A confederacy. https://doi.org/10.18653/v1/2023.findings-emnlp.966\n"
        "- Boden, M. A. (2004). *The creative mind*. Routledge. ISBN 978-0-415-31452-7\n"
    )
    assert reference_dois(refs) == {
        ("doshi", 2024): "10.1126/sciadv.adn5290",
        ("gomez-rodriguez", 2023): "10.18653/v1/2023.findings-emnlp.966",
    }


def test_the_pilot_notes_reference_lists():
    dois = reference_dois(split_references((FIXTURES / "ai-creativity-benchmarks.md").read_text())[1])
    assert dois[("anderson", 2024)] == "10.1145/3635636.3656204"
    assert ("lu", 2025) not in dois  # two different Lu (2025) papers, only one with a DOI
    assert len(reference_dois(split_references((FIXTURES / "definition.md").read_text())[1])) == 8
