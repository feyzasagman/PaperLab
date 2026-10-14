import { readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { assertTestStack } from './stackGuard'

/** The label every connection an E2E run makes starts with, so its litter is recognisable afterwards. */
export const E2E_CONNECTION_PREFIX = 'E2E connection '

/** The dev server proxies /api to the API, so one base URL covers both. */
const BASE = process.env.E2E_BASE_URL ?? 'http://localhost:5280'

/**
 * Where the owner's default model id waits for the teardown below. A fixed path, not a per-run one: a run that
 * never reaches teardown -- a killed worker, a crashed browser, a SIGKILL, a test that times out again while
 * cleaning up -- leaves the file behind, and the next run finds it and puts the owner's default back.
 */
export const OWNERS_DEFAULT_FILE = join(tmpdir(), 'paperlab-e2e-owners-default-model')

/** The restore answered 404: the recorded model no longer exists. */
export class StaleModel extends Error {}

type Model = { id: string; is_default: boolean }
type Connection = { id: string; label: string }

export async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`)
  if (!response.ok) throw new Error(`GET ${path} answered ${response.status}: bring the stack up before the E2E run`)
  return (await response.json()) as T
}

/** Throws unless the stack at BASE is a test stack. Setup and teardown both start with it: teardown runs even when setup threw. */
export async function assertHealthyTestStack() {
  assertTestStack((await get<{ stack?: unknown }>('/api/health')).stack, BASE)
}

/** The recorded id, or '' when there is no record. */
export async function readOwnersDefault(): Promise<string> {
  return readFile(OWNERS_DEFAULT_FILE, 'utf8').then(
    (text) => text.trim(),
    () => '',
  )
}

/** The id of the model chat uses when none is picked, or null. */
export async function defaultModelId(): Promise<string | null> {
  return (await get<Model[]>('/api/llm/models')).find((model) => model.is_default)?.id ?? null
}

/** Puts the owner's default back. Throws with the status and the file path: a failed restore is never silent. */
export async function restoreOwnersDefault(modelId: string) {
  const response = await fetch(`${BASE}/api/llm/default`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_id: modelId }),
  })
  if (response.status !== 200) {
    throw new (response.status === 404 ? StaleModel : Error)(
      `Could not put the default model back: PUT /api/llm/default answered ${response.status} for ${modelId}. ` +
        `The id is in ${OWNERS_DEFAULT_FILE}; set that model as the default in Settings, then delete the file.`,
    )
  }
}

/** Deletes every connection an E2E run left behind, with its models. Throws on a refused delete. */
export async function sweepE2EConnections() {
  for (const connection of await get<Connection[]>('/api/llm/connections')) {
    if (!connection.label.startsWith(E2E_CONNECTION_PREFIX)) continue
    const deleted = await fetch(`${BASE}/api/llm/connections/${connection.id}`, { method: 'DELETE' })
    if (deleted.status !== 204) throw new Error(`Could not delete "${connection.label}": ${deleted.status}`)
  }
}

/**
 * A fresh test stack has no model, and the AI specs (pre-screening, evidence cards...) expect a default one, as the
 * owner's library has. Reuses an existing model, else seeds one; not E2E-prefixed so the sweep leaves it. Local
 * (localhost): a cloud model makes the Suggest button ask window.confirm, which Playwright dismisses. The fake
 * provider never calls the URL. Runs before the owner's-default snapshot so that snapshot already sees a default.
 */
async function ensureDefaultModel() {
  const models = await get<Model[]>('/api/llm/models')
  if (models.some((model) => model.is_default)) return
  if (models.length > 0) return restoreOwnersDefault(models[0].id)
  const json = { 'Content-Type': 'application/json' }
  const created = await fetch(`${BASE}/api/llm/connections`, {
    method: 'POST',
    headers: json,
    body: JSON.stringify({ kind: 'openai_compatible', label: 'Test stack model', base_url: 'http://localhost:11434/v1' }),
  })
  if (created.status !== 201) throw new Error(`POST /api/llm/connections answered ${created.status}`)
  const { id } = (await created.json()) as Connection
  const model = await fetch(`${BASE}/api/llm/connections/${id}/models`, {
    method: 'POST',
    headers: json,
    body: JSON.stringify({ name: 'test-stack-model' }),
  })
  if (model.status !== 201) throw new Error(`POST /api/llm/connections/${id}/models answered ${model.status}`)
  await restoreOwnersDefault(((await model.json()) as { id: string }).id)
}

/** No spec meets the first-run setup by surprise: it is done for the whole run. first-run.spec.ts moves the flag and
 * puts it back itself. */
async function markSetupDone() {
  const response = await fetch(`${BASE}/api/setup`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ done: true }),
  })
  if (response.status !== 200) throw new Error(`PUT /api/setup answered ${response.status}: bring the stack up before the E2E run`)
}

/**
 * Records the owner's default model, and clears what an earlier run left behind. E2E runs on the owner's own
 * database, so every run has to hand it back exactly as it found it.
 */
export default async function globalSetup() {
  // First, before anything touches the stack: a run pointed at the owner's library stops here.
  await assertHealthyTestStack()
  await ensureDefaultModel()
  const left = await readOwnersDefault()
  // A record already here means an earlier run died before its teardown: its id, not whatever the default is
  // now (that run's own last pick), is the owner's. Putting it back first also keeps this run from recording
  // the wrong one. A leftover connection can hold the current default, so restore before sweeping.
  // A 404 means the model is gone (a record from before the test stack existed): drop the stale record and go on.
  const stale = left !== '' && (await restoreOwnersDefault(left).then(() => false, (error: unknown) => (error instanceof StaleModel ? true : Promise.reject(error))))
  if (stale) await rm(OWNERS_DEFAULT_FILE, { force: true })
  const ownersDefault = left !== '' && !stale ? left : await defaultModelId()
  if (ownersDefault === null) {
    await rm(OWNERS_DEFAULT_FILE, { force: true })
    console.warn('No default model to put back afterwards: this run leaves whichever one it sets.')
  } else {
    await writeFile(OWNERS_DEFAULT_FILE, ownersDefault)
  }
  await sweepE2EConnections()
  await markSetupDone()
}
