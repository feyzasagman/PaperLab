import type { GraphLink, GraphNode } from '@/api/client'
import { degrees, KINDS } from './graphModel'

/** A paper's linked papers in the side panel's order: by kind, then as the links come, each paper once. */
export function linkedInOrder(links: readonly GraphLink[], paperId: string): string[] {
  const order: string[] = []
  for (const kind of KINDS) {
    for (const link of links) {
      if (link.kind !== kind) continue
      const other = link.source === paperId ? link.target : link.target === paperId ? link.source : null
      if (other !== null && other !== paperId && !order.includes(other)) order.push(other)
    }
  }
  return order
}

/**
 * Where a hop lands: one step from `current` along `order`, wrapping round. From a paper not in the order (the one
 * stepping started from) Next goes to the first and Previous to the last. Null when there is nowhere to go.
 */
export function hopTarget(order: readonly string[], current: string | null, step: 1 | -1): string | null {
  if (order.length === 0) return null
  const at = current === null ? -1 : order.indexOf(current)
  if (at === -1) return step === 1 ? order[0] : order[order.length - 1]
  return order[(at + step + order.length) % order.length]
}

/** ] steps to the next linked paper and [ to the previous; any key held with Ctrl, Cmd or Alt is left alone. */
export function hopKey(event: Pick<KeyboardEvent, 'key' | 'ctrlKey' | 'metaKey' | 'altKey'>): 1 | -1 | null {
  if (event.ctrlKey || event.metaKey || event.altKey) return null
  return event.key === ']' ? 1 : event.key === '[' ? -1 : null
}

export type TourOrder = 'links' | 'years'

/**
 * The tour by links: from `start` (or, when it isn't on the graph, the most linked paper), every linked paper in the
 * side panel's order, then theirs, each paper once. Papers no link reaches are left out.
 */
export function tourByLinks(nodes: readonly GraphNode[], links: GraphLink[], start: string | null): string[] {
  const counts = degrees(links)
  const known = start !== null && nodes.some((node) => node.id === start) && (counts.get(start) ?? 0) > 0
  const first = known ? start : [...nodes].sort((a, b) => (counts.get(b.id) ?? 0) - (counts.get(a.id) ?? 0))[0]?.id
  if (first === undefined) return []
  const visited = [first]
  for (let i = 0; i < visited.length; i++) {
    for (const next of linkedInOrder(links, visited[i])) if (!visited.includes(next)) visited.push(next)
  }
  return visited
}

/** The tour oldest first: by year published, papers with no year last, ties by title. */
export function tourByYear(nodes: readonly GraphNode[]): string[] {
  const year = (node: GraphNode) => node.year ?? Number.POSITIVE_INFINITY
  return [...nodes].sort((a, b) => year(a) - year(b) || a.title.localeCompare(b.title)).map((node) => node.id)
}
