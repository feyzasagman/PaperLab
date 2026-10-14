import { rm } from 'node:fs/promises'
import { OWNERS_DEFAULT_FILE, assertHealthyTestStack, readOwnersDefault, restoreOwnersDefault, sweepE2EConnections } from './globalSetup'

/**
 * Puts the owner's default model back and deletes every connection the run made, whatever the specs did with
 * either. The record only goes away once the restore has actually worked, so a failed one is retried (and
 * reported) by the next run's setup instead of being lost.
 */
export default async function globalTeardown() {
  // Playwright runs teardown even when setup refused the stack: a refused stack gets no write from here either.
  await assertHealthyTestStack()
  const recorded = await readOwnersDefault()
  // Keep the failure rather than throwing on it: the sweep below still has to run.
  const failed = recorded === '' ? null : await restoreOwnersDefault(recorded).then(() => null, (error: unknown) => error)
  await sweepE2EConnections()
  if (failed !== null) throw failed
  await rm(OWNERS_DEFAULT_FILE, { force: true })
}
