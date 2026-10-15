import pytest
from conftest import recorded_discovery
from pydantic import ValidationError

from app.core.candidates import (
    MAX_AUTHORS,
    MAX_PDF_URLS,
    Candidate,
    _ids,
    from_acm_dl,
    from_biorxiv,
    from_biorxiv_entry,
    from_core,
    from_crossref,
    from_doaj,
    from_europe_pmc,
    from_hal,
    from_iacr_eprint,
    from_medrxiv,
    from_openaire,
    from_pmc,
    from_pubmed,
    from_s2,
    from_ssrn,
    from_work,
    from_zenodo,
    merge,
    with_s2,
)
from app.core.discovery import classify_query
from app.schemas.discovery import SOURCE_ID_PATTERNS, CandidateIn, CandidateOut


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("10.18653/v1/N19-1423", ("doi", "10.18653/v1/n19-1423")),
        ("https://doi.org/10.18653/v1/N19-1423", ("doi", "10.18653/v1/n19-1423")),
        ("doi: 10.1038/s41586-021-03819-2.", ("doi", "10.1038/s41586-021-03819-2")),
        ("10.48550/arXiv.1810.04805", ("arxiv", "1810.04805")),
        ("1810.04805", ("arxiv", "1810.04805")),
        ("arXiv:1810.04805v2", ("arxiv", "1810.04805")),
        ("https://arxiv.org/abs/2411.18021v1", ("arxiv", "2411.18021")),
        ("https://arxiv.org/pdf/2411.18021.pdf", ("arxiv", "2411.18021")),
        ("hep-th/9901001", ("arxiv", "hep-th/9901001")),
        ("math.GT/0309136", ("arxiv", "math.gt/0309136")),
        ("W2963341956", ("openalex", "W2963341956")),
        ("https://openalex.org/w2963341956", ("openalex", "W2963341956")),
        ("  BERT:   pre-training\n of transformers ", ("title", "BERT: pre-training of transformers")),
    ],
    ids=[
        "doi", "doi-url", "doi-with-label-and-period", "arxiv-doi", "arxiv", "arxiv-prefixed-versioned",
        "arxiv-abs-url", "arxiv-pdf-url", "old-arxiv", "old-arxiv-subject-class", "openalex", "openalex-url", "title",
    ],
)  # fmt: skip
def test_classify_query(query, expected):
    assert classify_query(query) == expected


def test_a_published_paper_without_a_free_copy_in_openalex_has_no_pdf_urls():
    bert = recorded_discovery("openalex_search_bert")["results"][0]

    candidate = from_work(bert)

    assert candidate.title.startswith("BERT")
    assert candidate.doi == "10.18653/v1/n19-1423"
    assert candidate.external_ids.get("openalex") == "W2963341956"
    assert candidate.external_ids.get("arxiv") is None
    assert candidate.pdf_urls == []  # its OA landing page is not a PDF link (D68)
    assert candidate.authors[0] == "Jacob Devlin"
    assert candidate.year == 2019


def test_an_arxiv_preprint_takes_its_id_from_its_doi_and_lists_its_pdf_once():
    candidate = from_work(recorded_discovery("openalex_work_arxiv_preprint"))

    assert candidate.external_ids.get("arxiv") == "2411.18021"
    assert candidate.pdf_urls == ["https://arxiv.org/pdf/2411.18021"]


def test_an_arxiv_location_gives_a_published_paper_its_arxiv_pdf_first():
    work = {
        "id": "https://openalex.org/W1",
        "title": "A paper",
        "doi": "https://doi.org/10.1000/pub",
        "best_oa_location": {
            "pdf_url": "https://publisher.example/pub.pdf",
            "landing_page_url": "https://publisher.example/pub",
        },
        "locations": [
            {"pdf_url": "https://publisher.example/pub.pdf", "landing_page_url": "https://publisher.example/pub"},
            {"pdf_url": None, "landing_page_url": "http://arxiv.org/abs/2003.07000v2"},
            {"pdf_url": "https://repository.example/copy.pdf", "landing_page_url": None},
            {"pdf_url": "ftp://old.example/copy.pdf", "landing_page_url": None},
        ],
        "open_access": {"oa_url": "https://publisher.example/pub"},
    }

    candidate = from_work(work)

    assert candidate.external_ids.get("arxiv") == "2003.07000"
    assert candidate.pdf_urls == [
        "https://arxiv.org/pdf/2003.07000",
        "https://publisher.example/pub.pdf",
        "https://repository.example/copy.pdf",
    ]


def test_an_openalex_article_hosted_on_arxiv_is_a_preprint():
    work = {
        "title": "On arXiv",
        "type": "article",
        "primary_location": {"source": {"display_name": "arXiv (Cornell University)"}},
    }

    candidate = from_work(work)

    assert candidate.source_type == "preprint"
    assert candidate.published_venue is None


def test_a_work_missing_optional_fields_still_maps():
    candidate = from_work({"title": None, "primary_location": {"source": None}})

    assert candidate == Candidate(title="Untitled", sources=("openalex",))


def test_semantic_scholar_papers_build_the_arxiv_pdf_even_when_their_pdf_link_is_empty():
    papers = recorded_discovery("s2_recommend_bert")["recommendedPapers"]
    arxiv_only = next(p for p in papers if p["externalIds"].get("ArXiv") and not p["openAccessPdf"]["url"])

    candidate = from_s2(arxiv_only)

    assert candidate.external_ids.get("arxiv") == arxiv_only["externalIds"]["ArXiv"]
    assert candidate.pdf_urls == [f"https://arxiv.org/pdf/{candidate.external_ids['arxiv']}"]
    assert candidate.external_ids.get("semantic_scholar") == arxiv_only["paperId"]
    assert candidate.authors == [a["name"] for a in arxiv_only["authors"]]


def test_semantic_scholar_dois_are_lowercased():
    candidate = from_s2({"paperId": "a" * 40, "title": "T", "externalIds": {"DOI": "10.1109/ASIANCON.2024.1"}})

    assert candidate.doi == "10.1109/asiancon.2024.1"
    assert candidate.pdf_urls == []


def test_semantic_scholar_adds_the_arxiv_copy_openalex_lacks():
    bert = from_work(recorded_discovery("openalex_search_bert")["results"][0])
    [s2_bert, _] = recorded_discovery("s2_batch_bert")

    candidate = with_s2(bert, s2_bert)

    assert candidate.external_ids.get("arxiv") == "1810.04805"
    assert candidate.pdf_urls[0] == "https://arxiv.org/pdf/1810.04805"
    assert candidate.external_ids.get("semantic_scholar") == s2_bert["paperId"]
    assert (candidate.title, candidate.doi, candidate.external_ids.get("openalex")) == (
        bert.title,
        bert.doi,
        bert.external_ids.get("openalex"),
    )


def test_no_semantic_scholar_record_leaves_the_candidate_as_it_is():
    bert = from_work(recorded_discovery("openalex_search_bert")["results"][0])

    assert with_s2(bert, None) is bert


# --- MAX_PDF_URLS / MAX_AUTHORS caps (the API's Add rejects a candidate over either) ---


def test_more_than_ten_pdf_locations_cap_at_ten_in_d68_order():
    work = {
        "id": "https://openalex.org/W1",
        "title": "A widely deposited paper",
        "locations": [{"pdf_url": f"https://repo.example/{i}.pdf"} for i in range(15)],
    }

    candidate = from_work(work)

    assert candidate.pdf_urls == [f"https://repo.example/{i}.pdf" for i in range(MAX_PDF_URLS)]


def test_more_than_500_authors_from_openalex_cap_at_500():
    work = {
        "id": "https://openalex.org/W1",
        "title": "A large collaboration",
        "authorships": [{"author": {"display_name": f"Author {i}"}} for i in range(600)],
    }

    candidate = from_work(work)

    assert len(candidate.authors) == MAX_AUTHORS
    assert candidate.authors[0] == "Author 0"
    assert candidate.authors[-1] == f"Author {MAX_AUTHORS - 1}"


def test_more_than_500_authors_from_semantic_scholar_cap_at_500():
    paper = {"paperId": "a" * 40, "title": "T", "authors": [{"name": f"Author {i}"} for i in range(600)]}

    candidate = from_s2(paper)

    assert len(candidate.authors) == MAX_AUTHORS


def _recorded_openalex_works():
    yield from recorded_discovery("openalex_search_bert")["results"]
    yield recorded_discovery("openalex_work_arxiv_preprint")


def _recorded_s2_papers():
    yield from (paper for paper in recorded_discovery("s2_batch_bert") if paper)
    yield recorded_discovery("s2_paper_arxiv_bert")
    yield from recorded_discovery("s2_recommend_bert")["recommendedPapers"]


@pytest.mark.parametrize(
    "candidate",
    [from_work(work) for work in _recorded_openalex_works()] + [from_s2(paper) for paper in _recorded_s2_papers()],
)
def test_every_recorded_candidate_round_trips_through_the_api_schemas(candidate):
    dumped = CandidateOut.model_validate(candidate, from_attributes=True).model_dump(mode="json")

    CandidateIn.model_validate(dumped)


@pytest.mark.parametrize(
    "external_ids",
    [{"arxiv": "not-an-arxiv-id"}, {"openalex": "12345"}, {"openalex": "W" + "1" * 20},
     {"semantic_scholar": "too-short"}, {"core": "abc"}, {"unknown_source": "anything"}],
    ids=["bad-arxiv", "bad-openalex", "openalex-over-20-chars", "bad-s2", "bad-core", "unknown-source"],
)  # fmt: skip
def test_candidate_in_rejects_a_malformed_or_unknown_external_id(external_ids):
    with pytest.raises(ValidationError):
        CandidateIn(title="T", external_ids=external_ids)


def test_candidate_in_accepts_every_known_source_in_its_real_shape():
    CandidateIn(
        title="T",
        external_ids={"arxiv": "1810.04805", "openalex": "W123", "semantic_scholar": "a" * 40, "core": "42"},
    )


def test_candidate_in_accepts_a_pubmed_id():
    CandidateIn(title="T", external_ids={"pubmed": "42825172"})


def test_candidate_in_rejects_a_non_numeric_pubmed_id():
    with pytest.raises(ValidationError):
        CandidateIn(title="T", external_ids={"pubmed": "not-a-pmid"})


def test_openalex_abstract_is_reconstructed_from_the_inverted_index():
    # OpenAlex never gives plain abstract text, only a word -> positions map (abstract_text, app/core/enrichment.py,
    # already used for library-paper enrichment; reused here for search candidates).
    work = {
        "title": "A paper",
        "primary_location": {"source": None},
        "abstract_inverted_index": {"BERT": [0], "is": [1], "a": [2], "model.": [3]},
    }
    assert from_work(work).abstract == "BERT is a model."


def test_openalex_abstract_is_none_when_the_index_is_absent():
    assert from_work({"title": "A paper", "primary_location": {"source": None}}).abstract is None


def test_semantic_scholar_abstract_maps_straight_through():
    paper = {"title": "A paper", "abstract": "A study of something interesting."}
    assert from_s2(paper).abstract == "A study of something interesting."


def test_core_abstract_maps_straight_through():
    work = {"id": 1, "title": "A paper", "abstract": "A study of something else."}
    assert from_core(work).abstract == "A study of something else."


def test_crossref_never_surfaces_an_abstract():
    """Crossref's own `abstract` field (when present) is raw JATS XML markup, not plain text — deliberately
    never read, so a candidate from this source never shows tags to the reader."""
    item = {"type": "journal-article", "title": ["A paper"], "abstract": "<jats:p>Some markup.</jats:p>"}
    assert from_crossref(item).abstract is None


def test_merge_keeps_the_abstract_from_the_most_trusted_source_that_has_one():
    """merge()'s trust order follows paper_sources.SOURCES (D73): openalex, crossref, semantic_scholar, arxiv,
    core. An OpenAlex record with no abstract should still pick up S2's, not lose it because OpenAlex ran first."""
    openalex_candidate = Candidate(title="Shared Paper", doi="10.1/shared", sources=("openalex",), abstract=None)
    s2_candidate = Candidate(
        title="Shared Paper", doi="10.1/shared", sources=("semantic_scholar",), abstract="The real abstract."
    )

    [merged] = merge({"openalex": [openalex_candidate], "semantic_scholar": [s2_candidate]}, limit=10)

    assert merged.abstract == "The real abstract."


def test_from_pubmed_maps_every_field():
    entry = {
        "pmid": "42825172",
        "title": "Are You PREPAREd?",
        "authors": ["Anique Baten", "Inge A Pool"],
        "year": 2026,
        "doi": "10.5334/pme.2504",
        "abstract": "Early after-hours work represents a demanding transition.",
    }

    candidate = from_pubmed(entry)

    assert candidate.title == "Are You PREPAREd?"
    assert candidate.authors == ["Anique Baten", "Inge A Pool"]
    assert candidate.year == 2026
    assert candidate.doi == "10.5334/pme.2504"
    assert candidate.external_ids == {"pubmed": "42825172"}
    assert candidate.abstract == "Early after-hours work represents a demanding transition."
    assert candidate.pdf_urls == []  # PubMed never lists a free PDF (PMC's own job, a later milestone)
    assert candidate.sources == ("pubmed",)


def test_from_pubmed_with_no_doi_or_abstract_still_maps():
    entry = {"pmid": "1", "title": "Bare Record", "authors": ["Jo Smith"], "year": 2020, "doi": None, "abstract": None}

    candidate = from_pubmed(entry)

    assert candidate.doi is None
    assert candidate.abstract is None
    assert candidate.external_ids == {"pubmed": "1"}  # key present even with every other field absent


def test_two_candidates_sharing_a_pubmed_id_are_the_same_paper():
    from app.core.candidates import _same_paper

    def record(title: str) -> dict:
        return {"pmid": "42825172", "title": title, "authors": [], "year": None, "doi": None, "abstract": None}

    a = from_pubmed(record("A"))
    b = from_pubmed(record("A, Revised"))

    assert _same_paper(a, b)


def test_doaj_and_openaire_ids_merge_different_sources_into_one_paper():
    """Same paper from two sources sharing a doaj/openaire id, with titles that differ on purpose: the match
    must come from the id alone, and the merged record keeps that id."""
    from_doaj_side = Candidate(title="Paper (DOAJ)", external_ids={"doaj": "abc"}, sources=("doaj",))
    from_openaire_side = Candidate(title="Paper (OpenAIRE)", external_ids={"doaj": "abc"}, sources=("openaire",))

    [merged] = merge({"doaj": [from_doaj_side], "openaire": [from_openaire_side]}, limit=10)

    assert merged.external_ids["doaj"] == "abc"
    assert set(merged.sources) == {"doaj", "openaire"}


def test_doaj_and_openaire_ids_merge_different_sources_into_one_paper_via_openaire_too():
    """Same shape as the doaj-key version above, but with a shared openaire id instead -- removing the
    openaire line from _ids()/_combined() would still pass that test undetected without this one."""
    from_doaj_side = Candidate(title="Paper (DOAJ)", external_ids={"openaire": "xyz"}, sources=("doaj",))
    from_openaire_side = Candidate(title="Paper (OpenAIRE)", external_ids={"openaire": "xyz"}, sources=("openaire",))

    [merged] = merge({"doaj": [from_doaj_side], "openaire": [from_openaire_side]}, limit=10)

    assert merged.external_ids["openaire"] == "xyz"
    assert set(merged.sources) == {"doaj", "openaire"}


def test_iacr_eprint_id_merges_different_sources_into_one_paper():
    """Same paper harvested via iacr_eprint and also found live by another source sharing the same
    iacr_eprint id, with titles that differ on purpose: the match must come from the shared id, not the title."""
    from_iacr_side = Candidate(
        title="Paper (IACR)", external_ids={"iacr_eprint": "2026/2368"}, sources=("iacr_eprint",)
    )
    from_other_side = Candidate(
        title="Paper (Other)", external_ids={"iacr_eprint": "2026/2368"}, sources=("openalex",)
    )

    [merged] = merge({"iacr_eprint": [from_iacr_side], "openalex": [from_other_side]}, limit=10)

    assert merged.external_ids["iacr_eprint"] == "2026/2368"
    assert set(merged.sources) == {"iacr_eprint", "openalex"}


def test_biorxiv_id_merges_different_sources_into_one_paper():
    """Same paper harvested via biorxiv and also found live by another source sharing the same DOI,
    with titles that differ on purpose: the match must come from the shared id, not the title."""
    from_biorxiv_side = Candidate(
        title="Paper (bioRxiv)", doi="10.1101/2026.10.01.000099",
        external_ids={"biorxiv": "10.1101/2026.10.01.000099"}, sources=("biorxiv",),
    )
    from_other_side = Candidate(
        title="Paper (Other)", doi="10.1101/2026.10.01.000099", sources=("crossref",),
    )

    [merged] = merge({"biorxiv": [from_biorxiv_side], "crossref": [from_other_side]}, limit=10)

    assert merged.external_ids["biorxiv"] == "10.1101/2026.10.01.000099"
    assert set(merged.sources) == {"biorxiv", "crossref"}


def test_from_pmc_maps_every_field():
    entry = {
        "pmcid": "13647476", "pmid": "42847424", "title": "Protein-Ratio Rheostats",
        "authors": ["Subbaya Subramanian"], "year": 2026, "doi": "10.1002/bies.70195",
        "abstract": "Cellular function depends on protein ratios.",
    }

    candidate = from_pmc(entry)

    assert candidate.external_ids == {"pmc": "13647476", "pubmed": "42847424"}
    assert candidate.sources == ("pmc",)
    assert candidate.doi == "10.1002/bies.70195"
    assert candidate.abstract == "Cellular function depends on protein ratios."


def test_from_pmc_with_no_pmid_has_only_a_pmc_id():
    entry = {
        "pmcid": "1", "pmid": None, "title": "Bare", "authors": [], "year": 2020, "doi": None, "abstract": None,
    }

    assert from_pmc(entry).external_ids == {"pmc": "1"}


def test_from_europe_pmc_reuses_pubmed_and_pmc_keys_not_its_own():
    entry = {
        "pmid": "42428244", "pmcid": "13346171", "title": "CRISPR review", "authors": ["Zhang X"],
        "year": 2026, "doi": "10.3389/fgeed.2026.1844919", "abstract": "An abstract.", "cited_by_count": 3,
    }

    candidate = from_europe_pmc(entry)

    assert candidate.external_ids == {"pubmed": "42428244", "pmc": "13346171"}
    assert "europe_pmc" not in candidate.external_ids
    assert candidate.sources == ("europe_pmc",)
    assert candidate.cited_by_count == 3


def test_from_europe_pmc_with_no_pmid_or_pmcid_has_no_external_ids():
    entry = {
        "pmid": None, "pmcid": None, "title": "Bare", "authors": [], "year": None, "doi": None,
        "abstract": None, "cited_by_count": None,
    }

    assert from_europe_pmc(entry).external_ids == {}


def test_a_paper_found_via_both_pubmed_and_europe_pmc_is_the_same_candidate():
    from_pubmed_candidate = from_pubmed(
        {"pmid": "42428244", "title": "CRISPR review", "authors": [], "year": 2026, "doi": None, "abstract": None}
    )
    from_epmc_candidate = from_europe_pmc(
        {"pmid": "42428244", "pmcid": None, "title": "CRISPR review (Europe PMC's own copy)", "authors": [],
         "year": 2026, "doi": None, "abstract": None, "cited_by_count": None}
    )

    assert _ids(from_pubmed_candidate) & _ids(from_epmc_candidate)  # share the "pubmed:42428244" key


def test_from_zenodo_maps_every_field():
    entry = {
        "id": "22132605", "title": "CRISPR Review", "authors": ["Dar, Shehneela"], "year": 2026,
        "doi": "10.5281/zenodo.22132605", "abstract": "An overview.",
        "pdf_url": "https://zenodo.org/api/records/22132605/files/paper.pdf/content",
    }

    candidate = from_zenodo(entry)

    assert candidate.external_ids == {"zenodo": "22132605"}
    assert candidate.sources == ("zenodo",)
    assert candidate.pdf_urls == ["https://zenodo.org/api/records/22132605/files/paper.pdf/content"]


def test_from_zenodo_with_no_pdf_has_no_pdf_urls():
    entry = {
        "id": "1", "title": "Bare", "authors": [], "year": None, "doi": None, "abstract": None,
        "pdf_url": None,
    }

    assert from_zenodo(entry).pdf_urls == []


def test_from_hal_maps_every_field():
    entry = {
        "docid": "4020890", "title": "CRISPR Review", "authors": ["Shafie, Nurul"], "year": 2014,
        "doi": "10.4172/1948-593x.1000109", "abstract": "A review.",
        "pdf_url": "https://hal.science/hal-04020890/file/paper.pdf",
    }

    candidate = from_hal(entry)

    assert candidate.external_ids == {"hal": "4020890"}
    assert candidate.sources == ("hal",)
    assert candidate.pdf_urls == ["https://hal.science/hal-04020890/file/paper.pdf"]


def test_from_hal_with_no_pdf_doi_or_abstract_maps_without_them():
    entry = {"docid": "1", "title": "Bare", "authors": [], "year": None, "doi": None, "abstract": None, "pdf_url": None}

    candidate = from_hal(entry)

    assert candidate.external_ids == {"hal": "1"}
    assert candidate.pdf_urls == []
    assert candidate.abstract is None


def test_from_hal_coerces_a_non_string_docid_to_str():
    entry = {
        "docid": 4020890, "title": "Bare", "authors": [], "year": None, "doi": None, "abstract": None,
        "pdf_url": None,
    }

    candidate = from_hal(entry)

    assert candidate.external_ids == {"hal": "4020890"}


def test_from_acm_dl_reuses_crossrefs_own_mapping_but_tags_the_acm_dl_source():
    item = {
        "DOI": "10.1145/3577923.3583658", "type": "proceedings-article",
        "title": ["AutoSpill: Credential Leakage from Mobile Password Managers"],
        "author": [{"given": "Andrea", "family": "Possemato"}],
        "issued": {"date-parts": [[2026]]}, "container-title": ["Proceedings of the ACM"],
        "is-referenced-by-count": 12,
    }

    candidate = from_acm_dl(item)

    assert candidate.sources == ("acm_dl",)
    assert candidate.doi == "10.1145/3577923.3583658"
    assert candidate.title == "AutoSpill: Credential Leakage from Mobile Password Managers"
    assert candidate.pdf_urls == []
    assert candidate.abstract is None


def test_from_acm_dl_filters_non_paper_types_the_same_way_from_crossref_does():
    item = {"DOI": "10.1145/x", "type": "dataset", "title": ["Not a paper"]}

    assert from_acm_dl(item) is None


def test_from_ssrn_reuses_from_works_own_mapping_but_tags_the_ssrn_source():
    work = {
        "id": "https://openalex.org/W1990513740", "doi": "https://doi.org/10.2139/ssrn.1496176",
        "title": "Diffusion of Innovations 1", "publication_year": 2026, "best_oa_location": None,
        "primary_location": {"source": {"display_name": "SSRN Electronic Journal"}}, "locations": [],
        "authorships": [{"author": {"display_name": "Ada Fixture"}}], "cited_by_count": 9,
    }

    candidate = from_ssrn(work)

    assert candidate.sources == ("ssrn",)
    assert candidate.external_ids == {"openalex": "W1990513740"}
    assert candidate.doi == "10.2139/ssrn.1496176"
    assert candidate.pdf_urls == []  # confirmed live: SSRN never has one, best_oa_location is None here


def test_from_ssrn_with_no_doi_or_authors_maps_without_them():
    work = {
        "id": "https://openalex.org/W2", "doi": None, "title": "Bare", "publication_year": None,
        "best_oa_location": None, "primary_location": None, "locations": [], "authorships": [],
        "cited_by_count": None,
    }

    candidate = from_ssrn(work)

    assert candidate.doi is None
    assert candidate.authors == []
    assert candidate.sources == ("ssrn",)


def test_from_doaj_maps_every_field():
    entry = {
        "id": "000122f776cb4f27b0f575971a4bed38",
        "bibjson": {
            "title": "A feature selection scheme", "abstract": "Selection of important features is vital.",
            "year": "2025", "author": [{"name": "Philemon Uten Emmoh"}],
            "identifier": [{"id": "10.46481/jnsps.2025.2273", "type": "doi"}, {"id": "2714-2817", "type": "pissn"}],
            "link": [
                {"content_type": "pdf", "type": "fulltext", "url": "https://example.org/paper.pdf"},
                {"content_type": "HTML", "type": "fulltext", "url": "https://example.org/landing"},
            ],
        },
    }

    candidate = from_doaj(entry)

    assert candidate.external_ids == {"doaj": "000122f776cb4f27b0f575971a4bed38"}
    assert candidate.sources == ("doaj",)
    assert candidate.doi == "10.46481/jnsps.2025.2273"
    assert candidate.title == "A feature selection scheme"
    assert candidate.authors == ["Philemon Uten Emmoh"]
    assert candidate.year == 2025
    assert candidate.abstract == "Selection of important features is vital."
    assert candidate.pdf_urls == ["https://example.org/paper.pdf"]


def test_from_doaj_with_no_doi_pdf_or_abstract_maps_without_them():
    entry = {
        "id": "0" * 32,
        "bibjson": {"title": "Bare", "year": None, "author": [], "identifier": [{"id": "1234-5678", "type": "eissn"}]},
    }

    candidate = from_doaj(entry)

    assert candidate.doi is None
    assert candidate.abstract is None
    assert candidate.pdf_urls == []
    assert candidate.external_ids == {"doaj": "0" * 32}


def test_from_doaj_strips_html_markup_from_the_abstract():
    entry = {
        "id": "0" * 32,
        "bibjson": {"title": "X", "abstract": "<p>Selection of &amp; features</p>", "author": [], "identifier": []},
    }

    assert from_doaj(entry).abstract == "Selection of & features"


def test_from_openaire_maps_every_field():
    entry = {
        "id": "openaire____::ad7636681cefebfbde101792892e3c1a",
        "type": "publication",
        "mainTitle": "CRISPR gene editing in enzyme development",
        "descriptions": ["An overview of CRISPR applications."],
        "pids": [{"scheme": "doi", "value": "10.1016/j.enzmictec.2025.110799"}],
        "authors": [{"fullName": "Youmin Zhu"}],
        "publicationDate": "2026-04-01",
        "instances": [{"urls": ["https://doi.org/10.1016/j.enzmictec.2025.110799"]}],
    }

    candidate = from_openaire(entry)

    assert candidate.external_ids == {"openaire": "openaire____::ad7636681cefebfbde101792892e3c1a"}
    assert candidate.sources == ("openaire",)
    assert candidate.doi == "10.1016/j.enzmictec.2025.110799"
    assert candidate.title == "CRISPR gene editing in enzyme development"
    assert candidate.authors == ["Youmin Zhu"]
    assert candidate.year == 2026
    assert candidate.abstract == "An overview of CRISPR applications."
    assert candidate.pdf_urls == []  # instances[].urls are landing pages, never a direct PDF (confirmed live)


def test_from_openaire_with_no_doi_description_or_authors_maps_without_them():
    entry = {
        "id": "x" * 32, "type": "publication", "mainTitle": "Bare", "descriptions": None, "pids": None,
        "authors": None, "publicationDate": None, "instances": None,
    }

    candidate = from_openaire(entry)

    assert candidate.doi is None
    assert candidate.abstract is None
    assert candidate.authors == []
    assert candidate.year is None
    assert candidate.external_ids == {"openaire": "x" * 32}


def test_from_openaire_strips_jats_markup_from_the_abstract():
    entry = {
        "id": "x" * 32, "type": "publication", "mainTitle": "X",
        "descriptions": ["<jats:title>Abstract</jats:title> <jats:p>An overview.</jats:p>"],
    }

    assert from_openaire(entry).abstract == "Abstract An overview."


def test_from_openaire_filters_non_publication_types():
    """type=publication is sent on every request (Review Focus #3), but this is defense in depth in
    case a non-publication record (software, dataset) ever slips through -- matching from_acm_dl's own
    None-filtering precedent for a non-paper Crossref type."""
    entry = {"id": "y" * 32, "type": "software", "mainTitle": "Not a paper"}

    assert from_openaire(entry) is None


def test_openaire_id_pattern_accepts_a_hex_prefix_not_just_letters():
    """Real OpenAIRE ids aren't always letters-and-underscores before the "::" (confirmed live:
    e.g. "06cdd3ff4700::..."), not just "openaire____"/"lens_dedup__" -- this pattern must accept both
    shapes."""
    pattern = SOURCE_ID_PATTERNS["openaire"]

    assert pattern.fullmatch("06cdd3ff4700::0174e8f8b12ef0b96659eaf8451882d5")
    assert pattern.fullmatch("openaire____::ad7636681cefebfbde101792892e3c1a")
    assert not pattern.fullmatch("abc")
    assert not pattern.fullmatch("0" * 32)  # a bare DOAJ-shaped id, no "::" separator, must not match


def test_from_iacr_eprint_maps_every_field():
    entry = {
        "identifier": "oai:eprint.iacr.org:2026/2368",
        "datestamp": "2026-10-05T23:52:42Z",
        "title": "Low-Latency Parallel Digit Decomposition",
        "creators": ["Jung Hee Cheon", "Kyungah Cho"],
        "description": "An overview of digit decomposition.",
    }

    candidate = from_iacr_eprint(entry)

    assert candidate.external_ids == {"iacr_eprint": "2026/2368"}
    assert candidate.sources == ("iacr_eprint",)
    assert candidate.title == "Low-Latency Parallel Digit Decomposition"
    assert candidate.authors == ["Jung Hee Cheon", "Kyungah Cho"]
    assert candidate.year == 2026
    assert candidate.abstract == "An overview of digit decomposition."
    assert candidate.doi is None
    assert candidate.pdf_urls == []  # IACR's own PDF link construction is not attempted in this plan


def test_from_iacr_eprint_with_no_creators_or_description_maps_without_them():
    """`year` comes from the eprint id's own 4-digit prefix, not `datestamp` (final review fix 4) -- so
    it's always present even when datestamp is None."""
    entry = {
        "identifier": "oai:eprint.iacr.org:2026/0003", "datestamp": None, "title": "Bare", "creators": [],
        "description": None,
    }

    candidate = from_iacr_eprint(entry)

    assert candidate.authors == []
    assert candidate.abstract is None
    assert candidate.year == 2026
    assert candidate.external_ids == {"iacr_eprint": "2026/0003"}


def test_from_biorxiv_entry_maps_every_field():
    entry = {
        "title": "A Preprint About Lattices",
        "authors": "Harvey, D. Y.; Stoll, H.; Hamilton, R. H.",
        "doi": "10.1101/2026.10.01.000001",
        "date": "2026-10-01",
        "abstract": "An overview of lattices.",
        "published": "NA",
        "server": "bioRxiv",
    }

    candidate = from_biorxiv_entry(entry, server="biorxiv")

    assert candidate.title == "A Preprint About Lattices"
    assert candidate.authors == ["Harvey, D. Y.", "Stoll, H.", "Hamilton, R. H."]
    assert candidate.year == 2026
    assert candidate.doi == "10.1101/2026.10.01.000001"
    assert candidate.external_ids == {"biorxiv": "10.1101/2026.10.01.000001"}
    assert candidate.abstract == "An overview of lattices."
    assert candidate.sources == ("biorxiv",)
    assert candidate.pdf_urls == []  # never attempted -- this plan's own scope is getting it searchable


def test_from_biorxiv_entry_for_medrxiv_uses_the_medrxiv_key():
    entry = {
        "title": "A medRxiv Study", "authors": "Last, First", "doi": "10.1101/2026.10.01.000002",
        "date": "2026-10-01", "abstract": "An abstract.", "published": "NA", "server": "medRxiv",
    }

    candidate = from_biorxiv_entry(entry, server="medrxiv")

    assert candidate.external_ids == {"medrxiv": "10.1101/2026.10.01.000002"}
    assert candidate.sources == ("medrxiv",)


def test_from_biorxiv_entry_with_no_abstract_or_unparseable_date_maps_without_them():
    entry = {
        "title": "Bare", "authors": "", "doi": "10.1101/2026.10.01.000003",
        "date": "", "abstract": None, "published": "NA", "server": "bioRxiv",
    }

    candidate = from_biorxiv_entry(entry, server="biorxiv")

    assert candidate.authors == []
    assert candidate.abstract is None
    assert candidate.year is None
    assert candidate.external_ids == {"biorxiv": "10.1101/2026.10.01.000003"}


def test_from_biorxiv_wraps_the_shared_mapper_for_the_registry():
    entry = {
        "title": "A Preprint", "authors": "Last, First", "doi": "10.1101/2026.10.01.000004",
        "date": "2026-10-01", "abstract": "An abstract.", "published": "NA", "server": "bioRxiv",
    }

    assert from_biorxiv(entry) == from_biorxiv_entry(entry, server="biorxiv")


def test_from_medrxiv_wraps_the_shared_mapper_for_the_registry():
    entry = {
        "title": "A Study", "authors": "Last, First", "doi": "10.1101/2026.10.01.000005",
        "date": "2026-10-01", "abstract": "An abstract.", "published": "NA", "server": "medRxiv",
    }

    assert from_medrxiv(entry) == from_biorxiv_entry(entry, server="medrxiv")
