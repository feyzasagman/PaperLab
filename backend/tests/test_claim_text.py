from pathlib import Path

from app.core.claim_text import Block, blocks, inline, split_references

FIXTURES = Path(__file__).parent / "fixtures" / "claims"


def test_headings_list_markers_and_emphasis_go_and_each_item_is_its_own_block():
    note = "# Title\n\n## Section\n\nA paragraph that\nwraps.\n\n- **Label.** First item.\n- Second *item*.\n1. Third."
    assert blocks(note) == [
        Block("A paragraph that wraps."), Block("Label. First item."), Block("Second item."), Block("Third.")
    ]


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


def test_no_pilot_block_keeps_markdown_syntax():
    for name in ("definition.md", "ai-creativity-benchmarks.md"):
        body, _ = split_references((FIXTURES / name).read_text())
        for block in blocks(body):
            assert not any(mark in block.text for mark in ("**", "## ", "|---")), (name, block.text)
            assert not block.text.startswith(("#", "- ", "* ")), (name, block.text)
