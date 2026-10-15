import { useEffect, useRef, type RefObject } from 'react'
import { ChevronLeft, ChevronRight, Link2, Pencil, Trash2 } from 'lucide-react'
import type { GraphLink, GraphNode } from '@/api/client'
import { Button } from '@/components/ui/button'
import { readerHref } from '@/lib/route'
import { connectionKey, connectionNote, degrees, hopSections, KIND_LABELS, KINDS } from './graphModel'

type Props = {
  nodes: GraphNode[]
  /** The visible links only: the panel says exactly what the canvas draws. */
  links: GraphLink[]
  focused: GraphNode | null
  /** Links out: papers 2 and 3 links away get their own sections. */
  hops: number
  onFocus: (paperId: string) => void
  onClear: () => void
  onAddLink: () => void
  onEditLink: (link: GraphLink) => void
  /** Whether the owner confirmed the removal, so the panel knows whether to move focus. */
  onRemoveLink: (link: GraphLink) => boolean
  /** Next and Previous round the linked papers of the paper stepping started from; null when there are none. */
  stepping: { from: string | null; position: number; count: number } | null
  /** One step: 1 next, -1 previous. */
  onStep: (step: 1 | -1) => void
}

type ButtonRefs = { current: Map<string, HTMLButtonElement> }

/** Sorted by link count then title. Always shown (spec §6), so it's the same component whether or not a paper is
 * focused — kept as one so the two panel states never drift apart, and given a stable key by its caller so clearing
 * focus doesn't remount it out from under the button the owner just clicked. */
function PapersList({
  nodes,
  degree,
  onFocus,
  buttonRefs,
  headingRef,
}: {
  nodes: GraphNode[]
  degree: Map<string, number>
  onFocus: (paperId: string) => void
  buttonRefs: ButtonRefs
  headingRef: RefObject<HTMLHeadingElement | null>
}) {
  const sorted = [...nodes].sort(
    (a, b) => (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0) || a.title.localeCompare(b.title)
  )
  return (
    <section aria-labelledby="graph-papers" className="flex flex-1 basis-0 min-h-32 flex-col gap-2">
      <h2 id="graph-papers" ref={headingRef} tabIndex={-1} className="text-sm font-medium">
        Papers
      </h2>
      <ul className="flex min-h-0 flex-col gap-1 overflow-y-auto">
        {sorted.map((node) => (
          <li key={node.id}>
            <button
              type="button"
              ref={(element) => {
                if (element) buttonRefs.current.set(node.id, element)
                else buttonRefs.current.delete(node.id)
              }}
              className="graph-paper flex w-full items-baseline gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors duration-150 outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50"
              data-paper-id={node.id}
              onClick={() => onFocus(node.id)}
            >
              <span className="truncate" title={node.title}>
                {node.title}
              </span>
              {node.reading_pass > 0 && (
                <span className="shrink-0 text-xs text-muted-foreground">Pass {node.reading_pass}</span>
              )}
              <span className="ml-auto shrink-0 text-muted-foreground">{degree.get(node.id) ?? 0}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}

/** A paper's connections by kind, plus the papers list. D108: the canvas is hidden, so this is the interface. */
export function GraphPanel({
  nodes,
  links,
  focused,
  hops,
  onFocus,
  onClear,
  onAddLink,
  onEditLink,
  onRemoveLink,
  stepping,
  onStep,
}: Props) {
  const byId = new Map(nodes.map((node) => [node.id, node]))
  const degree = degrees(links)
  const buttonRefs = useRef<Map<string, HTMLButtonElement>>(new Map())
  const connectionsHeading = useRef<HTMLHeadingElement>(null)
  const papersHeading = useRef<HTMLHeadingElement>(null)
  const clearedId = useRef<string | null>(null)
  const shownId = useRef<string | null>(null)

  // Moves focus so a screen reader announces what just appeared: to the heading when a paper becomes focused, or
  // (after "Clear focus", which records the paper it cleared) back to that paper's own button. Depends on the
  // focused paper's *identity* only: a refetch that returns a new object for the same id must not steal focus again.
  useEffect(() => {
    const previous = shownId.current
    shownId.current = focused?.id ?? null
    if (focused !== null) {
      connectionsHeading.current?.focus()
    } else if (clearedId.current) {
      buttonRefs.current.get(clearedId.current)?.focus()
      clearedId.current = null
    } else if (previous !== null && document.activeElement === document.body) {
      // K20: the focused paper left the graph (a workspace switch landed without it) and took its section, and the
      // keyboard focus inside it, along. Continue from the papers list, never from <body>. Only when focus was
      // actually lost: the Workspace select the owner just used keeps it.
      papersHeading.current?.focus()
    }
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [focused?.id])

  function clear() {
    if (focused) clearedId.current = focused.id
    onClear()
  }

  function removeLink(link: GraphLink) {
    if (onRemoveLink(link)) connectionsHeading.current?.focus()
  }

  const connections = focused ? links.filter((link) => link.source === focused.id || link.target === focused.id) : []
  const other = (link: GraphLink) => (link.source === focused?.id ? link.target : link.source)
  const note = (link: GraphLink) => (focused ? connectionNote(link, focused.id) : null)
  const away = focused ? hopSections(nodes, links, focused.id, hops) : []

  return (
    <>
      {focused !== null && (
        <section
          key="graph-connections"
          aria-labelledby="graph-connections"
          className="flex min-h-0 max-h-[60%] flex-col gap-3"
        >
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <h2 id="graph-connections" ref={connectionsHeading} tabIndex={-1} className="text-sm font-medium">
                Connected papers
              </h2>
              <p className="truncate text-sm text-muted-foreground" title={focused.title}>
                {focused.title}
              </p>
            </div>
            <Button variant="ghost" size="sm" onClick={clear}>
              Clear focus
            </Button>
          </div>

          <Button variant="outline" size="sm" className="self-start" onClick={onAddLink}>
            <Link2 aria-hidden />
            Link to another paper…
          </Button>

          {stepping && (
            <div className="flex flex-col gap-1">
              <div role="group" aria-label="Step through linked papers" className="flex gap-1">
                <Button variant="outline" size="sm" aria-label="Previous linked paper" onClick={() => onStep(-1)}>
                  <ChevronLeft aria-hidden />
                  Previous
                </Button>
                <Button variant="outline" size="sm" aria-label="Next linked paper" onClick={() => onStep(1)}>
                  Next
                  <ChevronRight aria-hidden />
                </Button>
              </div>
              {stepping.from !== null && (
                <p className="text-xs text-muted-foreground">{`Linked to ${stepping.from}: ${stepping.position} of ${stepping.count}`}</p>
              )}
            </div>
          )}

          <div className="flex min-h-0 flex-col gap-4 overflow-y-auto">
            {connections.length === 0 && (
              <p className="text-sm text-muted-foreground">No links in the layers you have on.</p>
            )}
            {KINDS.map((kind) => {
              const rows = connections.filter((link) => link.kind === kind)
              if (rows.length === 0) return null
              return (
                <div key={kind} className="flex flex-col gap-1">
                  <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                    {KIND_LABELS[kind]}
                  </h3>
                  <ul className="flex flex-col gap-1">
                    {rows.map((link) => {
                      const paper = byId.get(other(link))
                      if (!paper) return null
                      return (
                        <li
                          key={connectionKey(link)}
                          className="graph-connection flex items-center gap-1"
                        >
                          <a
                            href={readerHref(paper.id)}
                            className="min-w-0 flex-1 truncate rounded-md px-2 py-1.5 text-sm transition-colors duration-150 outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50"
                            title={paper.title}
                            data-paper-id={paper.id}
                          >
                            {paper.title}
                            {note(link) && <span className="text-muted-foreground"> — {note(link)}</span>}
                          </a>
                          {kind === 'manual' && (
                            <>
                              <Button
                                variant="ghost"
                                size="icon"
                                aria-label={`Edit label for ${paper.title}`}
                                onClick={() => onEditLink(link)}
                              >
                                <Pencil aria-hidden />
                              </Button>
                              <Button
                                variant="ghost"
                                size="icon"
                                aria-label={`Remove link to ${paper.title}`}
                                onClick={() => removeLink(link)}
                              >
                                <Trash2 aria-hidden />
                              </Button>
                            </>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                </div>
              )
            })}
            {away.map((section) => (
              <div key={section.heading} className="flex flex-col gap-1">
                <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{section.heading}</h3>
                <ul className="flex flex-col gap-1">
                  {section.papers.map((paper) => (
                    <li key={paper.id}>
                      <button
                        type="button"
                        className="graph-away flex w-full rounded-md px-2 py-1.5 text-left text-sm transition-colors duration-150 outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50"
                        data-paper-id={paper.id}
                        onClick={() => onFocus(paper.id)}
                      >
                        <span className="truncate" title={paper.title}>
                          {paper.title}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </section>
      )}
      <PapersList
        key="graph-papers"
        nodes={nodes}
        degree={degree}
        onFocus={onFocus}
        buttonRefs={buttonRefs}
        headingRef={papersHeading}
      />
    </>
  )
}
