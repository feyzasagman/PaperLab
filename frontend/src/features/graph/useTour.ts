import { useCallback, useEffect, useRef, useState } from 'react'

/** How long the tour stays on each paper. */
export const TOUR_STOP_MS = 3000

/**
 * The guided tour: opens each paper of an order in turn, TOUR_STOP_MS apart. It stops after the last, when `stop`
 * is called, and when the open paper becomes one the tour didn't open (the owner chose another).
 */
export function useTour(onSelect: (id: string) => void, focusId: string | null) {
  const [running, setRunning] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const opened = useRef<string | null>(null)
  const select = useRef(onSelect)
  useEffect(() => {
    select.current = onSelect
  })

  const stop = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current)
    timer.current = null
    opened.current = null
    setRunning(false)
  }, [])

  const start = useCallback(
    (order: readonly string[]) => {
      stop()
      if (order.length === 0) return
      setRunning(true)
      const visit = (i: number) => {
        opened.current = order[i]
        select.current(order[i])
        if (i + 1 < order.length) {
          timer.current = setTimeout(() => visit(i + 1), TOUR_STOP_MS)
        } else {
          timer.current = null
          setRunning(false)
        }
      }
      visit(0)
    },
    [stop],
  )

  useEffect(() => {
    if (running && opened.current !== null && focusId !== opened.current) stop()
  }, [running, focusId, stop])
  useEffect(() => stop, [stop])

  return { running, start, stop }
}
