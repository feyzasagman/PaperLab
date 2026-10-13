// @vitest-environment jsdom
import { render, screen, fireEvent, within, cleanup } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import * as queries from '@/api/queries'
import type { SavedSearch } from '@/api/client'
import { SavedSearches } from './SavedSearches'

const row: SavedSearch = {
  id: 's1', workspace_id: 'w1', query: 'llm code review', sources: ['arxiv'], year_from: 2022, year_to: null,
  schedule: '0 2 * * *', timezone: 'Europe/Berlin', schedule_kind: 'daily', time: '02:00', weekday: null,
  enabled: true, last_run_at: null, next_run_at: '2026-10-14T00:00:00Z', last_run_id: null, last_run_status: null,
  last_error: null, created_at: '2026-10-01T00:00:00Z',
}

const mutation = () => ({ mutate: vi.fn(), mutateAsync: vi.fn().mockResolvedValue(row), isPending: false, error: null })

function setup(rows: SavedSearch[], onRunStarted = vi.fn()) {
  const update = mutation(), del = mutation(), create = mutation(), runNow = mutation()
  vi.spyOn(queries, 'useSavedSearches').mockReturnValue({ data: rows } as any)
  vi.spyOn(queries, 'useUpdateSavedSearch').mockReturnValue(update as any)
  vi.spyOn(queries, 'useDeleteSavedSearch').mockReturnValue(del as any)
  vi.spyOn(queries, 'useCreateSavedSearch').mockReturnValue(create as any)
  vi.spyOn(queries, 'useRunSavedSearchNow').mockReturnValue(runNow as any)
  vi.spyOn(queries, 'usePaperSources').mockReturnValue({
    data: { contact_email: null, sources: [{ id: 'arxiv', name: 'arXiv', enabled: true, has_key: null, key_hint: null }] },
  } as any)
  render(<SavedSearches workspaceId="w1" currentQuery="typed query" onRunStarted={onRunStarted} />)
  return { update, del, create, runNow }
}

// Radix Select measures with ResizeObserver and scrolls items into view; jsdom has neither.
globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} }
Element.prototype.scrollIntoView ??= () => {}
Element.prototype.hasPointerCapture ??= () => false

afterEach(() => vi.restoreAllMocks())

test('empty workspace offers Save this search', () => {
  setup([])
  expect(screen.getByRole('button', { name: /Save this search/ })).toBeTruthy()
})

test('a row shows query, schedule in words, next run and a named switch; toggling pauses it', () => {
  const { update } = setup([row])
  expect(screen.getByText('llm code review')).toBeTruthy()
  expect(screen.getByText(/Daily at 02:00 · Europe\/Berlin/)).toBeTruthy()
  expect(screen.getByText(/Next run/)).toBeTruthy()
  fireEvent.click(screen.getByRole('switch', { name: /Enabled/ }))
  expect(update.mutate).toHaveBeenCalledWith({ id: 's1', body: { enabled: false } })
})

test('a paused row says Paused', () => {
  setup([{ ...row, enabled: false }])
  expect(screen.getByText('Paused')).toBeTruthy()
})

test('Delete asks in the app dialog and confirming deletes', () => {
  const confirm = vi.spyOn(window, 'confirm')
  const { del } = setup([row])
  fireEvent.click(screen.getByRole('button', { name: 'Delete: llm code review' }))
  expect(del.mutate).not.toHaveBeenCalled()
  const dialog = screen.getByRole('dialog')
  fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }))
  expect(del.mutate).toHaveBeenCalledWith('s1')
  expect(confirm).not.toHaveBeenCalled()
})

async function pickSchedule(name: string) {
  fireEvent.keyDown(screen.getByRole('combobox', { name: 'Schedule' }), { key: 'Enter' })
  fireEvent.click(await screen.findByRole('option', { name }))
}
const openNew = () => fireEvent.click(screen.getByRole('button', { name: /Save this search/ }))
const save = () => fireEvent.click(screen.getByRole('button', { name: 'Save' }))

test('daily create body: time set, weekday and cron null, no sources ticked means []', () => {
  const { create } = setup([])
  openNew()
  save()
  expect(create.mutateAsync).toHaveBeenCalledWith({
    query: 'typed query', sources: [], year_from: null, year_to: null, schedule_kind: 'daily',
    time: '02:00', weekday: null, cron: null, timezone: expect.any(String),
  })
})

test('ticked sources are sent by id', () => {
  const { create } = setup([])
  openNew()
  fireEvent.click(screen.getByRole('checkbox', { name: 'arXiv' }))
  save()
  expect(create.mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ sources: ['arxiv'] }))
})

test('custom body: cron set, time and weekday null', async () => {
  const { create } = setup([])
  openNew()
  await pickSchedule('Custom')
  fireEvent.change(screen.getByLabelText('Cron'), { target: { value: '0 2 * * 1-5' } })
  save()
  expect(create.mutateAsync).toHaveBeenCalledWith(
    expect.objectContaining({ schedule_kind: 'custom', cron: '0 2 * * 1-5', time: null, weekday: null }),
  )
})

test('edit sends {id, body} with non-null query, sources and timezone, and no enabled', () => {
  const { update } = setup([row])
  fireEvent.click(screen.getByRole('button', { name: 'Edit: llm code review' }))
  save()
  const [arg] = update.mutateAsync.mock.calls[0]
  expect(arg.id).toBe('s1')
  expect(arg.body).toEqual(expect.objectContaining({ query: 'llm code review', sources: ['arxiv'], timezone: 'Europe/Berlin', year_from: 2022, year_to: null }))
  expect(arg.body).not.toHaveProperty('enabled')
})

test('a server code shows as a readable message in the dialog', () => {
  const { create } = setup([])
  vi.spyOn(queries, 'useCreateSavedSearch').mockReturnValue({ ...create, error: new Error('invalid_schedule') } as any)
  cleanup()
  render(<SavedSearches workspaceId="w1" currentQuery="q" onRunStarted={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: /Save this search/ }))
  expect(screen.getByRole('dialog').textContent).toMatch(/schedule is not valid/)
})

test('Run now reports the new run id and is disabled while pending', () => {
  const onRunStarted = vi.fn()
  const { runNow } = setup([row], onRunStarted)
  fireEvent.click(screen.getByRole('button', { name: 'Run now: llm code review' }))
  const [id, opts] = runNow.mutate.mock.calls[0]
  expect(id).toBe('s1')
  opts.onSuccess({ id: 'run9' })
  expect(onRunStarted).toHaveBeenCalledWith('run9')
})

test('Run now is disabled while pending', () => {
  setup([row])
  vi.spyOn(queries, 'useRunSavedSearchNow').mockReturnValue({ ...mutation(), isPending: true } as any)
  cleanup()
  render(<SavedSearches workspaceId="w1" currentQuery="" onRunStarted={vi.fn()} />)
  for (const b of screen.getAllByRole('button', { name: 'Run now: llm code review' })) expect((b as HTMLButtonElement).disabled).toBe(true)
})

test('a failed Run now shows its error', () => {
  setup([row])
  vi.spyOn(queries, 'useRunSavedSearchNow').mockReturnValue({ ...mutation(), error: new Error('saved_search_not_found') } as any)
  cleanup()
  render(<SavedSearches workspaceId="w1" currentQuery="" onRunStarted={vi.fn()} />)
  expect(screen.getByRole('alert').textContent).toMatch(/no longer exists/)
})

test('Next run shows next_run_at in local time', () => {
  setup([row])
  const expected = new Date(row.next_run_at!).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
  expect(screen.getByText(`Next run: ${expected}`)).toBeTruthy()
})

test('Weekly shows a weekday select and creates a weekly search', async () => {
  const { create } = setup([])
  fireEvent.click(screen.getByRole('button', { name: /Save this search/ }))
  expect((screen.getByLabelText('Query') as HTMLInputElement).value).toBe('typed query')
  expect(screen.queryByLabelText('Weekday')).toBeNull()
  fireEvent.keyDown(screen.getByRole('combobox', { name: 'Schedule' }), { key: 'Enter' })
  fireEvent.click(await screen.findByRole('option', { name: 'Weekly' }))
  expect(screen.getByRole('combobox', { name: 'Weekday' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
  expect(create.mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ query: 'typed query', schedule_kind: 'weekly', weekday: 1 }))
})
