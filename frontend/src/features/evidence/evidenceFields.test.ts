import { expect, test } from 'vitest'
import type { EvidenceCard } from '@/api/client'
import { cellView, editChange, failureReason } from './evidenceFields'

const item = (value: string | null, extra = {}) => ({ value, quote: value ? 'the quote here' : null, page: 3, chunk_id: 'c1', flags: [], ...extra })
const card = (fields: Record<string, unknown>): EvidenceCard =>
  ({ paper_id: 'p1', status: 'ready', error: null, provenance: 'llm', fields }) as EvidenceCard

test('a list cell shows its first value and counts the rest', () => {
  const view = cellView(card({ findings: [item('A'), item('B'), item('C')] }), 'findings')
  expect(view.shown.map((s) => s.text)).toEqual(['A', 'B', 'C'])
  expect(view.leftOut).toBe(0)
})

test('an unsupported item is counted, never shown', () => {
  const dropped = { value: null, quote: null, page: null, chunk_id: null, flags: ['unsupported'] }
  const view = cellView(card({ findings: [item('A'), dropped, { ...dropped, flags: ['number_not_in_source'] }, { ...dropped, flags: ['no_statistic'] }] }), 'findings')
  expect(view.shown.map((s) => s.text)).toEqual(['A'])
  expect(view.leftOut).toBe(3)
})

test('a cleared item has a quote but no value: not shown, not "left out"', () => {
  const view = cellView(card({ design: item(null, { quote: 'still quoted', flags: ['edited'] }) }), 'design')
  expect(view.shown).toEqual([])
  expect(view.leftOut).toBe(0)
})

test('a sample reads as size and population, and flags edits', () => {
  const sample = { n: 120, population: 'adults', quote: 'q q q', page: 2, chunk_id: 'c', flags: ['edited'] }
  const view = cellView(card({ sample }), 'sample')
  expect(view.shown[0].text).toBe('120 · adults')
  expect(view.edited).toBe(true)
})

test('edit changes name the item index for lists only', () => {
  expect(editChange('findings', 2, 'X')).toEqual({ field: 'findings', index: 2, values: { value: 'X' } })
  expect(editChange('design', 0, 'X')).toEqual({ field: 'design', values: { value: 'X' } })
  expect(editChange('sample', 0, 'adults')).toEqual({ field: 'sample', values: { population: 'adults' } })
})

test('failure reasons are plain words', () => {
  expect(failureReason('not_ready')).toMatch(/isn't ready/)
  expect(failureReason('no_text')).toMatch(/no readable text/)
  expect(failureReason('model_error')).toMatch(/try again/i)
})
