import type { components } from './schema'

export type Paper = components['schemas']['PaperOut']
export type PaperUpdate = components['schemas']['PaperUpdate']
export type ReadingIn = components['schemas']['ReadingIn']
export type EvidenceCard = components['schemas']['CardOut']
export type EvidenceCardRow = components['schemas']['CardRowOut']
export type EvidenceChange = components['schemas']['EditIn']['changes'][number]
export type Chunk = components['schemas']['ChunkOut']
export type Note = components['schemas']['NoteOut']
export type NoteCreate = components['schemas']['NoteCreate']
export type NoteUpdate = components['schemas']['NoteUpdate']
export type ChatSource = components['schemas']['ChatSource']
export type NoteSource = components['schemas']['NoteSource']
export type ChatAnswer = components['schemas']['ChatAnswer']
export type SavedNote = components['schemas']['SavedNoteOut']
// Prefixed: the DOM already has a global `ErrorEvent`.
export type ChatSourcesEvent = components['schemas']['SourcesEvent']
export type ChatTokenEvent = components['schemas']['TokenEvent']
export type ChatDoneEvent = components['schemas']['DoneEvent']
export type ChatErrorEvent = components['schemas']['ErrorEvent']
export type PromoteRequest = components['schemas']['PromoteRequest']
export type NoteSuggestion = components['schemas']['NoteSuggestionOut']
export type NoteSuggestionsOut = components['schemas']['NoteSuggestionsOut']
export type Workspace = components['schemas']['WorkspaceOut']
export type ChartRef = components['schemas']['ChartRefOut']
export type Dataset = components['schemas']['DatasetOut']
export type DatasetSummary = components['schemas']['DatasetSummaryOut']
export type DatasetCreate = components['schemas']['DatasetCreate']
export type GridIn = components['schemas']['GridIn']
export type TablePreview = components['schemas']['TablePreviewOut']
export type ChartUse = components['schemas']['ChartUseOut']
export type NumberCandidate = components['schemas']['NumberCandidateOut']
export type NumberCreate = components['schemas']['NumberCreate']
export type NumberAdded = components['schemas']['NumberAddedOut']
export type Chart = components['schemas']['ChartOut']
export type ChartSummary = components['schemas']['ChartSummaryOut']
export type ChartSpec = Chart['spec']
export type SeriesChartSpec = components['schemas']['SeriesChart']
export type SeriesSpec = components['schemas']['Series']
export type ResolvedData = components['schemas']['ResolvedDataOut']
export type ResolvedDataset = components['schemas']['ResolvedDatasetOut']
export type ResolvedCell = components['schemas']['ResolvedCellOut']
export type ChatModel = components['schemas']['ChatModelOut']
export type LLMConnection = components['schemas']['ConnectionOut']
export type LLMConnectionCreate = components['schemas']['ConnectionCreate']
export type LLMConnectionUpdate = components['schemas']['ConnectionUpdate']
export type LLMModel = components['schemas']['ModelOut']
export type ConnectionCheck = components['schemas']['ConnectionCheckOut']
export type AvailableModels = components['schemas']['AvailableModelsOut']
export type PullProgressEvent = components['schemas']['PullProgressEvent']
export type PullDoneEvent = components['schemas']['PullDoneEvent']
export type PullErrorEvent = components['schemas']['PullErrorEvent']
export type EmbeddingStatus = components['schemas']['EmbeddingStatusOut']
export type SearchSource = components['schemas']['SearchSourceOut']
export type SearchSourceIn = components['schemas']['SearchSourceIn']
export type Rebuild = components['schemas']['RebuildOut']
export type DownloadProgressEvent = components['schemas']['DownloadProgressEvent']
export type DownloadDoneEvent = components['schemas']['DownloadDoneEvent']
export type DownloadErrorEvent = components['schemas']['DownloadErrorEvent']
export type Candidate = components['schemas']['CandidateOut']
export type SearchResult = components['schemas']['SearchOut']
export type PaperSources = components['schemas']['PaperSourcesOut']
export type PaperSource = components['schemas']['PaperSourceOut']
export type PaperSourceId = PaperSource['id']
export type PaperSourcesUpdate = components['schemas']['PaperSourcesUpdate']
export type References = components['schemas']['ReferencesOut']
export type Reference = components['schemas']['ReferenceOut']
export type ReferencesDirection = References['direction']
export type ReferencePage = components['schemas']['ReferencePageOut']
export type RefreshOut = components['schemas']['RefreshOut']
export type McpSetup = components['schemas']['McpSetupOut']
export type McpCheck = components['schemas']['McpCheckOut']
export type SetupState = components['schemas']['SetupOut']
export type GraphNode = components['schemas']['GraphNode']
export type GraphLink = components['schemas']['GraphLink']
export type LibraryGraph = components['schemas']['GraphOut']
export type PaperLinkOut = components['schemas']['LinkOut']
export type SearchRun = components['schemas']['SearchRunOut']
export type SavedSearch = components['schemas']['SavedSearchOut']
export type SavedSearchCreate = components['schemas']['SavedSearchCreate']
export type SavedSearchUpdate = components['schemas']['SavedSearchUpdate']
export type SearchRunCreate = components['schemas']['SearchRunCreate']
// The five sources search_batch actually fans out to (backend's SearchSource Literal) — unlike PaperSourceId,
// never includes 'unpaywall', which has no search/discovery role (spec §6).
// Named SearchRunSource, not SearchSource: M25 gives the embedding search source that name instead (SearchSourceOut).
export type SearchRunSource = SearchRunCreate['sources'][number]
export type Hit = components['schemas']['HitOut']
export type HitListOut = components['schemas']['HitListOut']
export type HitReviewUpdate = components['schemas']['HitReviewUpdate']
export type BulkHitReviewUpdate = components['schemas']['BulkHitReviewUpdate']
export type BulkUpdateOut = components['schemas']['BulkUpdateOut']
export type ImportHitsOut = components['schemas']['ImportHitsOut']
export type RankedHits = components['schemas']['RankedHitsOut']
export type ClearHitsOut = components['schemas']['ClearHitsOut']
export type SnowballRequest = components['schemas']['SnowballRequest']
export type SnowballOut = components['schemas']['SnowballOut']
export type EligibilityUpdate = components['schemas']['EligibilityUpdate']
export type EligibilityOut = components['schemas']['EligibilityOut']
export type PrismaExportOut = components['schemas']['PrismaExportOut']
export type ReadingContextList = components['schemas']['ReadingContextListOut']
export type ReadingQueue = components['schemas']['ReadingQueueOut']
export type ScreeningState = components['schemas']['ScreeningStateOut']
export type ScreeningUpdate = components['schemas']['ScreeningCriteriaUpdate']
export type AcceptSuggestionsOut = components['schemas']['AcceptSuggestionsOut']

/** Where a chat lives: the reader's paper, or a workspace. */
export type ChatScope = { kind: 'paper' | 'workspace'; id: string }
const chatUrl = (scope: ChatScope) => `/api/${scope.kind === 'paper' ? 'papers' : 'workspaces'}/${scope.id}/chat`

/** The last `loc` segment of each FastAPI validation error that has one, deduplicated and in order. */
function invalidFields(errors: unknown[]): string[] {
  const fields = errors.flatMap((error) => {
    const loc = typeof error === 'object' && error !== null && 'loc' in error ? (error as { loc: unknown }).loc : null
    const field = Array.isArray(loc) ? loc.at(-1) : null
    return typeof field === 'string' ? [field] : []
  })
  return [...new Set(fields)]
}

/** A failed response's `detail` as readable text. FastAPI sends a string, or a list of validation errors. */
export async function errorDetail(response: Response): Promise<string> {
  const detail: unknown = await response.json().then((body) => body.detail, () => response.statusText)
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const fields = invalidFields(detail)
    return fields.length > 0 ? `Check these fields: ${fields.join(', ')}.` : 'Some details are invalid.'
  }
  return JSON.stringify(detail)
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) throw new Error(await errorDetail(response))
  return (response.status === 204 ? undefined : await response.json()) as T
}

export const citationsUrl = (workspaceId: string, format: string) =>
  `/api/workspaces/${encodeURIComponent(workspaceId)}/citations?format=${format}`

const sendJson = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const api = {
  listPapers: () => request<Paper[]>('/api/papers'),
  getPaper: (id: string) => request<Paper>(`/api/papers/${id}`),
  /** With a `workspaceId`, the new paper also joins that workspace. */
  uploadPaper: (file: File, workspaceId?: string) => {
    const form = new FormData()
    form.append('file', file)
    if (workspaceId) form.append('workspace_id', workspaceId)
    return request<Paper>('/api/papers', { method: 'POST', body: form })
  },
  updatePaper: (id: string, update: PaperUpdate) => request<Paper>(`/api/papers/${id}`, sendJson('PATCH', update)),
  /** The reader's own record (D118, D119): passes finished, keep/later/drop, or both. A field left out keeps its
   * value. 422 for an out-of-range pass, an unknown decision, or an empty body. */
  setReading: (id: string, changes: ReadingIn) => request<Paper>(`/api/papers/${id}/reading`, sendJson('PUT', changes)),
  deletePaper: (id: string) => request<void>(`/api/papers/${id}`, { method: 'DELETE' }),
  reingestPaper: (id: string) => request<Paper>(`/api/papers/${id}/reingest`, { method: 'POST' }),
  paperFileUrl: (id: string) => `/api/papers/${id}/file`,
  /** Papers outside the library for a title, DOI, arXiv ID or OpenAlex ID, and a notice per source that failed. */
  searchPapers: (query: string) => request<SearchResult>(`/api/discovery/search?q=${encodeURIComponent(query)}`),
  similarPapers: (paperId: string) => request<Candidate[]>(`/api/papers/${paperId}/similar`),
  /** Downloads a found paper's free PDF into the library; with a `workspaceId` it also joins that workspace. */
  addCandidate: (candidate: Candidate, workspaceId?: string) =>
    request<Paper>('/api/discovery/add', sendJson('POST', { candidate, workspace_id: workspaceId ?? null })),
  listChunks: (paperId: string, page: number) => request<Chunk[]>(`/api/papers/${paperId}/chunks?page=${page}`),
  listNotes: (paperId: string) => request<Note[]>(`/api/papers/${paperId}/notes`),
  createNote: (note: NoteCreate) => request<Note>('/api/notes', sendJson('POST', note)),
  updateNote: (id: string, patch: NoteUpdate) => request<Note>(`/api/notes/${id}`, sendJson('PATCH', patch)),
  deleteNote: (id: string) => request<void>(`/api/notes/${id}`, { method: 'DELETE' }),
  /** Every note, newest first: all of them (`null`), a paper's (its id), or those on no paper (`'none'`). */
  listAllNotes: (paper: string | null) => request<Note[]>(paper === null ? '/api/notes' : `/api/notes?paper=${paper}`),
  /** Replaces a note's papers; a passage on a paper left out goes with it. */
  setNotePapers: (noteId: string, paperIds: string[]) =>
    request<Note>(`/api/notes/${noteId}/papers`, sendJson('PUT', { paper_ids: paperIds })),
  listWorkspaces: () => request<Workspace[]>('/api/workspaces'),
  createWorkspace: (name: string) => request<Workspace>('/api/workspaces', sendJson('POST', { name })),
  renameWorkspace: (id: string, name: string) =>
    request<Workspace>(`/api/workspaces/${id}`, sendJson('PATCH', { name })),
  deleteWorkspace: (id: string) => request<void>(`/api/workspaces/${id}`, { method: 'DELETE' }),
  listWorkspacePapers: (id: string) => request<Paper[]>(`/api/workspaces/${id}/papers`),
  listWorkspaceNotes: (id: string) => request<Note[]>(`/api/workspaces/${id}/notes`),
  addToWorkspace: (workspaceId: string, paperId: string) =>
    request<void>(`/api/workspaces/${workspaceId}/papers/${paperId}`, { method: 'PUT' }),
  removeFromWorkspace: (workspaceId: string, paperId: string) =>
    request<void>(`/api/workspaces/${workspaceId}/papers/${paperId}`, { method: 'DELETE' }),
  listChat: (scope: ChatScope) => request<ChatAnswer[]>(chatUrl(scope)),
  promoteNote: (promote: PromoteRequest) => request<Note>('/api/notes/promote', sendJson('POST', promote)),
  /** Saves block `index` of a saved answer's suggested notes. A second save is refused with `already_saved`. */
  saveSuggestion: (outputId: string, index: number) =>
    request<Note>(`/api/chat/answers/${outputId}/notes`, sendJson('POST', { index })),
  /** One-click suggestions, not automatic notes: accept one through promoteNote, unchanged, using this
   * response's own output_id and a suggestion's chunk_id. */
  suggestNotes: (paperId: string) =>
    request<NoteSuggestionsOut>(`/api/papers/${paperId}/notes/suggest`, { method: 'POST' }),
  previewTable: (paperId: string, page: number, region: [number, number, number, number]) =>
    request<TablePreview>(`/api/papers/${paperId}/tables/preview`, sendJson('POST', { page, region })),
  addNumber: (paperId: string, number: NumberCreate) =>
    request<NumberAdded>(`/api/papers/${paperId}/numbers`, sendJson('POST', number)),
  numberCandidates: (text: string) => request<NumberCandidate[]>('/api/numbers/candidates', sendJson('POST', { text })),
  /** A paper's datasets, or every dataset without a paper id. */
  listDatasets: (paperId?: string) =>
    request<DatasetSummary[]>(paperId ? `/api/datasets?paper_id=${paperId}` : '/api/datasets'),
  getDataset: (id: string) => request<Dataset>(`/api/datasets/${id}`),
  createDataset: (dataset: DatasetCreate) => request<Dataset>('/api/datasets', sendJson('POST', dataset)),
  /** CSV from a file, or text pasted from a spreadsheet wrapped in a File. */
  importDataset: (file: File, name?: string) => {
    const form = new FormData()
    form.append('file', file)
    if (name) form.append('name', name)
    return request<Dataset>('/api/datasets/import', { method: 'POST', body: form })
  },
  renameDataset: (id: string, name: string) => request<Dataset>(`/api/datasets/${id}`, sendJson('PATCH', { name })),
  /** Without `force`, removing a column a chart uses fails with `used_by_charts`. */
  saveGrid: (id: string, grid: GridIn, force = false) =>
    request<Dataset>(`/api/datasets/${id}/grid${force ? '?force=true' : ''}`, sendJson('PUT', grid)),
  deleteDataset: (id: string, force = false) =>
    request<void>(`/api/datasets/${id}${force ? '?force=true' : ''}`, { method: 'DELETE' }),
  listCharts: () => request<ChartSummary[]>('/api/charts'),
  getChart: (id: string) => request<Chart>(`/api/charts/${id}`),
  createChart: (title: string, spec: ChartSpec) => request<Chart>('/api/charts', sendJson('POST', { title, spec })),
  updateChart: (id: string, patch: { title?: string; spec?: ChartSpec }) =>
    request<Chart>(`/api/charts/${id}`, sendJson('PATCH', patch)),
  duplicateChart: (id: string) => request<Chart>(`/api/charts/${id}/duplicate`, { method: 'POST' }),
  deleteChart: (id: string) => request<void>(`/api/charts/${id}`, { method: 'DELETE' }),
  /** The current cells a spec names, for a saved chart or an unsaved one in the builder. */
  resolveChart: (spec: ChartSpec) => request<ResolvedData>('/api/charts/resolve', sendJson('POST', { spec })),
  addChartToNote: (chartId: string) => request<Note>(`/api/charts/${chartId}/note`, { method: 'POST' }),
  attachChart: (noteId: string, chartId: string) =>
    request<void>(`/api/notes/${noteId}/charts/${chartId}`, { method: 'PUT' }),
  detachChart: (noteId: string, chartId: string) =>
    request<void>(`/api/notes/${noteId}/charts/${chartId}`, { method: 'DELETE' }),
  /**
   * The raw response: on success its body is the SSE stream that `useChatStream` reads. `modelId` null: the default.
   * `parentId`: the saved answer this question follows up (paper chat only); null for a question on its own.
   */
  askChat: (scope: ChatScope, question: string, modelId: string | null, parentId: string | null, signal?: AbortSignal) =>
    fetch(chatUrl(scope), { ...sendJson('POST', { question, model_id: modelId, parent_id: parentId }), signal }),
  listChatModels: () => request<ChatModel[]>('/api/llm/models'),
  listConnections: () => request<LLMConnection[]>('/api/llm/connections'),
  createConnection: (body: LLMConnectionCreate) => request<LLMConnection>('/api/llm/connections', sendJson('POST', body)),
  updateConnection: (id: string, body: LLMConnectionUpdate) =>
    request<LLMConnection>(`/api/llm/connections/${id}`, sendJson('PATCH', body)),
  deleteConnection: (id: string) => request<void>(`/api/llm/connections/${id}`, { method: 'DELETE' }),
  testConnection: (id: string) => request<ConnectionCheck>(`/api/llm/connections/${id}/test`, { method: 'POST' }),
  availableModels: (id: string) => request<AvailableModels>(`/api/llm/connections/${id}/available`),
  addModel: (connectionId: string, name: string) =>
    request<LLMModel>(`/api/llm/connections/${connectionId}/models`, sendJson('POST', { name })),
  removeModel: (id: string) => request<void>(`/api/llm/models/${id}`, { method: 'DELETE' }),
  setDefaultModel: (modelId: string) => request<LLMModel>('/api/llm/default', sendJson('PUT', { model_id: modelId })),
  /** The raw response: on success its body is the pull's SSE stream (progress…, then done or error). `addToChat`
   * false: Settings → Search's pull, which never lists the model in chat (`done.model` is null). */
  pullModel: (connectionId: string, name: string, signal?: AbortSignal, addToChat = true) =>
    fetch(`/api/llm/connections/${connectionId}/pull`, { ...sendJson('POST', { name, add_to_chat: addToChat }), signal }),
  /** `name` goes in the query: Ollama names can contain `/`. */
  deleteInstalledModel: (connectionId: string, name: string) =>
    request<void>(`/api/llm/connections/${connectionId}/installed?name=${encodeURIComponent(name)}`, { method: 'DELETE' }),
  embeddingStatus: () => request<EmbeddingStatus>('/api/embedding'),
  /** The raw response: on success its body is the download's SSE stream (progress…, then done or error). */
  downloadSearchModel: () => fetch('/api/embedding/model', { method: 'POST' }),
  /** `missingOnly`: Settings' Try again, which embeds only the papers not yet on the active source. */
  reindexLibrary: (missingOnly: boolean) =>
    request<{ papers: number }>('/api/embedding/reindex', sendJson('POST', { confirm: true, missing_only: missingOnly })),
  /** Probes the source, then saves it and queues the papers not yet on it: 202 {papers}, 200 for the source in use. */
  switchSearchSource: (body: SearchSourceIn) =>
    request<{ papers: number }>('/api/embedding/source', sendJson('PUT', body)),
  paperSources: () => request<PaperSources>('/api/paper-sources'),
  /** Only what `patch` holds changes: a key left out is kept, a null key is removed. */
  updatePaperSources: (patch: PaperSourcesUpdate) =>
    request<PaperSources>('/api/paper-sources', sendJson('PATCH', patch)),
  /** One direction of a paper's references, ranked for this library. */
  references: (paperId: string, direction: ReferencesDirection) =>
    request<References>(`/api/papers/${paperId}/references?direction=${direction}`),
  /** Queues a fetch of both directions; a second call while already fetching is a no-op. */
  refreshReferences: (paperId: string) =>
    request<RefreshOut>(`/api/papers/${paperId}/references/refresh`, { method: 'POST' }),
  /** Downloads a reference's free PDF into the library; with a `workspaceId` it also joins that workspace. */
  importReference: (refId: string, workspaceId?: string) =>
    request<Paper>(`/api/references/${refId}/import`, sendJson('POST', { workspace_id: workspaceId ?? null })),
  /** The whole library's, or one workspace's, To read list and shared references (D121, D166). 404 unknown workspace. */
  referencePage: (workspaceId: string | null) =>
    request<ReferencePage>(workspaceId ? `/api/references?workspace=${workspaceId}` : '/api/references'),
  /** Marks a reference To read; a second call keeps the first time. 404 unknown reference. */
  queueReference: (refId: string) => request<{ queued_at: string | null }>(`/api/references/${refId}/queue`, sendJson('PUT', undefined)),
  /** Takes a reference off To read; harmless when it isn't on it. 404 unknown reference. */
  unqueueReference: (refId: string) => request<{ queued_at: string | null }>(`/api/references/${refId}/queue`, { method: 'DELETE' }),
  /** The folder `docker compose up` ran in, to prefill Connect Claude; null outside Compose. */
  mcpSetup: () => request<McpSetup>('/api/mcp/setup'),
  /** Starts the MCP server as a client would and reads the library through it. Takes up to 30 s. */
  checkMcpServer: () => request<McpCheck>('/api/mcp/check', { method: 'POST' }),
  /** Whether the first-run setup is done: while it isn't, the app opens #/setup on start. */
  setup: () => request<SetupState>('/api/setup'),
  /** Finish and Skip send true; false is for tests. */
  setSetupDone: (done: boolean) => request<SetupState>('/api/setup', sendJson('PUT', { done })),
  /** Every library paper and the links between them, or one workspace's. Capped at 2000 links. */
  libraryGraph: (workspaceId: string | null) =>
    request<LibraryGraph>(workspaceId ? `/api/graph?workspace=${workspaceId}` : '/api/graph'),
  createPaperLink: (fromPaper: string, toPaper: string, label: string) =>
    request<PaperLinkOut>('/api/links', sendJson('POST', { from_paper: fromPaper, to_paper: toPaper, label })),
  renamePaperLink: (id: string, label: string) =>
    request<PaperLinkOut>(`/api/links/${id}`, sendJson('PATCH', { label })),
  deletePaperLink: (id: string) => request<void>(`/api/links/${id}`, { method: 'DELETE' }),
  startSearchRun: (workspaceId: string, body: SearchRunCreate) =>
    request<SearchRun>(`/api/workspaces/${workspaceId}/search/runs`, sendJson('POST', body)),
  stopSearchRun: (workspaceId: string, runId: string) =>
    request<SearchRun>(`/api/workspaces/${workspaceId}/search/runs/${runId}/stop`, { method: 'POST' }),
  getEvidenceCard: (paperId: string) => request<EvidenceCard>(`/api/papers/${paperId}/evidence-card`),
  generateEvidenceCard: (paperId: string, regenerate: boolean, confirm: boolean) =>
    request<EvidenceCard[]>(`/api/papers/${paperId}/evidence-card`, sendJson('POST', { regenerate, confirm })),
  editEvidenceCard: (paperId: string, changes: EvidenceChange[]) =>
    request<EvidenceCard>(`/api/papers/${paperId}/evidence-card`, sendJson('PATCH', { changes })),
  listEvidenceCards: (workspaceId: string) =>
    request<EvidenceCardRow[]>(`/api/workspaces/${workspaceId}/evidence-cards`),
  generateEvidenceCards: (workspaceId: string) =>
    request<EvidenceCard[]>(`/api/workspaces/${workspaceId}/evidence-cards`, sendJson('POST', {})),
  evidenceCardsCsvUrl: (workspaceId: string) => `/api/workspaces/${workspaceId}/evidence-cards.csv`,
  listSavedSearches: (workspaceId: string) =>
    request<SavedSearch[]>(`/api/workspaces/${workspaceId}/saved-searches`),
  createSavedSearch: (workspaceId: string, body: SavedSearchCreate) =>
    request<SavedSearch>(`/api/workspaces/${workspaceId}/saved-searches`, sendJson('POST', body)),
  updateSavedSearch: (workspaceId: string, id: string, body: SavedSearchUpdate) =>
    request<SavedSearch>(`/api/workspaces/${workspaceId}/saved-searches/${id}`, sendJson('PATCH', body)),
  deleteSavedSearch: (workspaceId: string, id: string) =>
    request<void>(`/api/workspaces/${workspaceId}/saved-searches/${id}`, { method: 'DELETE' }),
  runSavedSearchNow: (workspaceId: string, id: string) =>
    request<SearchRun>(`/api/workspaces/${workspaceId}/saved-searches/${id}/run`, { method: 'POST' }),
  getSearchRun: (workspaceId: string, runId: string) =>
    request<SearchRun>(`/api/workspaces/${workspaceId}/search/runs/${runId}`),
  listSearchHits: (
    workspaceId: string,
    params: {
      after?: string; limit?: number; stage1_status?: string; acquisition_status?: string; sort?: 'ai'; min_relevance?: number
    },
  ) => {
    const entries = Object.entries(params).filter(([, value]) => value !== undefined) as [string, string | number][]
    const query = new URLSearchParams(entries.map(([key, value]) => [key, String(value)]))
    return request<HitListOut>(`/api/workspaces/${workspaceId}/search/hits?${query}`)
  },
  patchSearchHit: (workspaceId: string, hitId: string, body: HitReviewUpdate) =>
    request<Hit>(`/api/workspaces/${workspaceId}/search/hits/${hitId}`, sendJson('PATCH', body)),
  bulkPatchSearchHits: (workspaceId: string, body: BulkHitReviewUpdate) =>
    request<BulkUpdateOut>(`/api/workspaces/${workspaceId}/search/hits/bulk`, sendJson('PATCH', body)),
  rankedSearchHits: (workspaceId: string, limit = 200) =>
    request<RankedHits>(`/api/workspaces/${workspaceId}/search/hits/ranked?limit=${limit}`),
  /** `hitIds` omitted imports every hit still pending acquisition. */
  importSearchHits: (workspaceId: string, hitIds?: string[]) =>
    request<ImportHitsOut>(
      `/api/workspaces/${workspaceId}/search/hits/import`, sendJson('POST', { hit_ids: hitIds ?? null }),
    ),
  /** Deletes every hit in the pool not yet imported or manually acquired; papers already in the corpus stay. */
  clearSearchHits: (workspaceId: string) =>
    request<ClearHitsOut>(`/api/workspaces/${workspaceId}/search/hits`, { method: 'DELETE' }),
  uploadHitPdf: (workspaceId: string, hitId: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<Hit>(`/api/workspaces/${workspaceId}/search/hits/${hitId}/upload`, { method: 'POST', body: form })
  },
  snowball: (workspaceId: string, body: SnowballRequest) =>
    request<SnowballOut>(`/api/workspaces/${workspaceId}/search/snowball`, sendJson('POST', body)),
  setEligibility: (workspaceId: string, paperId: string, runId: string, body: EligibilityUpdate) =>
    request<EligibilityOut>(
      `/api/workspaces/${workspaceId}/papers/${paperId}/eligibility?run=${runId}`,
      sendJson('PATCH', body),
    ),
  /** A workspace's citations as text (the apa-html preview and the plain apa to copy). Downloads use the URL directly. */
  citationsText: async (workspaceId: string, format: string): Promise<string> => {
    const response = await fetch(citationsUrl(workspaceId, format))
    if (!response.ok) throw new Error(await errorDetail(response))
    return response.text()
  },
  /** `runs`: a single run id, or `'all'` to combine every run in the workspace. */
  prismaExport: (workspaceId: string, runs: string) =>
    request<PrismaExportOut>(`/api/workspaces/${workspaceId}/search/prisma?runs=${runs}`),
  readingContext: (paperId: string) => request<ReadingContextList>(`/api/papers/${paperId}/reading-context`),
  readingQueue: (workspaceId: string, runId: string) =>
    request<ReadingQueue>(`/api/workspaces/${workspaceId}/search/reading-queue?run=${runId}`),
  screeningState: (workspaceId: string) => request<ScreeningState>(`/api/workspaces/${workspaceId}/search/screening`),
  /** Any subset of the criteria and settings. */
  saveScreening: (workspaceId: string, body: ScreeningUpdate) =>
    request<ScreeningState>(`/api/workspaces/${workspaceId}/search/screening`, sendJson('PUT', body)),
  acceptSuggestions: (workspaceId: string, hitIds: string[]) =>
    request<AcceptSuggestionsOut>(
      `/api/workspaces/${workspaceId}/search/hits/accept-suggestions`, sendJson('POST', { hit_ids: hitIds }),
    ),
  startSuggestions: (workspaceId: string, confirmRemote: boolean) =>
    request<ScreeningState>(
      `/api/workspaces/${workspaceId}/search/suggestions`, sendJson('POST', { confirm_remote: confirmRemote }),
    ),
  stopSuggestions: (workspaceId: string) =>
    request<ScreeningState>(`/api/workspaces/${workspaceId}/search/suggestions/stop`, { method: 'POST' }),
}
