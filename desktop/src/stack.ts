/**
 * The launch (spec §5, steps 2–7) as a function of docker and a health probe, and what the window shows for each state
 * (§8). main.ts wires it to Electron; the tests run it over a fake runner and a fake clock.
 */
import { chmodSync, copyFileSync, mkdirSync, realpathSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { needsBackup, type BackupResult } from './backup'
import { composeVersionOk, isDockerDesktop, lastLine, type Docker, type Result } from './docker'
import type { StartupAction } from './navigation'

/** What a first launch downloads, in MB: the release image (164) plus Postgres and Redis (152 + 17), rounded up
 * (Task 5's measured CONTENT SIZE, in place of the spec's predicted 905). */
export const DOWNLOAD_MB = 335
const DOCKER_POLL_MS = 2_000
const DOCKER_WAIT_MS = 120_000
const HEALTH_POLL_MS = 1_000
const HEALTH_WAIT_MS = 120_000
const PERMISSION = /permission denied/i
const PORT_TAKEN = /port is already allocated|address already in use/i

export type Screen =
  | { kind: 'checking' }
  | { kind: 'docker-needed' }
  | { kind: 'starting-docker' }
  | { kind: 'start-docker' }
  | { kind: 'docker-permission' }
  | { kind: 'update-docker'; linux: boolean }
  | { kind: 'linux-containers' }
  | { kind: 'downloading'; line: string | null }
  | { kind: 'download-failed'; detail: string }
  | { kind: 'backing-up' }
  | { kind: 'backup-failed'; detail: string }
  | { kind: 'starting' }
  | { kind: 'port-in-use'; port: number; settingsFile: string }
  | { kind: 'didnt-start'; detail: string }
  | { kind: 'ready'; url: string }
  | { kind: 'stopping' }

export type Deps = {
  docker: Docker | null
  platform: NodeJS.Platform
  /** Opens Docker Desktop; null for any other Docker, and on Linux. Whether the found binary qualifies is decided by
   * `autoStartFor`, below, before `Deps` is built. */
  autoStart: (() => Promise<void>) | null
  probe: (url: string) => Promise<boolean>
  sleep: (ms: number) => Promise<void>
  now: () => number
  port: number
  settingsFile: string
  version: string
  lastVersion: string | null
  writeFiles: () => void
  backup: () => Promise<BackupResult>
  /** The lastVersion a backup has already been taken for this upgrade from (D142), so a retry after backup ok but a
   * later step failing doesn't dump again over the good backup. */
  backedUpFrom: string | null
  /** Records that this upgrade's backup is done, right after the rename. */
  markBackedUp: () => void
  /** Writes lastVersion once PaperLab answers, so after migrate has run. */
  markStarted: () => void
  /** Aborted when the window closes during startup. */
  signal?: AbortSignal
}

type Show = (screen: Screen) => void

const ok = (result: Result) => result.code === 0 && !result.timedOut
const going = (deps: Deps) => deps.signal?.aborted !== true

/** Steps 2–7: each screen through `show`, and the last one returned: `ready` with PaperLab's address, or what stopped
 * it. */
export async function launch(deps: Deps, show: Show): Promise<Screen> {
  show({ kind: 'checking' })
  const { docker } = deps
  const screen: Screen =
    docker === null
      ? { kind: 'docker-needed' }
      : ((await checkDocker(docker, deps, show)) ??
        writeFilesOrFail(deps) ??
        (await ensureImage(docker, deps, show)) ??
        (await backupIfUpgrading(docker, deps, show)) ??
        (await start(docker, deps, show)))
  show(screen)
  return screen
}

/** Steps 2–3: Docker answering (Docker Desktop opened and waited for), Linux containers, Compose 2.24 or later. */
async function checkDocker(docker: Docker, deps: Deps, show: Show): Promise<Screen | null> {
  let info = await docker.info()
  if (PERMISSION.test(info.stderr + info.stdout)) return { kind: 'docker-permission' }
  if (!ok(info) && deps.autoStart !== null) {
    show({ kind: 'starting-docker' })
    await deps.autoStart()
    const until = deps.now() + DOCKER_WAIT_MS
    while (!ok(info) && deps.now() < until && going(deps)) {
      await deps.sleep(DOCKER_POLL_MS)
      info = await docker.info()
    }
    if (!going(deps)) return { kind: 'stopping' }
  }
  if (!ok(info)) return { kind: 'start-docker' }
  if (info.stdout.trim() === 'windows') return { kind: 'linux-containers' }
  if (!composeVersionOk(await docker.composeVersion())) return { kind: 'update-docker', linux: deps.platform === 'linux' }
  return null
}

/** Step 4. */
function writeFilesOrFail(deps: Deps): Screen | null {
  try {
    deps.writeFiles()
    return null
  } catch (error) {
    return { kind: 'didnt-start', detail: `PaperLab couldn't write its files: ${String(error)}` }
  }
}

/** Step 5: this version's image, downloaded when it isn't here. An older version's image is never used instead. */
async function ensureImage(docker: Docker, deps: Deps, show: Show): Promise<Screen | null> {
  if (await docker.imagePresent()) return null
  show({ kind: 'downloading', line: null })
  const pulled = await docker.pull((line) => show({ kind: 'downloading', line }))
  if (!going(deps)) return { kind: 'stopping' }
  return ok(pulled) ? null : { kind: 'download-failed', detail: lastLine(pulled) ?? 'docker compose pull failed' }
}

/** Step 6 (D142): before a new version first starts, the database alone, then the backup. migrate waits for both. A
 * retry that already took this upgrade's dump (backedUpFrom === lastVersion) skips straight to migrate, so it never
 * dumps the now-migrated database over the good pre-upgrade backup. */
async function backupIfUpgrading(docker: Docker, deps: Deps, show: Show): Promise<Screen | null> {
  if (!needsBackup(deps.lastVersion, deps.version)) return null
  if (deps.backedUpFrom === deps.lastVersion) return null
  show({ kind: 'backing-up' })
  const database = await docker.startDatabase()
  if (!going(deps)) return { kind: 'stopping' }
  if (!ok(database)) return { kind: 'backup-failed', detail: lastLine(database) ?? "The database didn't start." }
  const backup = await deps.backup()
  if (!going(deps)) return { kind: 'stopping' }
  if (!backup.ok) return { kind: 'backup-failed', detail: backup.detail }
  deps.markBackedUp()
  return null
}

/** Step 7: compose up (it waits for migrate), then /api/health every second for up to 120 s. */
async function start(docker: Docker, deps: Deps, show: Show): Promise<Screen> {
  show({ kind: 'starting' })
  const up = await docker.up()
  if (!ok(up)) return upFailed(docker, deps, up)
  const address = `http://127.0.0.1:${deps.port}`
  const until = deps.now() + HEALTH_WAIT_MS
  while (deps.now() < until && going(deps)) {
    if (await deps.probe(`${address}/api/health`)) {
      deps.markStarted()
      return { kind: 'ready', url: `${address}/` }
    }
    await deps.sleep(HEALTH_POLL_MS)
  }
  if (!going(deps)) return { kind: 'stopping' }
  const logs = await docker.logs(['migrate', 'api'])
  return { kind: 'didnt-start', detail: logs.stdout.trim() || logs.stderr.trim() || "PaperLab didn't answer within 120 seconds." }
}

async function upFailed(docker: Docker, deps: Deps, up: Result): Promise<Screen> {
  const output = `${up.stdout}\n${up.stderr}`
  if (PORT_TAKEN.test(output)) return { kind: 'port-in-use', port: deps.port, settingsFile: deps.settingsFile }
  if (up.timedOut) return { kind: 'didnt-start', detail: 'Docker took longer than 5 minutes to start PaperLab.' }
  // Alembic's own line ("Can't locate revision …") is in migrate's logs; compose up only names the service.
  const migrate = /migrate/.test(output) ? lastLine(await docker.logs(['migrate'])) : null
  return { kind: 'didnt-start', detail: migrate ?? lastLine(up) ?? 'docker compose up failed.' }
}

/** Whether `dockerPath` (as `findDocker` returned it) is Docker Desktop's own CLI, for deciding `Deps.autoStart`.
 * Carried from Task 8's review: `findDocker` never resolves symlinks, and `/usr/local/bin/docker` is often one into
 * Docker.app, so the real path is checked, not the found one. `realpath` is a seam for tests; a path `realpath` can't
 * resolve (already gone, or the sandbox denies it) is judged as found. */
export function autoStartFor(
  dockerPath: string,
  platform: NodeJS.Platform,
  open: () => Promise<void>,
  realpath: (path: string) => string = realpathSync,
): (() => Promise<void>) | null {
  const real = resolve(dockerPath, realpath)
  return isDockerDesktop(real, platform) ? open : null
}

function resolve(path: string, realpath: (path: string) => string): string {
  try {
    return realpath(path)
  } catch {
    return path
  }
}

/** What the startup page draws (src/startup.html's show()). Text only; the page never reads it as HTML. */
export type View = {
  title: string
  body: string[]
  detail: string | null
  link: { href: string; label: string } | null
  actions: StartupAction[]
  busy: boolean
}

const view = (title: string, body: string[], more: Partial<View> = {}): View => ({
  title,
  body,
  detail: null,
  link: null,
  actions: [],
  busy: false,
  ...more,
})
const RETRY: StartupAction[] = ['retry']
const FAILED: StartupAction[] = ['retry', 'show-log']

type Views = { [K in Screen['kind']]: (screen: Extract<Screen, { kind: K }>) => View }

const VIEWS: Views = {
  checking: () => view('Opening PaperLab', ['Checking Docker…'], { busy: true }),
  'docker-needed': () =>
    view(
      'Docker needed',
      [
        'PaperLab runs on Docker Desktop. Install it, open it once, then press Retry.',
        'Any Docker that provides the docker command works too, such as OrbStack, Colima or Rancher Desktop.',
      ],
      { link: { href: 'https://www.docker.com/products/docker-desktop/', label: 'Get Docker Desktop' }, actions: RETRY },
    ),
  'starting-docker': () => view('Starting Docker', ['If Docker Desktop opens its own window, finish it there.'], { busy: true }),
  'start-docker': () => view('Start Docker', ["Docker isn't running. Start Docker Desktop or your own Docker, then press Retry."], { actions: RETRY }),
  'docker-permission': () =>
    view(
      'Docker refused access',
      ['On Linux, add your user to the docker group: sudo usermod -aG docker $USER, then log out and back in.'],
      { actions: RETRY },
    ),
  'update-docker': ({ linux }) =>
    view(
      'Update Docker',
      [
        linux
          ? 'PaperLab needs Docker Compose 2.24 or later. Install docker-compose-plugin from docker.com, then press Retry.'
          : 'PaperLab needs Docker Compose 2.24 or later. Update Docker Desktop, then press Retry.',
      ],
      { actions: RETRY },
    ),
  'linux-containers': () =>
    view(
      'Switch to Linux containers',
      ["Docker runs Windows containers. In Docker Desktop's menu, choose Switch to Linux containers, then press Retry."],
      { actions: RETRY },
    ),
  downloading: ({ line }) =>
    view('Downloading PaperLab', [`About ${DOWNLOAD_MB} MB the first time; an update downloads only what changed.`], {
      detail: line,
      busy: true,
    }),
  'download-failed': ({ detail }) =>
    view("Couldn't download PaperLab", ['Check the internet connection, then press Retry.'], { detail, actions: FAILED }),
  'backing-up': () =>
    view('Backing up your library', ['PaperLab saves a copy of your library before this version first starts.'], { busy: true }),
  'backup-failed': ({ detail }) =>
    view('Backup failed', ["This version didn't start, so your library is as it was."], { detail, actions: FAILED }),
  starting: () => view('Starting PaperLab', ['This takes a few seconds, longer after an update.'], { busy: true }),
  'port-in-use': ({ port, settingsFile }) =>
    view('Port in use', [`Another program is using port ${port}. Close it and Retry, or set "port" in ${settingsFile}.`], {
      actions: RETRY,
    }),
  'didnt-start': ({ detail }) =>
    view("Didn't start", ["PaperLab didn't start. Show log lists every Docker command it ran."], { detail, actions: FAILED }),
  ready: () => view('Opening PaperLab', [], { busy: true }),
  stopping: () => view('Stopping PaperLab', ['Your library is kept.'], { busy: true }),
}

export function viewFor(screen: Screen): View {
  return (VIEWS[screen.kind] as (screen: Screen) => View)(screen)
}

/** Step 4: the compose file and the MCP launcher, copied from the app on every launch so an update refreshes them.
 * With `binDir` (~/.local/bin, macOS and Linux), also a `paperlab-mcp` there that runs the launcher, the same file
 * `scripts/paperlab-mcp --install` writes, so Connect Claude's config and command read the same for every user. */
export function writeFiles(dataDir: string, sources: { compose: string; script: string }, binDir?: string): void {
  mkdirSync(join(dataDir, 'scripts'), { recursive: true })
  copyFileSync(sources.compose, join(dataDir, 'docker-compose.yml'))
  const script = join(dataDir, 'scripts', 'paperlab-mcp')
  copyFileSync(sources.script, script)
  chmodSync(script, 0o755)
  if (binDir === undefined) return
  mkdirSync(binDir, { recursive: true })
  const launcher = join(binDir, 'paperlab-mcp')
  writeFileSync(launcher, `#!/bin/sh\nexec '${script.replaceAll("'", "'\\''")}' "$@"\n`)
  chmodSync(launcher, 0o755)
}
