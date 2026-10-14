import { mkdirSync, mkdtempSync, readFileSync, statSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { makeDocker, type Result, type Runner } from './docker'
import { autoStartFor, DOWNLOAD_MB, launch, viewFor, writeFiles, type Deps, type Screen } from './stack'

type Reply = Partial<Result> & { lines?: string[] }
/** How docker answers a command, given how many times that same command has run. */
type Answer = (times: number) => Reply
const always = (reply: Reply): Answer => () => reply
const NOT_RUNNING = { code: 1, stderr: 'Cannot connect to the Docker daemon at unix:///var/run/docker.sock.\n' }
const STANDARD: Array<[RegExp, Answer]> = [
  [/^info /, always({ stdout: 'linux\n' })],
  [/^compose version/, always({ stdout: '2.29.1\n' })],
]

/** A docker over a fake runner: the first answer whose pattern matches the command, else success. Commands are
 * recorded without `-f /data/docker-compose.yml`, to read shorter. */
function fakeDocker(answers: Array<[RegExp, Answer]> = []) {
  const calls: string[] = []
  const run: Runner = async (_file, args, options) => {
    const command = args.join(' ').replace(' -f /data/docker-compose.yml', '')
    calls.push(command)
    const answer = [...answers, ...STANDARD].find(([pattern]) => pattern.test(command))?.[1]
    const { lines = [], ...reply } = answer?.(calls.filter((call) => call === command).length) ?? {}
    for (const line of lines) options.onLine?.(line)
    return { code: 0, stdout: '', stderr: '', timedOut: false, ...reply }
  }
  const docker = makeDocker('/usr/local/bin/docker', run, { dataDir: '/data', version: '0.2.0', port: 5190 }, () => {}, {
    platform: 'darwin',
    env: {},
  })
  return { docker, calls }
}

/** The launch's other inputs: a fake clock, a probe that answers at once, the same version as last time. */
function deps(docker: Deps['docker'], more: Partial<Deps> = {}) {
  const clock = { now: 0 }
  const events: string[] = []
  const all: Deps = {
    docker,
    platform: 'darwin',
    autoStart: null,
    probe: async () => true,
    sleep: async (ms) => void (clock.now += ms),
    now: () => clock.now,
    port: 5190,
    settingsFile: '/data/settings.json',
    version: '0.2.0',
    lastVersion: '0.2.0',
    writeFiles: () => void events.push('write files'),
    backup: async () => {
      events.push('backup')
      return { ok: true }
    },
    markStarted: () => void events.push('started'),
    backedUpFrom: null,
    markBackedUp: () => void events.push('marked backed up'),
    ...more,
  }
  return { deps: all, events, clock }
}

async function run(all: Deps) {
  const shown: Screen[] = []
  const last = await launch(all, (screen) => void shown.push(screen))
  return { last, shown: shown.map((screen) => screen.kind) }
}

describe('launch', () => {
  it('starts PaperLab and opens it once it answers, writing its files first and marking the version started', async () => {
    const { docker, calls } = fakeDocker()
    const { deps: all, events } = deps(docker)

    const { last, shown } = await run(all)

    expect(last).toEqual({ kind: 'ready', url: 'http://127.0.0.1:5190/' })
    expect(shown).toEqual(['checking', 'starting', 'ready'])
    expect(calls).toEqual([
      'info --format {{.OSType}}',
      'compose version --short',
      'image inspect ghcr.io/khannoussi-malek/paperlab:0.2.0',
      'compose up -d',
    ])
    expect(events).toEqual(['write files', 'started'])
  })

  it('with no docker at all, says Docker is needed', async () => {
    expect((await run(deps(null).deps)).last).toEqual({ kind: 'docker-needed' })
  })

  it('opens Docker Desktop when it is not running, asks every 2 s for 120 s, then says to start Docker', async () => {
    const { docker, calls } = fakeDocker([[/^info /, always(NOT_RUNNING)]])
    const opened: string[] = []
    const { deps: all, clock } = deps(docker, { autoStart: async () => void opened.push('Docker Desktop') })

    const { last, shown } = await run(all)

    expect(last).toEqual({ kind: 'start-docker' })
    expect(shown).toEqual(['checking', 'starting-docker', 'start-docker'])
    expect(opened).toEqual(['Docker Desktop'])
    expect(calls.filter((call) => call.startsWith('info'))).toHaveLength(61)
    expect(clock.now).toBe(120_000)
  })

  it('goes on to start PaperLab once Docker Desktop answers', async () => {
    const { docker } = fakeDocker([[/^info /, (times) => (times < 3 ? NOT_RUNNING : { stdout: 'linux\n' })]])

    expect((await run(deps(docker, { autoStart: async () => {} }).deps)).last.kind).toBe('ready')
  })

  it("says to start Docker at once for any other Docker, or one that didn't answer in time", async () => {
    const down = fakeDocker([[/^info /, always(NOT_RUNNING)]])
    expect((await run(deps(down.docker).deps)).last).toEqual({ kind: 'start-docker' })
    expect(down.calls).toHaveLength(1)

    const slow = fakeDocker([[/^info /, always({ timedOut: true, code: -1 })]])
    expect((await run(deps(slow.docker).deps)).last).toEqual({ kind: 'start-docker' })
  })

  it('names the docker group when Docker refuses access', async () => {
    const refused = { code: 1, stderr: 'permission denied while trying to connect to the Docker daemon socket\n' }
    const { docker } = fakeDocker([[/^info /, always(refused)]])

    expect((await run(deps(docker, { autoStart: async () => {} }).deps)).last).toEqual({ kind: 'docker-permission' })
  })

  it('asks for Linux containers when Docker runs Windows ones', async () => {
    const { docker } = fakeDocker([[/^info /, always({ stdout: 'windows\n' })]])

    expect((await run(deps(docker).deps)).last).toEqual({ kind: 'linux-containers' })
  })

  it('asks to update Docker for Compose older than 2.24 or none, saying what to install on Linux', async () => {
    const old = fakeDocker([[/^compose version/, always({ stdout: '2.23.3\n' })]])
    expect((await run(deps(old.docker).deps)).last).toEqual({ kind: 'update-docker', linux: false })

    const none = fakeDocker([[/^compose version/, always({ code: 1, stderr: "docker: 'compose' is not a docker command.\n" })]])
    expect((await run(deps(none.docker, { platform: 'linux' }).deps)).last).toEqual({ kind: 'update-docker', linux: true })
  })

  it("downloads a missing image showing the last progress line, and a failed download shows why", async () => {
    const missing: [RegExp, Answer] = [/^image inspect/, always({ code: 1 })]
    const pulled = fakeDocker([missing, [/^compose pull/, always({ lines: ['db Pulling', 'api Pull complete'] })]])
    const shown: Screen[] = []
    await launch(deps(pulled.docker).deps, (screen) => void shown.push(screen))
    expect(shown.filter((screen) => screen.kind === 'downloading')).toEqual([
      { kind: 'downloading', line: null },
      { kind: 'downloading', line: 'db Pulling' },
      { kind: 'downloading', line: 'api Pull complete' },
    ])

    const offline = fakeDocker([missing, [/^compose pull/, always({ code: 1, stderr: 'dial tcp: lookup ghcr.io: no such host\n' })]])
    expect((await run(deps(offline.docker).deps)).last).toEqual({
      kind: 'download-failed',
      detail: 'dial tcp: lookup ghcr.io: no such host',
    })
  })

  it('backs up an upgraded library with the database alone, before migrate; a fresh install has nothing to back up', async () => {
    const upgrade = fakeDocker()
    const { deps: all, events } = deps(upgrade.docker, { lastVersion: '0.1.0' })
    const { last, shown } = await run(all)
    expect(last.kind).toBe('ready')
    expect(shown).toEqual(['checking', 'backing-up', 'starting', 'ready'])
    expect(upgrade.calls.slice(-2)).toEqual(['compose up -d --wait db', 'compose up -d'])
    expect(events).toEqual(['write files', 'backup', 'marked backed up', 'started'])

    const fresh = fakeDocker()
    const first = deps(fresh.docker, { lastVersion: null })
    await run(first.deps)
    expect(first.events).toEqual(['write files', 'started'])
    expect(fresh.calls).not.toContain('compose up -d --wait db')
  })

  it('skips the backup on a retry that already took it for this upgrade, going straight to migrate', async () => {
    const { docker, calls } = fakeDocker()
    const { deps: all, events } = deps(docker, { lastVersion: '0.1.0', backedUpFrom: '0.1.0' })

    const { last } = await run(all)

    expect(last.kind).toBe('ready')
    expect(events).not.toContain('backup')
    expect(calls).not.toContain('compose up -d --wait db')
  })

  it('stops at Backup failed when pg_dump fails, and migrate never runs', async () => {
    const { docker, calls } = fakeDocker()
    const failing = deps(docker, { lastVersion: '0.1.0', backup: async () => ({ ok: false, detail: 'pg_dump: error: denied' }) })

    const { last } = await run(failing.deps)

    expect(last).toEqual({ kind: 'backup-failed', detail: 'pg_dump: error: denied' })
    expect(calls).not.toContain('compose up -d')
    expect(failing.events).not.toContain('started')
  })

  it('says which port is taken, and where to change it', async () => {
    for (const stderr of ['Bind for 127.0.0.1:5190 failed: port is already allocated', 'listen tcp 127.0.0.1:5190: bind: address already in use']) {
      const { docker } = fakeDocker([[/^compose up -d$/, always({ code: 1, stderr })]])
      expect((await run(deps(docker).deps)).last).toEqual({ kind: 'port-in-use', port: 5190, settingsFile: '/data/settings.json' })
    }
  })

  it("shows up's last line when it fails, Alembic's own line when migrate fails, and says when up took over 5 minutes", async () => {
    const refused = fakeDocker([[/^compose up -d$/, always({ code: 1, stderr: 'Error response from daemon: no space left on device\n' })]])
    expect((await run(deps(refused.docker).deps)).last).toEqual({
      kind: 'didnt-start',
      detail: 'Error response from daemon: no space left on device',
    })

    const downgraded = fakeDocker([
      [/^compose up -d$/, always({ code: 1, stderr: 'service "migrate" didn\'t complete successfully: exit 1\n' })],
      [/^compose logs --no-color --tail 20 migrate$/, always({ stdout: "migrate-1  | FAILED: Can't locate revision identified by '0014'\n" })],
    ])
    expect((await run(deps(downgraded.docker).deps)).last).toEqual({
      kind: 'didnt-start',
      detail: "migrate-1  | FAILED: Can't locate revision identified by '0014'",
    })

    const slow = fakeDocker([[/^compose up -d$/, always({ code: -1, timedOut: true })]])
    expect((await run(deps(slow.docker).deps)).last).toEqual({
      kind: 'didnt-start',
      detail: 'Docker took longer than 5 minutes to start PaperLab.',
    })
  })

  it("asks /api/health every second for 120 s, then shows the migrate and api logs", async () => {
    const logs = 'migrate-1  | INFO  [alembic.runtime.migration] Will assume transactional DDL.\napi-1  | ERROR: boom'
    const { docker } = fakeDocker([[/^compose logs --no-color --tail 20 migrate api$/, always({ stdout: `${logs}\n` })]])
    const asked: string[] = []
    const probe = async (url: string) => {
      asked.push(url)
      return false
    }
    const { deps: all, clock, events } = deps(docker, { probe })

    expect((await run(all)).last).toEqual({ kind: 'didnt-start', detail: logs })
    expect(asked).toHaveLength(120)
    expect(asked[0]).toBe('http://127.0.0.1:5190/api/health')
    expect(clock.now).toBe(120_000)
    expect(events).not.toContain('started')
  })

  it('ends without another docker command once the window is closed', async () => {
    const { docker, calls } = fakeDocker()
    const closing = new AbortController()
    const probe = async () => {
      closing.abort() // the window closes while PaperLab is still starting
      return false
    }
    const { deps: all } = deps(docker, { signal: closing.signal, probe })

    expect((await run(all)).last).toEqual({ kind: 'stopping' })
    expect(calls.at(-1)).toBe('compose up -d')
  })

  it('stops without checking Compose when the window closes while waiting for Docker Desktop', async () => {
    const closing = new AbortController()
    // The second `info` answers ok, as in "goes on to start PaperLab once Docker Desktop answers" — but the window
    // closes during the poll that fetches it, so nothing past the wait loop may run.
    const { docker, calls } = fakeDocker([[/^info /, (times) => (times < 2 ? NOT_RUNNING : { stdout: 'linux\n' })]])
    const sleep = async () => void closing.abort() // the window closes while waiting for Docker Desktop
    const { deps: all } = deps(docker, { autoStart: async () => {}, signal: closing.signal, sleep })

    expect((await run(all)).last).toEqual({ kind: 'stopping' })
    expect(calls).toEqual(['info --format {{.OSType}}', 'info --format {{.OSType}}'])
  })

  it('stops without acting on the pull when the window closes during the download', async () => {
    const missing: [RegExp, Answer] = [/^image inspect/, always({ code: 1 })]
    const { docker, calls } = fakeDocker([missing, [/^compose pull/, always({ lines: ['db Pulling'] })]])
    const closing = new AbortController()
    const shown: Screen[] = []
    const showAndClose = (screen: Screen) => {
      shown.push(screen)
      if (screen.kind === 'downloading' && screen.line !== null) closing.abort() // the window closes mid-pull
    }
    const { deps: all } = deps(docker, { signal: closing.signal })

    expect(await launch(all, showAndClose)).toEqual({ kind: 'stopping' })
    expect(calls).not.toContain('compose up -d')
  })

  it('stops without starting PaperLab when the window closes during the backup', async () => {
    const { docker, calls } = fakeDocker()
    const closing = new AbortController()
    const backup: Deps['backup'] = async () => {
      closing.abort() // the window closes mid-backup
      return { ok: true }
    }
    const { deps: all } = deps(docker, { lastVersion: '0.1.0', signal: closing.signal, backup })

    expect((await run(all)).last).toEqual({ kind: 'stopping' })
    expect(calls).not.toContain('compose up -d') // migrate runs inside `compose up -d`
  })

  it("says so when its files can't be written", async () => {
    const { docker } = fakeDocker()
    const { deps: all } = deps(docker, {
      writeFiles: () => {
        throw new Error('EACCES: permission denied')
      },
    })

    expect((await run(all)).last).toEqual({
      kind: 'didnt-start',
      detail: "PaperLab couldn't write its files: Error: EACCES: permission denied",
    })
  })
})

describe('viewFor', () => {
  it('titles every screen as the spec names it', () => {
    const titles = (
      [
        [{ kind: 'docker-needed' }, 'Docker needed'],
        [{ kind: 'starting-docker' }, 'Starting Docker'],
        [{ kind: 'start-docker' }, 'Start Docker'],
        [{ kind: 'update-docker', linux: false }, 'Update Docker'],
        [{ kind: 'linux-containers' }, 'Switch to Linux containers'],
        [{ kind: 'downloading', line: null }, 'Downloading PaperLab'],
        [{ kind: 'download-failed', detail: 'x' }, "Couldn't download PaperLab"],
        [{ kind: 'backing-up' }, 'Backing up your library'],
        [{ kind: 'backup-failed', detail: 'x' }, 'Backup failed'],
        [{ kind: 'port-in-use', port: 5190, settingsFile: '/data/settings.json' }, 'Port in use'],
        [{ kind: 'didnt-start', detail: 'x' }, "Didn't start"],
        [{ kind: 'stopping' }, 'Stopping PaperLab'],
      ] as Array<[Screen, string]>
    ).map(([screen, title]) => [viewFor(screen).title, title])

    for (const [shown, spec] of titles) expect(shown).toBe(spec)
  })

  it("carries the spec's sentences, the last line, and each screen's way out", () => {
    const needed = viewFor({ kind: 'docker-needed' })
    expect(needed.body[0]).toContain('PaperLab runs on Docker Desktop')
    expect(needed.link).toEqual({ href: 'https://www.docker.com/products/docker-desktop/', label: 'Get Docker Desktop' })
    expect(needed.actions).toEqual(['retry'])
    expect(viewFor({ kind: 'starting-docker' }).body).toEqual(['If Docker Desktop opens its own window, finish it there.'])
    expect(viewFor({ kind: 'downloading', line: 'api Pulling' })).toMatchObject({
      body: [`About ${DOWNLOAD_MB} MB the first time; an update downloads only what changed.`],
      detail: 'api Pulling',
      busy: true,
    })
    expect(viewFor({ kind: 'port-in-use', port: 5191, settingsFile: '/data/settings.json' }).body).toEqual([
      'Another program is using port 5191. Close it and Retry, or set "port" in /data/settings.json.',
    ])
    expect(viewFor({ kind: 'backup-failed', detail: 'pg_dump: error' })).toMatchObject({
      detail: 'pg_dump: error',
      actions: ['retry', 'show-log'],
    })
    expect(viewFor({ kind: 'update-docker', linux: true }).body[0]).toContain('docker-compose-plugin')
  })
})

describe('writeFiles', () => {
  it('copies the compose file and the MCP launcher on every launch, the launcher runnable', () => {
    const from = mkdtempSync(join(tmpdir(), 'paperlab-resources-'))
    const data = join(mkdtempSync(join(tmpdir(), 'paperlab-data-')), 'PaperLab')
    const sources = { compose: join(from, 'docker-compose.yml'), script: join(from, 'paperlab-mcp') }
    writeFileSync(sources.compose, 'name: paperlab-app\n')
    writeFileSync(sources.script, '#!/bin/sh\n', { mode: 0o644 })

    writeFiles(data, sources)
    writeFileSync(sources.compose, 'name: paperlab-app # updated\n')
    writeFiles(data, sources) // an update refreshes both

    expect(readFileSync(join(data, 'docker-compose.yml'), 'utf8')).toBe('name: paperlab-app # updated\n')
    expect(readFileSync(join(data, 'scripts', 'paperlab-mcp'), 'utf8')).toBe('#!/bin/sh\n')
    expect(statSync(join(data, 'scripts', 'paperlab-mcp')).mode & 0o777).toBe(0o755)
  })

  it("writes ~/.local/bin/paperlab-mcp running the data folder's launcher, so Claude's setup reads the same for everyone", () => {
    const from = mkdtempSync(join(tmpdir(), 'paperlab-resources-'))
    const data = join(mkdtempSync(join(tmpdir(), 'paperlab-data-')), "Bob's PaperLab")
    const bin = join(mkdtempSync(join(tmpdir(), 'paperlab-home-')), '.local', 'bin')
    const sources = { compose: join(from, 'docker-compose.yml'), script: join(from, 'paperlab-mcp') }
    writeFileSync(sources.compose, 'name: paperlab-app\n')
    writeFileSync(sources.script, '#!/bin/sh\n')

    writeFiles(data, sources, bin)

    const launcher = join(data, 'scripts', 'paperlab-mcp').replaceAll("'", "'\\''")
    expect(readFileSync(join(bin, 'paperlab-mcp'), 'utf8')).toBe(`#!/bin/sh\nexec '${launcher}' "$@"\n`)
    expect(statSync(join(bin, 'paperlab-mcp')).mode & 0o777).toBe(0o755)
  })
})

// Not in the brief: carried from Task 8's review. findDocker never resolves symlinks, and /usr/local/bin/docker is
// often one into Docker.app, so autoStartFor resolves the real path before judging it.
describe('autoStartFor', () => {
  it('counts a symlink into Docker.app as Docker Desktop', async () => {
    const appDir = mkdtempSync(join(tmpdir(), 'paperlab-dockerapp-'))
    const real = join(appDir, 'Docker.app', 'Contents', 'Resources', 'bin', 'docker')
    mkdirSync(dirname(real), { recursive: true })
    writeFileSync(real, '')
    const link = join(appDir, 'docker')
    symlinkSync(real, link)
    const open = async () => {}

    expect(autoStartFor(link, 'darwin', open)).toBe(open)
  })

  it('is not Docker Desktop for any other docker, and a broken path is judged as given', async () => {
    const open = async () => {}

    expect(autoStartFor('/opt/homebrew/bin/docker', 'darwin', open)).toBeNull()
    expect(autoStartFor('/no/such/docker', 'darwin', open, () => {
      throw new Error('ENOENT')
    })).toBeNull()
  })
})
