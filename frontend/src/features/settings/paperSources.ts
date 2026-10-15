import type { PaperSourceId } from '@/api/client'

/** What each source is for, under its switch in Settings → Paper sources. */
export const DESCRIPTIONS: Record<PaperSourceId, string> = {
  openalex: 'Title and DOI search, and details for the papers you upload.',
  crossref: 'Title and DOI search across papers with a DOI. Free.',
  semantic_scholar: 'Similar papers, arXiv lookups and free PDF links. Free; a free key avoids its busy shared pool.',
  arxiv: 'arXiv preprints and their PDFs. Free.',
  core: 'Open-access papers from university repositories, with PDFs. Free; a free key raises its limit.',
  unpaywall: 'Free, legal PDF links for papers with a DOI. Free; needs the contact email.',
  pubmed: 'Biomedical literature search via PubMed (NCBI). Free; a free key raises its rate limit.',
  pmc: 'Full-text biomedical literature via PMC (NCBI). Free; a free key raises its rate limit.',
  europe_pmc: 'Biomedical and life-science literature via Europe PMC. Free, no key needed.',
  zenodo: 'General-purpose open-access repository via Zenodo (CERN). Free; an optional key can be saved but isn\'t used yet.',
  hal: 'French open-access repository via HAL. Free, no key needed.',
  acm_dl: 'ACM Digital Library papers, via Crossref. Free, no key needed.',
  ssrn: 'Working papers and preprints via SSRN (an Elsevier company), via OpenAlex. Shares OpenAlex\'s own usage budget and key below.',
  doaj: 'Peer-reviewed open-access journal articles via the Directory of Open Access Journals. Free, no key needed.',
  openaire: 'Open-access research and data via OpenAIRE, aggregating European repositories. Free, no key needed.',
  iacr_eprint: 'Cryptology preprints from the IACR ePrint Archive, synced periodically in the background. Free, no key needed.',
  biorxiv: 'Biology preprints from bioRxiv, synced periodically in the background. Free, no key needed.',
  medrxiv: 'Medical preprints from medRxiv, synced periodically in the background. Free, no key needed.',
  dblp: 'Computer-science publications, venues and authors. Free, no key needed; no PDFs.',
}

/** The "May cost money" badge plus its price line, for sources that can charge even without a key. Only OpenAlex
 * today; a future gated/paid source (Scopus, Web of Science, IEEE Xplore) adds its own entry here instead of a
 * new hardcoded conditional in PaperSourceRow. */
export const PRICE_WARNINGS: Partial<Record<PaperSourceId, string>> = {
  openalex:
    'Free up to $0.10 of use a day without a key, or $1 a day with a free key from openalex.org. More needs a paid plan there.',
  ssrn: 'Uses OpenAlex\'s own budget and key (set above under OpenAlex) -- off by default for the same reason OpenAlex is.',
}

/** A short hint shown under a source's description when something besides its own switch must be true for it to
 * actually run — today, only Unpaywall (needs the contact email). Returns null when there is nothing to show. */
export const PREREQUISITE_HINTS: Partial<Record<PaperSourceId, (hasEmail: boolean) => string | null>> = {
  unpaywall: (hasEmail) => (hasEmail ? null : 'Add a contact email to use Unpaywall.'),
}

// The server's rule for the contact email (core/paper_sources.py), so a typo is caught before it is sent.
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/
const MAX_EMAIL_LENGTH = 254
const EMAIL_PROBLEM = 'Enter an email address like name@example.org.'

/**
 * Why the server would refuse `text` as the contact email, in its words, or null when it would be saved. Blank text
 * is not a problem: it saves nothing (Save stays disabled), and Remove is how an email is cleared.
 */
export function emailProblem(text: string): string | null {
  const email = text.trim()
  if (email === '') return null
  // Counted in characters like Python's len(), not UTF-16 units.
  return EMAIL.test(email) && [...email].length <= MAX_EMAIL_LENGTH ? null : EMAIL_PROBLEM
}

/** "Key ending in T123", or "Key saved" for a key too short to hint (the server sends no hint for keys under 8). */
export const keyLine = (hint: string | null): string => (hint ? `Key ending in ${hint}` : 'Key saved')
