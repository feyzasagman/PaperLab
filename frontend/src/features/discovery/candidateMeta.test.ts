import { describe, expect, it } from 'vitest'
import type { Candidate } from '@/api/client'
import { candidateKey, citationsLabel, pageLink, sameCandidate } from './candidateMeta'

const ids = { doi: null, external_ids: {} }

describe('pageLink', () => {
  it('prefers the DOI, then arXiv, then OpenAlex, then Semantic Scholar, then CORE', () => {
    const all = { doi: '10.1/x', external_ids: { arxiv: '1810.04805', openalex: 'W1', semantic_scholar: 'a'.repeat(40), core: '42' } }
    expect(pageLink(all)).toBe('https://doi.org/10.1/x')
    expect(pageLink({ ...all, doi: null })).toBe('https://arxiv.org/abs/1810.04805')
    expect(pageLink({ ...all, doi: null, external_ids: { ...all.external_ids, arxiv: undefined as unknown as string } })).toBe(
      'https://openalex.org/W1',
    )
    expect(pageLink({ ...ids, external_ids: { semantic_scholar: 'a'.repeat(40), core: '42' } })).toBe(
      `https://www.semanticscholar.org/paper/${'a'.repeat(40)}`,
    )
    expect(pageLink({ ...ids, external_ids: { core: '42' } })).toBe('https://core.ac.uk/works/42')
  })

  it('is null without any identifier', () => {
    expect(pageLink(ids)).toBeNull()
  })

  it('falls back to PubMed when nothing else is set', () => {
    expect(pageLink({ ...ids, external_ids: { pubmed: '42825172' } })).toBe('https://pubmed.ncbi.nlm.nih.gov/42825172/')
  })

  it('falls back to PMC when nothing else is set', () => {
    expect(pageLink({ ...ids, external_ids: { pmc: '9876543' } })).toBe('https://pmc.ncbi.nlm.nih.gov/articles/PMC9876543/')
  })

  it('falls back to Zenodo when nothing else is set', () => {
    expect(pageLink({ ...ids, external_ids: { zenodo: '123456' } })).toBe('https://zenodo.org/records/123456')
  })

  it('falls back to HAL when nothing else is set', () => {
    expect(pageLink({ ...ids, external_ids: { hal: '4020890' } })).toBe('https://hal.science/view/index/docid/4020890')
  })

  it('falls back to DOAJ when nothing else is set', () => {
    expect(pageLink({ ...ids, external_ids: { doaj: '0'.repeat(32) } })).toBe(`https://doaj.org/article/${'0'.repeat(32)}`)
  })

  it('falls back to OpenAIRE when nothing else is set', () => {
    const id = 'openaire____::' + '0'.repeat(32)
    expect(pageLink({ ...ids, external_ids: { openaire: id } })).toBe(
      `https://explore.openaire.eu/search/publication?articleId=${encodeURIComponent(id)}`,
    )
  })

  it('falls back to IACR ePrint when nothing else is set', () => {
    expect(pageLink({ ...ids, external_ids: { iacr_eprint: '2026/2368' } })).toBe('https://eprint.iacr.org/2026/2368')
  })

  it('falls back to bioRxiv when nothing else is set', () => {
    expect(pageLink({ ...ids, external_ids: { biorxiv: '10.1101/2026.10.01.000001' } })).toBe('https://doi.org/10.1101/2026.10.01.000001')
  })

  it('falls back to medRxiv when nothing else is set', () => {
    expect(pageLink({ ...ids, external_ids: { medrxiv: '10.1101/2026.10.01.000002' } })).toBe('https://doi.org/10.1101/2026.10.01.000002')
  })
})

describe('citationsLabel', () => {
  it('groups thousands and says citation for one', () => {
    expect(citationsLabel(120985)).toBe('120,985 citations')
    expect(citationsLabel(1)).toBe('1 citation')
    expect(citationsLabel(0)).toBe('0 citations')
  })

  it('is empty when the count is unknown', () => {
    expect(citationsLabel(null)).toBe('')
  })
})

describe('candidateKey', () => {
  it('uses the first identifier, else the doi, else the position', () => {
    const candidate = (fields: Partial<Candidate>) => ({ ...ids, ...fields }) as Candidate
    expect(candidateKey(candidate({ external_ids: { openalex: 'W1' }, doi: '10.1/x' }), 0)).toBe('W1')
    expect(candidateKey(candidate({ doi: '10.1/x' }), 0)).toBe('10.1/x')
    expect(candidateKey(candidate({ external_ids: { core: '42' } }), 0)).toBe('42')
    expect(candidateKey(candidate({}), 3)).toBe('row-3')
  })
})

describe('sameCandidate', () => {
  const candidate = (fields: Partial<Candidate>) => ({ ...ids, title: 'T', ...fields }) as Candidate

  it('matches by openalex id, else doi in any case, else semantic_scholar, arxiv or core', () => {
    expect(
      sameCandidate(
        candidate({ external_ids: { openalex: 'W1' } }),
        candidate({ external_ids: { openalex: 'W1' }, doi: '10.1/other' }),
      ),
    ).toBe(true)
    expect(
      sameCandidate(candidate({ external_ids: { openalex: 'W2' } }), candidate({ external_ids: { openalex: 'W1' } })),
    ).toBe(false)
    expect(sameCandidate(candidate({ doi: '10.1/X' }), candidate({ doi: '10.1/x' }))).toBe(true)
    expect(
      sameCandidate(
        candidate({ external_ids: { semantic_scholar: 'a'.repeat(40) } }),
        candidate({ external_ids: { semantic_scholar: 'a'.repeat(40) } }),
      ),
    ).toBe(true)
    // arXiv and CORE find papers that have no other identifier.
    expect(
      sameCandidate(
        candidate({ external_ids: { arxiv: '2609.00001' } }),
        candidate({ external_ids: { arxiv: '2609.00001' } }),
      ),
    ).toBe(true)
    expect(sameCandidate(candidate({ external_ids: { core: '42' } }), candidate({ external_ids: { core: '42' } }))).toBe(true)
    expect(sameCandidate(candidate({ external_ids: { core: '43' } }), candidate({ external_ids: { core: '42' } }))).toBe(false)
  })

  it('matches by doi even when added also carries a semantic_scholar id the other row lacks', () => {
    // Pins doi ranking second (right after openalex), not last: a row that never got an S2 match must still
    // dedup against one that did, as long as they share a doi — otherwise the UI shows a stale "Add" button.
    expect(
      sameCandidate(
        candidate({ doi: '10.1/x' }),
        candidate({ doi: '10.1/x', external_ids: { semantic_scholar: 'a'.repeat(40) } }),
      ),
    ).toBe(true)
  })

  it('is false when the added candidate has no identifier to match on', () => {
    expect(sameCandidate(candidate({ external_ids: { openalex: 'W1' } }), candidate({}))).toBe(false)
  })

  it('matches by pubmed id when nothing stronger is set', () => {
    expect(
      sameCandidate(candidate({ external_ids: { pubmed: '42825172' } }), candidate({ external_ids: { pubmed: '42825172' } })),
    ).toBe(true)
    expect(
      sameCandidate(candidate({ external_ids: { pubmed: '1' } }), candidate({ external_ids: { pubmed: '2' } })),
    ).toBe(false)
  })

  it('matches by pmc id when nothing stronger is set', () => {
    expect(
      sameCandidate(candidate({ external_ids: { pmc: '9876543' } }), candidate({ external_ids: { pmc: '9876543' } })),
    ).toBe(true)
    expect(
      sameCandidate(candidate({ external_ids: { pmc: '1' } }), candidate({ external_ids: { pmc: '2' } })),
    ).toBe(false)
  })

  it('matches by zenodo id when nothing stronger is set', () => {
    expect(
      sameCandidate(candidate({ external_ids: { zenodo: '123456' } }), candidate({ external_ids: { zenodo: '123456' } })),
    ).toBe(true)
    expect(
      sameCandidate(candidate({ external_ids: { zenodo: '1' } }), candidate({ external_ids: { zenodo: '2' } })),
    ).toBe(false)
  })
})


it('links DBLP metadata-only candidates to their record', () => {
  expect(pageLink({ doi: null, external_ids: { dblp: 'conf/nips/VaswaniSPUJGKP17' } }))
    .toBe('https://dblp.org/rec/conf/nips/VaswaniSPUJGKP17')
})
