import type { Candidate, PaperSourceId } from '@/api/client'

/** How a source is named on a found paper's badges. */
export const SOURCE_NAMES: Record<PaperSourceId, string> = {
  openalex: 'OpenAlex',
  crossref: 'Crossref',
  semantic_scholar: 'Semantic Scholar',
  arxiv: 'arXiv',
  core: 'CORE',
  unpaywall: 'Unpaywall',
  pubmed: 'PubMed',
  pmc: 'PMC',
  europe_pmc: 'Europe PMC',
  zenodo: 'Zenodo',
  hal: 'HAL',
  acm_dl: 'ACM DL',
  ssrn: 'SSRN',
  doaj: 'DOAJ',
  openaire: 'OpenAIRE',
  iacr_eprint: 'IACR ePrint',
  biorxiv: 'bioRxiv',
  medrxiv: 'medRxiv',
}

/** "1,234 citations", "1 citation", or '' when the count is unknown. */
export const citationsLabel = (count: number | null): string =>
  count === null ? '' : `${count.toLocaleString('en-US')} ${count === 1 ? 'citation' : 'citations'}`

/** "Open page" link builders, most preferred first, per source id (Phase 0b) — a new source's link needs one
 * entry here, not a new branch in pageLink itself. */
const ID_URL_BUILDERS: Record<string, (id: string) => string> = {
  arxiv: (id) => `https://arxiv.org/abs/${id}`,
  openalex: (id) => `https://openalex.org/${id}`,
  semantic_scholar: (id) => `https://www.semanticscholar.org/paper/${id}`,
  core: (id) => `https://core.ac.uk/works/${id}`,
  pubmed: (id) => `https://pubmed.ncbi.nlm.nih.gov/${id}/`,
  pmc: (id) => `https://pmc.ncbi.nlm.nih.gov/articles/PMC${id}/`,
  zenodo: (id) => `https://zenodo.org/records/${id}`,
  hal: (id) => `https://hal.science/view/index/docid/${id}`,
  doaj: (id) => `https://doaj.org/article/${id}`,
  openaire: (id) => `https://explore.openaire.eu/search/publication?articleId=${encodeURIComponent(id)}`,
  iacr_eprint: (id) => `https://eprint.iacr.org/${id}`,
  biorxiv: (id) => `https://doi.org/${id}`,
  medrxiv: (id) => `https://doi.org/${id}`,
}
/** Preference order for candidateKey/sameCandidate (pageLink has its own order, defined inline above) — doi is
 * handled separately in each since it isn't a key in external_ids. */
const ID_PRIORITY = ['openalex', 'semantic_scholar', 'arxiv', 'core', 'pubmed', 'pmc', 'zenodo', 'hal', 'doaj', 'openaire', 'iacr_eprint', 'biorxiv', 'medrxiv']

/** Where "Open page" goes: the DOI, else arXiv, OpenAlex, Semantic Scholar, CORE, then PubMed, then PMC, then
 * Zenodo, then HAL, then DOAJ, then OpenAIRE, then IACR ePrint, then bioRxiv, then medRxiv; null with no identifier. */
export function pageLink(candidate: Pick<Candidate, 'doi' | 'external_ids'>): string | null {
  if (candidate.doi) return `https://doi.org/${candidate.doi}`
  for (const source of ['arxiv', 'openalex', 'semantic_scholar', 'core', 'pubmed', 'pmc', 'zenodo', 'hal', 'doaj', 'openaire', 'iacr_eprint', 'biorxiv', 'medrxiv']) {
    const id = candidate.external_ids[source]
    if (id) return ID_URL_BUILDERS[source](id)
  }
  return null
}

/** A React key that survives a refetch: the first identifier the candidate has, else its position. */
export function candidateKey(candidate: Candidate, index: number): string {
  for (const source of ID_PRIORITY) {
    const id = candidate.external_ids[source]
    if (id) return id
  }
  return candidate.doi ?? `row-${index}`
}

/** Whether `candidate` is the paper `added` names: by openalex id, else doi (any case), semantic_scholar, arxiv,
 * core, pubmed, pmc, zenodo, hal, doaj, openaire, iacr_eprint, biorxiv or medrxiv. Doi ranks second here (not last, unlike candidateKey/pageLink) because this drives "already in your
 * library" dedup across cached search results — ranking it last reintroduced a real gap: two rows for the same
 * paper that share a doi but not a semantic_scholar id (e.g. one row never got an S2 match) would stop matching,
 * leaving a stale "Add" button that 409s on click. */
export function sameCandidate(candidate: Candidate, added: Candidate): boolean {
  if (added.external_ids.openalex) return candidate.external_ids.openalex === added.external_ids.openalex
  if (added.doi) return candidate.doi?.toLowerCase() === added.doi.toLowerCase()
  for (const source of ['semantic_scholar', 'arxiv', 'core', 'pubmed', 'pmc', 'zenodo', 'hal', 'doaj', 'openaire', 'iacr_eprint', 'biorxiv', 'medrxiv']) {
    const addedId = added.external_ids[source]
    if (addedId) return candidate.external_ids[source] === addedId
  }
  return false
}
