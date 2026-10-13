import { Info } from 'lucide-react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { Paper } from '@/api/client'
import { api } from '@/api/client'
import { keys } from '@/api/queries'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { readerHref } from '@/lib/route'

type Outcome = { kind: 'added' } | { kind: 'owned'; paperId: string } | { kind: 'no-pdf' } | { kind: 'not-found' }

/** Looks the published DOI up the way Find papers does, and adds it only when a free PDF is listed. */
async function addPublished(doi: string): Promise<Outcome> {
  const { results } = await api.searchPapers(doi)
  const found = results.find((c) => c.doi?.toLowerCase() === doi.toLowerCase())
  if (!found) return { kind: 'not-found' }
  if (found.paper_id) return { kind: 'owned', paperId: found.paper_id }
  if (found.pdf_urls.length === 0) return { kind: 'no-pdf' }
  await api.addCandidate(found)
  return { kind: 'added' }
}

/** Shown for a preprint that has since been published. It never replaces the preprint: the owner decides. */
export function PublishedVersionBanner({ paper }: { paper: Paper }) {
  const client = useQueryClient()
  const doi = paper.published_doi!
  const doiHref = `https://doi.org/${encodeURIComponent(doi)}`
  const add = useMutation({
    mutationFn: () => addPublished(doi),
    onSuccess: (outcome) => outcome.kind === 'added' && Promise.all([client.invalidateQueries({ queryKey: keys.papers }), client.invalidateQueries({ queryKey: keys.workspaces })]),
  })
  const outcome = add.data
  return (
    <Alert className="published-banner rounded-none border-x-0 border-t-0 px-4 py-2.5">
      <Info aria-hidden />
      <AlertTitle>A published version of this preprint exists</AlertTitle>
      <AlertDescription>
        <a href={doiHref} target="_blank" rel="noreferrer" className="text-primary hover:underline">
          doi:{doi}
        </a>{' '}
        {outcome?.kind === 'added' && 'Added to your library.'}
        {outcome?.kind === 'owned' && (
          <>
            Already in your library.{' '}
            <a href={readerHref(outcome.paperId)} className="text-primary hover:underline">
              Open it
            </a>
          </>
        )}
        {outcome?.kind === 'no-pdf' && (
          <>
            There is no free PDF to add, but you can read it at{' '}
            <a href={doiHref} target="_blank" rel="noreferrer" className="text-primary hover:underline">
              its page
            </a>
            .
          </>
        )}
        {outcome?.kind === 'not-found' && "We couldn't find it just now. The DOI link above should still work."}
        {add.isError && "We couldn't add it just now. Please try again."}
        {(!outcome || add.isError) && (
          <Button size="sm" variant="outline" className="ml-2" disabled={add.isPending} onClick={() => add.mutate()}>
            {add.isPending ? 'Adding…' : 'Add the published version'}
          </Button>
        )}
      </AlertDescription>
    </Alert>
  )
}
