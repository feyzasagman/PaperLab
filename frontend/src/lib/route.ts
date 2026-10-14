import { useSyncExternalStore } from 'react'
import { withViewTransition } from '@/components/motion'

export type ReaderTab = 'notes' | 'chat' | 'data' | 'similar' | 'references' | 'evidence'
export type WorkspaceTab = 'papers' | 'notes' | 'chat' | 'search' | 'acquisition' | 'screening' | 'prisma' | 'reading' | 'evidence'
/** Settings is one page per section, so a section can be linked to, reloaded and kept in the window's history. */
export type SettingsSection = 'models' | 'sources' | 'search' | 'desktop' | 'claude'
export type Rect = [number, number, number, number]
/** A grid cell to scroll to and focus once, when a chart point of data typed in is opened. */
export type CellFocus = { rowId: string; columnId: string }
/** What the reader shows once on load, then drops from the hash. `id` tells one target from the next. */
export type ReaderTarget =
  | { kind: 'chunk'; id: string; page: number }
  | { kind: 'note'; id: string }
  | { kind: 'region'; id: string; page: number; rects: Rect[] }
  | null
export type Route =
  | { name: 'library' }
  | { name: 'reader'; paperId: string; tab: ReaderTab; target: ReaderTarget }
  // `runId` is the active search run, round-tripped through the URL (I1) so a page reload doesn't lose track of
  // it — null when there's none, same convention as `target` above.
  | { name: 'workspace'; workspaceId: string; tab: WorkspaceTab; runId: string | null }
  | { name: 'charts' }
  | { name: 'chart'; chartId: string }
  | { name: 'chart-builder'; chartId: string | null; datasetId: string | null }
  | { name: 'dataset'; datasetId: string; focus: CellFocus | null }
  | { name: 'settings'; section: SettingsSection }
  | { name: 'connect-claude' }
  | { name: 'graph' }
  | { name: 'notes'; paper: string | null } // a paper id, 'none' for no paper, or null for every note
  | { name: 'references'; workspaceId: string | null } // a workspace id, or null for the whole library
  | { name: 'setup' }

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const HASH = /^#\/(papers|workspaces)\/([0-9a-f-]{36})(?:\?(.*))?$/i
const CHARTS_HASH = /^#\/charts(?:\/(new|[0-9a-f-]{36})(\/edit)?)?(?:\?(.*))?$/i
const DATASET_HASH = /^#\/datasets\/([0-9a-f-]{36})(?:\?(.*))?$/i
const SETTINGS_HASH = '#/settings'
const SETTINGS_SECTION_HASH = /^#\/settings\/([a-z]+)$/
const SETTINGS_SECTIONS: readonly SettingsSection[] = ['models', 'sources', 'search', 'desktop', 'claude']
const CONNECT_CLAUDE_HASH = '#/connect-claude'
const GRAPH_HASH = '#/graph'
const NOTES_HASH = /^#\/notes(?:\?(.*))?$/i
const REFERENCES_HASH = /^#\/references(?:\?(.*))?$/i
const SETUP_HASH = '#/setup'

function pageOf(params: URLSearchParams): number | null {
  const page = Number(params.get('page'))
  return Number.isInteger(page) && page >= 1 ? page : null
}

function rectsOf(text: string | null): Rect[] | null {
  if (!text) return null
  const rects = text.split(';').map((part) => part.split(',').map(Number))
  return rects.every((r) => r.length === 4 && r.every(Number.isFinite)) ? (rects as Rect[]) : null
}

function readerTarget(params: URLSearchParams): ReaderTarget {
  const page = pageOf(params)
  const chunk = params.get('chunk') ?? ''
  if (UUID.test(chunk) && page !== null) return { kind: 'chunk', id: chunk, page }
  const note = params.get('note') ?? ''
  if (UUID.test(note)) return { kind: 'note', id: note }
  const rects = rectsOf(params.get('rects'))
  return rects && page !== null ? { kind: 'region', id: `${page}:${params.get('rects')}`, page, rects } : null
}

function chartsRoute(match: RegExpExecArray): Route {
  const [, target, edit, query] = match
  if (target === undefined) return { name: 'charts' }
  if (target.toLowerCase() === 'new') {
    const dataset = new URLSearchParams(query).get('dataset') ?? ''
    return { name: 'chart-builder', chartId: null, datasetId: UUID.test(dataset) ? dataset : null }
  }
  if (!UUID.test(target)) return { name: 'library' }
  return edit ? { name: 'chart-builder', chartId: target, datasetId: null } : { name: 'chart', chartId: target }
}

export function parseRoute(hash: string): Route {
  if (hash === SETTINGS_HASH) return { name: 'settings', section: 'models' }
  const settings = SETTINGS_SECTION_HASH.exec(hash)
  // An unknown section opens the first one rather than the library: the address is still Settings.
  if (settings) {
    const asked = settings[1] as SettingsSection
    return { name: 'settings', section: SETTINGS_SECTIONS.includes(asked) ? asked : 'models' }
  }
  if (hash === CONNECT_CLAUDE_HASH) return { name: 'connect-claude' }
  if (hash === GRAPH_HASH) return { name: 'graph' }
  const notes = NOTES_HASH.exec(hash)
  if (notes) {
    const paper = new URLSearchParams(notes[1]).get('paper') ?? ''
    return { name: 'notes', paper: paper === 'none' || UUID.test(paper) ? paper : null }
  }
  const references = REFERENCES_HASH.exec(hash)
  if (references) {
    const workspaceId = new URLSearchParams(references[1]).get('workspace') ?? ''
    return { name: 'references', workspaceId: UUID.test(workspaceId) ? workspaceId : null }
  }
  if (hash === SETUP_HASH) return { name: 'setup' }
  const charts = CHARTS_HASH.exec(hash)
  if (charts) return chartsRoute(charts)
  const dataset = DATASET_HASH.exec(hash)
  if (dataset) {
    if (!UUID.test(dataset[1])) return { name: 'library' }
    const params = new URLSearchParams(dataset[2])
    const [rowId, columnId] = [params.get('row') ?? '', params.get('column') ?? '']
    const focus = UUID.test(rowId) && UUID.test(columnId) ? { rowId, columnId } : null
    return { name: 'dataset', datasetId: dataset[1], focus }
  }
  const match = HASH.exec(hash)
  if (!match || !UUID.test(match[2])) return { name: 'library' }
  const params = new URLSearchParams(match[3])
  const tab = params.get('tab')
  if (match[1] === 'workspaces') {
    const run = params.get('run') ?? ''
    return {
      name: 'workspace',
      workspaceId: match[2],
      tab:
        tab === 'notes' ||
        tab === 'chat' ||
        tab === 'search' ||
        tab === 'acquisition' ||
        tab === 'screening' ||
        tab === 'prisma' ||
        tab === 'reading' ||
        tab === 'evidence'
          ? tab
          : 'papers',
      runId: UUID.test(run) ? run : null,
    }
  }
  const readerTab = tab === 'chat' || tab === 'data' || tab === 'similar' || tab === 'references' || tab === 'evidence' ? tab : 'notes'
  return { name: 'reader', paperId: match[2], tab: readerTab, target: readerTarget(params) }
}

export const readerHref = (paperId: string, tab: ReaderTab = 'notes') =>
  tab === 'notes' ? `#/papers/${paperId}` : `#/papers/${paperId}?tab=${tab}`

/** The page travels with the chunk, so the reader fetches only that page's chunks to find its rects. */
export const chunkHref = (paperId: string, chunkId: string, page: number) =>
  `#/papers/${paperId}?chunk=${chunkId}&page=${page}`

export const noteHref = (paperId: string, noteId: string) => `#/papers/${paperId}?note=${noteId}`

/** A place on a page to flash, on the Data tab: where a charted number or a captured table sits. */
export const regionHref = (paperId: string, page: number, rects: Rect[]) =>
  `#/papers/${paperId}?tab=data&page=${page}&rects=${rects.map((r) => r.join(',')).join(';')}`

/** `runId` round-trips the active search run through the URL (I1); omit it (or pass null) when there's none. */
export const workspaceHref = (workspaceId: string, tab: WorkspaceTab = 'papers', runId?: string | null) => {
  const params = new URLSearchParams()
  if (tab !== 'papers') params.set('tab', tab)
  if (runId) params.set('run', runId)
  const query = params.toString()
  return query ? `#/workspaces/${workspaceId}?${query}` : `#/workspaces/${workspaceId}`
}

export const settingsHref = SETTINGS_HASH
export const settingsSectionHref = (section: SettingsSection) => `${SETTINGS_HASH}/${section}`
export const connectClaudeHref = CONNECT_CLAUDE_HASH
export const graphHref = GRAPH_HASH
/** The Notes page: every note, one paper's (its id), or the notes on no paper ('none'). */
export const notesHref = (paper?: string | null) => (paper ? `#/notes?paper=${paper}` : '#/notes')
/** The References page: the whole library, or one workspace's To read and shared references. */
export const referencesHref = (workspaceId?: string | null) =>
  workspaceId ? `#/references?workspace=${workspaceId}` : '#/references'
export const setupHref = SETUP_HASH
export const chartsHref = '#/charts'
export const chartHref = (chartId: string) => `#/charts/${chartId}`
export const editChartHref = (chartId: string) => `#/charts/${chartId}/edit`
/** With a dataset, the builder starts from a quick chart of it. */
export const newChartHref = (datasetId?: string) => (datasetId ? `#/charts/new?dataset=${datasetId}` : '#/charts/new')
export const datasetHref = (datasetId: string, focus?: CellFocus) =>
  focus ? `#/datasets/${datasetId}?row=${focus.rowId}&column=${focus.columnId}` : `#/datasets/${datasetId}`

/** Whether two URLs show the same page: only the query (a tab, a target) differs, so the view needs no cross-fade. */
export const samePage = (oldURL: string, newURL: string) => new URL(oldURL).hash.split('?')[0] === new URL(newURL).hash.split('?')[0]

// One hashchange listener for every useRoute, so a navigation is one cross-fade however many components read the route.
// A new page cross-fades; a tab or a target within one swaps at once, since its panel fades in by itself and the page
// takes no clicks while a transition runs.
// ponytail: the snapshot reads the live hash, so a render that lands before the transition's update shows the new page
// early and that navigation skips its fade; hold the hash in the store if that ever shows.
const listeners = new Set<() => void>()
function notifyAll(event: HashChangeEvent) {
  const notify = () => listeners.forEach((listener) => listener())
  if (samePage(event.oldURL, event.newURL)) notify()
  else withViewTransition(notify)
}

function subscribe(onChange: () => void) {
  if (listeners.size === 0) window.addEventListener('hashchange', notifyAll)
  listeners.add(onChange)
  return () => {
    listeners.delete(onChange)
    if (listeners.size === 0) window.removeEventListener('hashchange', notifyAll)
  }
}

// ponytail: hash routing covers these views with a few params; adopt a router once routes nest.
export function useRoute(): Route {
  return parseRoute(useSyncExternalStore(subscribe, () => window.location.hash))
}
