import { defineConfig } from '@playwright/test'

// A spec that marks a model default (PUT /api/llm/default) moves the one shared `is_default` row. Every spec
// using the `llmName`/`llmConnection` fixtures snapshots "the owner's default" at setup and restores it at
// teardown (fixtures.ts); if that snapshot races a concurrent default change, it restores the wrong model. Any
// such spec carries this tag, which puts it in its own single-worker project running after everything else, so
// nothing else is ever mid-test while the default moves. A tag, not a filename: a new spec that moves the
// default says so itself instead of quietly losing the protection. `@moves-paper-sources` is the same for the one
// paper-sources row: a test that changes the switches or the email must not run beside a search or an ingest.
// `@moves-setup` is the same for the one setup flag: a spec that sets it back to not done would send every other
// page to `#/setup`.
const MOVES_DEFAULT = /@moves-default|@moves-paper-sources|@moves-setup/

// A spec that needs the stack started with no search model (MODELS_DIR=/models/none, README) carries this tag: it
// runs only as its own project, on that stack, and skips itself on a stack that has the model.
const NO_SEARCH_MODEL = /@no-search-model/

// The release stack (desktop/docker-compose.yml as project paperlab-release-e2e on :5290: the release image, no search model), run as its own project
// with E2E_RELEASE=1 and E2E_BASE_URL=http://127.0.0.1:5290 (README, Development). These files show that the base
// download is enough to read, take notes and chat with short papers, and that the first-run setup works there.
const RELEASE_SPECS = /\/(first-run|library|reader-render|highlight-to-note|note-actions|chat|settings-connections)\.spec\.ts$/

// Runs against the test stack: scripts/e2e-stack.sh up first (D274). No mocked backend.
export default defineConfig({
  testDir: 'e2e',
  timeout: 60_000,
  // The run borrows the owner's own default model and leaves connections behind: these put both back even when
  // a run is killed mid-test, which no per-test teardown can cover.
  globalSetup: './e2e/globalSetup.ts',
  globalTeardown: './e2e/globalTeardown.ts',
  // One real backend shared by every worker: each test makes its own papers and workspaces, so none may assume
  // it owns the whole library. `--workers=1` runs serially.
  workers: 4,
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:5280',
    // Fail a stuck action in seconds, so a broken UI fails fast and fixtures still clean up.
    actionTimeout: 10_000,
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'parallel',
      grepInvert: [MOVES_DEFAULT, NO_SEARCH_MODEL],
    },
    {
      name: 'default-mover',
      grep: MOVES_DEFAULT,
      workers: 1,
      dependencies: ['parallel'],
    },
    {
      name: 'no-search-model',
      grep: NO_SEARCH_MODEL,
      workers: 1,
    },
    // Serial: first-run.spec.ts moves the setup flag, and nothing else may run meanwhile.
    ...(process.env.E2E_RELEASE === '1' ? [{ name: 'release', testMatch: RELEASE_SPECS, workers: 1 }] : []),
  ],
})
