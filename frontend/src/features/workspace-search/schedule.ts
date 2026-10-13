import type { SavedSearch } from '@/api/client'

export const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

export const browserTimeZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone

type Schedule = Pick<SavedSearch, 'schedule_kind' | 'time' | 'weekday' | 'timezone'> & { schedule?: string }

/** "Daily at 02:00 · Europe/Berlin", "Mondays at 18:30 · UTC", "Cron 0 2 * * 1-5 · UTC". */
export function describeSchedule(s: Schedule): string {
  const zone = s.timezone
  if (s.schedule_kind === 'daily') return `Daily at ${s.time} · ${zone}`
  if (s.schedule_kind === 'weekly') return `${WEEKDAYS[s.weekday ?? 0]}s at ${s.time} · ${zone}`
  return `Cron ${s.schedule} · ${zone}`
}

/** "2023–2025", "from 2023", "to 2025" or null. */
export function describeYears(from: number | null, to: number | null): string | null {
  if (from && to) return `${from}–${to}`
  if (from) return `from ${from}`
  if (to) return `to ${to}`
  return null
}

export const ERRORS: Record<string, string> = {
  invalid_schedule: 'That schedule is not valid. Check the time or the cron text.',
  invalid_timezone: 'That time zone is not recognised. Use a name like Europe/Berlin.',
  invalid_years: 'The first year must not be after the last year.',
  unknown_source: 'One of the chosen sources is not available.',
  saved_search_not_found: 'This saved search no longer exists.',
}

/** A server error code as a readable sentence (unknown codes pass through). */
export const errorText = (code: string | undefined) => (code ? (ERRORS[code] ?? code) : null)
