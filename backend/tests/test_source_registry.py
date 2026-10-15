from app.core import source_registry as reg


def test_source_ids_preserve_d73s_trust_order():
    assert reg.SOURCE_IDS == (
        "openalex", "crossref", "semantic_scholar", "arxiv", "core", "unpaywall", "pubmed", "pmc", "europe_pmc",
        "zenodo", "hal", "acm_dl", "ssrn", "doaj", "openaire", "iacr_eprint", "biorxiv", "medrxiv", "dblp",
    )


def test_discovery_source_ids_exclude_unpaywall_but_keep_the_trust_order():
    assert reg.DISCOVERY_SOURCE_IDS == (
        "openalex", "crossref", "semantic_scholar", "arxiv", "core", "pubmed", "pmc", "europe_pmc",
        "zenodo", "hal", "acm_dl", "ssrn", "doaj", "openaire", "iacr_eprint", "biorxiv", "medrxiv", "dblp",
    )


def test_names():
    assert reg.NAMES == {
        "openalex": "OpenAlex", "crossref": "Crossref", "semantic_scholar": "Semantic Scholar", "arxiv": "arXiv",
        "core": "CORE", "unpaywall": "Unpaywall", "pubmed": "PubMed", "pmc": "PMC", "europe_pmc": "Europe PMC",
        "zenodo": "Zenodo", "hal": "HAL", "acm_dl": "ACM DL", "ssrn": "SSRN", "doaj": "DOAJ", "openaire": "OpenAIRE",
        "iacr_eprint": "IACR ePrint", "biorxiv": "bioRxiv", "medrxiv": "medRxiv", "dblp": "DBLP",
    }  # fmt: skip


def test_keyed_ids():
    assert reg.KEYED_IDS == ("openalex", "semantic_scholar", "core", "pubmed", "pmc", "zenodo")


def test_enabled_by_default_turns_everything_on_except_openalex():
    assert reg.ENABLED_BY_DEFAULT == {
        "openalex": False, "crossref": True, "semantic_scholar": True, "arxiv": True, "core": True,
        "unpaywall": True, "pubmed": True, "pmc": True, "europe_pmc": True,
        "zenodo": True, "hal": True, "acm_dl": True, "ssrn": False, "doaj": True, "openaire": True,
        "iacr_eprint": True, "biorxiv": True, "medrxiv": True, "dblp": False,
    }  # fmt: skip


def test_key_source_defaults_to_none_except_for_ssrn():
    assert reg.BY_ID["ssrn"].key_source == "openalex"
    assert all(spec.key_source is None for spec in reg.REGISTRY if spec.id != "ssrn")


def test_asks_match_todays_classify_query_kinds():
    assert {kind: set(sources) for kind, sources in reg.ASKS.items()} == {
        "title": {
            "openalex", "crossref", "arxiv", "core", "pubmed", "pmc", "europe_pmc", "zenodo", "hal", "acm_dl",
            "ssrn", "doaj", "openaire", "dblp",
        },
        "doi": {"openalex", "crossref"},
        "arxiv": {"semantic_scholar", "arxiv"},
        "openalex": {"openalex"},
    }


def test_unpaywall_has_no_ask_and_is_not_a_discovery_source():
    unpaywall = reg.BY_ID["unpaywall"]
    assert (unpaywall.ask, unpaywall.is_discovery_source) == (None, False)


def test_page_size_by_source_matches_todays_workspace_search_constant():
    assert reg.PAGE_SIZE_BY_SOURCE == {
        "openalex": 100, "crossref": 30, "arxiv": 20, "core": 20, "semantic_scholar": 75, "pubmed": 20,
        "pmc": 20, "europe_pmc": 25, "zenodo": 25, "hal": 25, "acm_dl": 30, "ssrn": 25, "doaj": 100,
        "openaire": 20, "iacr_eprint": 50, "biorxiv": 30, "medrxiv": 30, "dblp": 20,
    }  # fmt: skip


def test_page_funcs_and_mappers_cover_every_discovery_source():
    assert set(reg.PAGE_FUNCS) == set(reg.DISCOVERY_SOURCE_IDS)
    assert set(reg.MAPPERS) == set(reg.DISCOVERY_SOURCE_IDS)


def test_starting_cursor_value_only_overrides_openalex():
    assert reg.STARTING_CURSOR_VALUE == {"openalex": 1, "europe_pmc": "*", "zenodo": 1, "doaj": 1, "openaire": 1}


def test_starting_cursor_value_also_covers_europe_pmcs_opaque_cursor():
    assert reg.STARTING_CURSOR_VALUE["europe_pmc"] == "*"
