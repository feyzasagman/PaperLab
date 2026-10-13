// @vitest-environment jsdom
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ScreeningAssist } from './ScreeningAssist'

// No shared renderWithClient helper exists yet — copied from SearchTab.test.tsx's own wrapper.
function renderWithClient(ui: React.ReactElement, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return { client, ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) }
}

const state = (over = {}) => ({
  criteria: 'RCTs on sleep', research_question: null, inclusion_criteria: 'RCTs on sleep', exclusion_criteria: null,
  prescreen_after_scheduled_runs: true, auto_import_min_relevance: null, ranked_used: false, suggest_status: 'idle', suggest_done: 0, suggest_total: 0,
  suggest_error: null, model_label: 'Ollama · qwen3:8b', model_is_local: true, model_host: 'localhost', ...over,
})

function stubFetch(routes: Record<string, (init?: RequestInit) => unknown>) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? 'GET'} ${new URL(url, 'http://x').pathname}`
    const body = routes[key]?.(init)
    return new Response(JSON.stringify(body ?? {}), { status: body === undefined ? 404 : 200 })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => vi.unstubAllGlobals())
const base = '/api/workspaces/w1/search'

describe('ScreeningAssist', () => {
  it('saves the research question and both criteria with one PUT', async () => {
    const fetchMock = stubFetch({ [`GET ${base}/screening`]: () => state({ criteria: null }), [`PUT ${base}/screening`]: () => state() })
    renderWithClient(<ScreeningAssist workspaceId="w1" />)
    fireEvent.change(await screen.findByLabelText('Research question'), { target: { value: 'Does sleep help memory?' } })
    fireEvent.change(screen.getByLabelText('Include when…'), { target: { value: 'RCTs' } })
    fireEvent.change(screen.getByLabelText('Exclude when…'), { target: { value: 'animal studies' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save criteria' }))
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `${base}/screening`,
        expect.objectContaining({
          method: 'PUT',
          body: JSON.stringify({
            research_question: 'Does sleep help memory?', inclusion_criteria: 'RCTs', exclusion_criteria: 'animal studies',
          }),
        }),
      ),
    )
  })

  it('saves the pre-screen switch and the auto-import choice on their own', async () => {
    const fetchMock = stubFetch({ [`GET ${base}/screening`]: () => state(), [`PUT ${base}/screening`]: () => state() })
    renderWithClient(<ScreeningAssist workspaceId="w1" />)
    fireEvent.click(await screen.findByRole('switch', { name: 'Pre-screen new hits after a scheduled search (local model only)' }))
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(`${base}/screening`, expect.objectContaining({ body: '{"prescreen_after_scheduled_runs":false}' })),
    )
    fireEvent.change(screen.getByLabelText('Import free PDFs automatically for hits rated'), { target: { value: '4' } })
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(`${base}/screening`, expect.objectContaining({ body: '{"auto_import_min_relevance":4}' })),
    )
    expect(screen.getByText('Off by default. Only free PDFs are imported; your decisions stay yours.')).toBeInTheDocument()
  })

  it('shows the server message when criteria cannot change while suggestions run', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) =>
      init?.method === 'PUT'
        ? new Response(JSON.stringify({ detail: 'Stop the suggestions before changing the criteria' }), { status: 409 })
        : new Response(JSON.stringify(state({ criteria: null })), { status: 200 })))
    renderWithClient(<ScreeningAssist workspaceId="w1" />)
    fireEvent.change(await screen.findByLabelText('Research question'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save criteria' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Stop the suggestions before changing the criteria')
  })

  it('starts suggestions without asking for a local model', async () => {
    const confirm = vi.spyOn(window, 'confirm')
    const fetchMock = stubFetch({ [`GET ${base}/screening`]: () => state(), [`POST ${base}/suggestions`]: () => state({ suggest_status: 'running' }) })
    renderWithClient(<ScreeningAssist workspaceId="w1" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Suggest for unscreened hits' }))
    expect(confirm).not.toHaveBeenCalled()
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(`${base}/suggestions`, expect.objectContaining({ body: '{"confirm_remote":false}' })),
    )
  })

  it('asks before sending abstracts to a cloud model', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const fetchMock = stubFetch({ [`GET ${base}/screening`]: () => state({ model_is_local: false, model_host: 'api.openai.com' }) })
    renderWithClient(<ScreeningAssist workspaceId="w1" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Suggest for unscreened hits' }))
    expect(confirm).toHaveBeenCalledWith('Abstracts will be sent to api.openai.com and may cost money. Continue?')
    expect(fetchMock).not.toHaveBeenCalledWith(`${base}/suggestions`, expect.anything())
  })

  it('shows progress and a Stop button while running', async () => {
    stubFetch({ [`GET ${base}/screening`]: () => state({ suggest_status: 'running', suggest_done: 140, suggest_total: 2710 }) })
    renderWithClient(<ScreeningAssist workspaceId="w1" />)
    expect(await screen.findByText('Suggesting… 140 / 2710')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument()
  })

  it('disables Suggest when no chat model', async () => {
    stubFetch({ [`GET ${base}/screening`]: () => state({ model_label: null, model_is_local: null, model_host: null }) })
    renderWithClient(<ScreeningAssist workspaceId="w1" />)
    expect(await screen.findByRole('button', { name: 'Suggest for unscreened hits' })).toBeDisabled()
    expect(screen.getByText('Set up a chat model in Settings to get suggestions.')).toBeInTheDocument()
  })

  it('disables Suggest until criteria are saved, and shows the last error', async () => {
    stubFetch({ [`GET ${base}/screening`]: () => state({ criteria: null, inclusion_criteria: null, suggest_error: "Can't reach localhost:11434" }) })
    renderWithClient(<ScreeningAssist workspaceId="w1" />)
    expect(await screen.findByRole('button', { name: 'Suggest for unscreened hits' })).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent("Can't reach localhost:11434")
  })

  it('keeps the panel open when nothing is saved and folds it, with the question in its summary, when criteria exist', async () => {
    stubFetch({ [`GET ${base}/screening`]: () => state({ inclusion_criteria: null, criteria: null }) })
    const first = renderWithClient(<ScreeningAssist workspaceId="w1" />)
    await screen.findByText('Screening criteria')
    expect(document.querySelector('details')).toHaveProperty('open', true)
    first.unmount()
    stubFetch({ [`GET ${base}/screening`]: () => state({ research_question: 'Does sleep help memory?' }) })
    renderWithClient(<ScreeningAssist workspaceId="w1" />)
    await screen.findByText('Screening criteria')
    expect(document.querySelector('summary')).toHaveTextContent('Does sleep help memory?')
    expect(document.querySelector('details')).toHaveProperty('open', false)
    expect(screen.getByRole('button', { name: 'Suggest for unscreened hits' })).toBeEnabled()
  })

  it('does not fold the panel after the first criteria are saved', async () => {
    let saved = false
    stubFetch({
      [`GET ${base}/screening`]: () => state({ inclusion_criteria: saved ? 'RCTs' : null, criteria: null }),
      [`PUT ${base}/screening`]: () => { saved = true; return state({ inclusion_criteria: 'RCTs' }) },
    })
    renderWithClient(<ScreeningAssist workspaceId="w1" />)
    fireEvent.change(await screen.findByLabelText('Include when…'), { target: { value: 'RCTs' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save criteria' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Suggest for unscreened hits' })).toBeEnabled())
    expect(document.querySelector('details')).toHaveProperty('open', true)
  })

  it('needs a research question or inclusion criteria: exclusion alone is not enough', async () => {
    stubFetch({ [`GET ${base}/screening`]: () => state({ inclusion_criteria: null, exclusion_criteria: 'animal studies', criteria: null }) })
    renderWithClient(<ScreeningAssist workspaceId="w1" />)
    expect(await screen.findByRole('button', { name: 'Suggest for unscreened hits' })).toBeDisabled()
    expect(screen.getByText('Add a research question or inclusion criteria first.')).toBeInTheDocument()
  })
})
