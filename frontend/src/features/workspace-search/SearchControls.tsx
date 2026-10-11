import { useState } from 'react'
import { usePaperSources } from '@/api/queries'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { SearchRunCreate, SearchRunSource } from '@/api/client'

// Search/discovery sources only (spec §6) — mirrors the backend's SearchSource Literal. Unpaywall is DOI-only PDF
// enrichment, never fanned out to by search_batch, so it's excluded here regardless of its own settings switch
// (C1: sending it used to 422 every Start click). A Record, not an array: adding a search source to the backend's
// SearchRunSource union without adding it here is now a compiler error, not a silent no-op. IACR ePrint DOES belong
// here: its backend SourceSpec (source_registry.py) has no one-shot `ask` at all, so it never participates in any
// one-shot lookup path (including FindPapersButton.tsx, which has no source list of its own to exclude it from --
// it just calls useSearchPapers with no per-source enumeration). It DOES have a `page` function, and search_page
// is only ever reached through a Workspace Search run, so this Start-button source list is the one place IACR's
// harvested rows can actually surface (M32 batch6a final review,
// fix 1 -- the prior exclusion here, following Task 8's own brief, made the whole feature unreachable from the UI).
// bioRxiv and medRxiv share this same reasoning: neither has a one-shot `ask` either, both only ever surface
// through their own harvested local index via `page()`, so they belong here too.
const SEARCH_SOURCES: Record<SearchRunSource, true> = {
  arxiv: true, crossref: true, core: true, semantic_scholar: true, openalex: true, pubmed: true,
  pmc: true, europe_pmc: true, zenodo: true, hal: true, acm_dl: true, ssrn: true, doaj: true, openaire: true,
  iacr_eprint: true, biorxiv: true, medrxiv: true,
}

function isSearchSource(id: string): id is SearchRunSource {
  return id in SEARCH_SOURCES
}

export function SearchControls({
  onStart,
  onStop,
  isRunning,
}: {
  onStart: (args: SearchRunCreate) => void
  onStop?: () => void
  isRunning: boolean
}) {
  const [query, setQuery] = useState('')
  const paperSources = usePaperSources()
  // undefined until usePaperSources resolves, so Start stays disabled rather than sending an empty/wrong list.
  const enabledSources = paperSources.data?.sources
    .filter((source) => source.enabled && isSearchSource(source.id))
    .map((source) => source.id as SearchRunSource)

  return (
    <div className="flex items-center gap-2 border-b p-3">
      <label htmlFor="search-query" className="sr-only">
        Search query
      </label>
      <Input
        id="search-query"
        aria-label="Search query"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="e.g. large language model AND code review"
        disabled={isRunning}
      />
      {isRunning ? (
        <Button variant="destructive" onClick={onStop}>
          Stop
        </Button>
      ) : (
        <Button
          onClick={() => onStart({ query, filters: {}, sources: enabledSources!, query_overrides: {} })}
          disabled={!query || !enabledSources || enabledSources.length === 0}
        >
          Start
        </Button>
      )}
    </div>
  )
}
