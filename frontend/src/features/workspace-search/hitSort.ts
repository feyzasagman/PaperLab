export type HitSort = 'found' | 'ranked' | 'year' | 'ai'
// Widened to `| undefined` on top of the brief's own alias: `browserStorage()` (features/notes/highlightColors)
// returns `ColorStorage | undefined`, not `| null`, and the two are structurally identical otherwise.
type Storage = Pick<globalThis.Storage, 'getItem' | 'setItem'> | null | undefined

const key = (workspaceId: string) => `paperlab-hit-sort:${workspaceId}`

/** A per-viewer convenience (spec §3.4): a blocked or missing storage just means found order. */
export function loadHitSort(storage: Storage, workspaceId: string): HitSort {
  try {
    const value = storage?.getItem(key(workspaceId))
    return value === 'ranked' || value === 'year' || value === 'ai' ? value : 'found'
  } catch {
    return 'found'
  }
}

export function saveHitSort(storage: Storage, workspaceId: string, sort: HitSort): void {
  try {
    storage?.setItem(key(workspaceId), sort)
  } catch {
    // Private window or blocked site data: the choice lasts this visit only.
  }
}
