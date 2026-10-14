import { describe, expect, it } from 'vitest'
import type { Paper } from '@/api/client'
import { toForm, toUpdate } from './paperForm'

const paper = {
  title: 'BERT: Pre-training of Deep',
  authors: ['Jacob Devlin', 'Ming-Wei Chang'],
  year: 2019,
  venue: null,
  doi: '10.18653/v1/n19-1423',
  abstract: 'We introduce a new language representation model.',
  is_retracted: false,
  source_type: null,
  peer_reviewed: 'unknown',
} as Paper

describe('toForm', () => {
  it('puts one author per line and blanks what is unknown', () => {
    expect(toForm(paper)).toEqual({
      title: 'BERT: Pre-training of Deep',
      authors: 'Jacob Devlin\nMing-Wei Chang',
      year: '2019',
      venue: '',
      doi: '10.18653/v1/n19-1423',
      abstract: 'We introduce a new language representation model.',
      isRetracted: false,
      sourceType: '',
      peerReviewed: 'unknown',
    })
  })
})

describe('toUpdate', () => {
  it('is empty when nothing changed, whitespace included', () => {
    const form = toForm(paper)
    expect(toUpdate(paper, { ...form, title: ` ${form.title} `, authors: `${form.authors}\n\n` })).toEqual({})
  })

  it('sends only the changed fields, authors trimmed and without blank lines', () => {
    const form = { ...toForm(paper), title: 'BERT', authors: ' Jacob Devlin \n\nKenton Lee', isRetracted: true }
    expect(toUpdate(paper, form)).toEqual({ title: 'BERT', authors: ['Jacob Devlin', 'Kenton Lee'], is_retracted: true })
  })

  it('clears year, venue and doi when they are blanked, and sets them when filled', () => {
    expect(toUpdate(paper, { ...toForm(paper), year: '', doi: ' ' })).toEqual({ year: null, doi: null })
    expect(toUpdate(paper, { ...toForm(paper), year: '2018', venue: 'NAACL' })).toEqual({ year: 2018, venue: 'NAACL' })
  })

  it('does not treat an untouched but padded stored value as a change', () => {
    const padded = {
      ...paper,
      title: '  BERT: Pre-training of Deep  ',
      venue: ' NAACL ',
      doi: ' 10.18653/v1/n19-1423 ',
      abstract: ' We introduce a new language representation model. ',
    } as Paper
    expect(toUpdate(padded, toForm(padded))).toEqual({})
  })

  // Abstract corrections use the same changed-fields rule as venue/doi.
  it('sends the abstract when changed, clears it when blanked, and omits it unchanged', () => {
    expect(toUpdate(paper, { ...toForm(paper), abstract: 'A revised abstract.' })).toEqual({
      abstract: 'A revised abstract.',
    })
    expect(toUpdate(paper, { ...toForm(paper), abstract: '   ' })).toEqual({ abstract: null })
    expect(toUpdate(paper, { ...toForm(paper), abstract: `${paper.abstract}\n` })).toEqual({})
  })
})

describe('source type and peer review', () => {
  it('sends a changed source type and peer review, and null when the type is set back to Not set', () => {
    const form = { ...toForm(paper), sourceType: 'preprint' as const, peerReviewed: 'false' as const }
    expect(toUpdate(paper, form)).toEqual({ source_type: 'preprint', peer_reviewed: 'false' })
    const typed = { ...paper, source_type: 'preprint' } as Paper
    expect(toUpdate(typed, { ...toForm(typed), sourceType: '' })).toEqual({ source_type: null })
  })
})
