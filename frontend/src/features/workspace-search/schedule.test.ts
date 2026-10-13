import { expect, test } from 'vitest'
import { browserTimeZone, describeSchedule, describeYears } from './schedule'

test('describes daily, weekly and custom schedules', () => {
  expect(describeSchedule({ schedule_kind: 'daily', time: '02:00', weekday: null, timezone: 'Europe/Berlin' })).toBe(
    'Daily at 02:00 · Europe/Berlin',
  )
  expect(describeSchedule({ schedule_kind: 'weekly', time: '18:30', weekday: 1, timezone: 'UTC' })).toBe(
    'Mondays at 18:30 · UTC',
  )
  expect(
    describeSchedule({ schedule_kind: 'custom', time: null, weekday: null, timezone: 'UTC', schedule: '0 2 * * 1-5' }),
  ).toBe('Cron 0 2 * * 1-5 · UTC')
})

test('browserTimeZone is the Intl zone; years read naturally', () => {
  expect(browserTimeZone()).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone)
  expect(describeYears(2020, 2024)).toBe('2020–2024')
  expect(describeYears(null, 2024)).toBe('to 2024')
  expect(describeYears(null, null)).toBeNull()
})
