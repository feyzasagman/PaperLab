/**
 * PaperLab's desktop app (spec §5): one window over the Docker stack. Wires the launch (stack.ts) to the startup page,
 * window safety, closing and the tray, the Settings switches (preload.ts), old images and the update check.
 */
import { spawn } from 'node:child_process'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { app, BrowserWindow, dialog, ipcMain, Menu, nativeImage, shell, Tray } from 'electron'
import type { IpcMainInvokeEvent, WebContents } from 'electron'
import { backUp } from './backup'
import { findDocker, makeDocker, spawnRunner, staleTags, usable, type Docker } from './docker'
import { linkIn, readerHash } from './deepLink'
import { fileLog } from './log'
import { navigationFor, startupAction, type StartupAction } from './navigation'
import { readSettings, settingsFile, updateSettings, type Settings } from './settings'
import { autoStartFor, launch, viewFor, writeFiles, type Deps, type Screen } from './stack'
import { checkForUpdate, type Update } from './updates'

// Tests only: a data folder of their own. Otherwise Electron's userData (spec §5), which also keys the single-instance
// lock. Must run before anything reads the path.
if (process.env.PAPERLAB_DATA_DIR) app.setPath('userData', process.env.PAPERLAB_DATA_DIR)

const DATA = app.getPath('userData')
const LOG_FILE = join(DATA, 'logs', 'main.log')
const log = fileLog(LOG_FILE)
const STARTUP_FILE = join(__dirname, '..', 'src', 'startup.html')
const STARTUP_URL = pathToFileURL(STARTUP_FILE).href
/** Development only: skip Docker and show a PaperLab that is already running, such as the dev stack on :5180. An
 * environment variable set for a packaged app is not a trusted seam, so this (and PAPERLAB_DOCKER_PATHS, below) are
 * honoured only unpackaged: what the tests and `npm start` run. PAPERLAB_DATA_DIR and PAPERLAB_DOCKER stay live in a
 * packaged app; the docs use them. */
const DEV_URL = app.isPackaged ? null : (process.env.PAPERLAB_URL ?? null)
/** What `findDocker` sees: PAPERLAB_DOCKER_PATHS dropped once packaged (see DEV_URL, above). */
const DOCKER_ENV: NodeJS.ProcessEnv = app.isPackaged ? { ...process.env, PAPERLAB_DOCKER_PATHS: undefined } : process.env
const RESOURCES = app.isPackaged ? process.resourcesPath : join(__dirname, '..')
const SOURCES = {
  compose: join(RESOURCES, 'docker-compose.yml'),
  script: app.isPackaged ? join(RESOURCES, 'scripts', 'paperlab-mcp') : join(RESOURCES, '..', 'scripts', 'paperlab-mcp'),
}

let settings: Settings = readSettings(DATA)
const APP_ORIGIN = DEV_URL === null ? `http://127.0.0.1:${settings.port}` : new URL(DEV_URL).origin
let win: BrowserWindow | null = null
let tray: Tray | null = null
let launching: AbortController | null = null
let quitting = false

function setSettings(patch: Partial<Settings>) {
  settings = updateSettings(DATA, settings, patch)
}

const locateDocker = () => findDocker(process.platform, DOCKER_ENV, homedir(), usable)
const dockerAt = (bin: string, signal?: AbortSignal) =>
  makeDocker(bin, spawnRunner, { dataDir: DATA, version: app.getVersion(), port: settings.port }, log, { signal })

/** Docker Desktop, opened when Docker isn't running (macOS, Windows). Any other Docker gets the Start Docker screen.
 * Whether `bin` qualifies is `autoStartFor`'s call (stack.ts): it resolves the real path and checks `isDockerDesktop`,
 * so that rule lives in one place instead of being re-decided here. */
function dockerDesktopStarter(bin: string | null): (() => Promise<void>) | null {
  if (bin === null) return null
  const programFiles = process.env.ProgramFiles ?? 'C:\\Program Files'
  const command = process.platform === 'darwin' ? 'open' : join(programFiles, 'Docker', 'Docker', 'Docker Desktop.exe')
  const args = process.platform === 'darwin' ? ['-a', 'Docker'] : []
  const open = async () => {
    const child = spawn(command, args, { detached: true, stdio: 'ignore' })
    child.on('error', (error) => log(`starting Docker Desktop failed: ${String(error)}`))
    child.unref()
  }
  return autoStartFor(bin, process.platform, open)
}

function launchDeps(docker: Docker | null, bin: string | null, signal: AbortSignal): Deps {
  return {
    docker,
    platform: process.platform,
    autoStart: dockerDesktopStarter(bin),
    probe: (url) => fetch(url, { signal: AbortSignal.timeout(2_000) }).then((response) => response.ok, () => false),
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    now: Date.now,
    port: settings.port,
    settingsFile: settingsFile(DATA),
    version: app.getVersion(),
    lastVersion: settings.lastVersion,
    writeFiles: () =>
      writeFiles(DATA, SOURCES, process.platform === 'win32' ? undefined : join(homedir(), '.local', 'bin'), log),
    backup: () => backUp(DATA, (path) => docker?.dumpTo(path) ?? Promise.reject(new Error('docker was not found'))),
    backedUpFrom: settings.backedUpFrom,
    markBackedUp: () => setSettings({ backedUpFrom: settings.lastVersion }),
    markStarted: () => setSettings({ lastVersion: app.getVersion(), backedUpFrom: null }),
    signal,
  }
}

/** The startup page draws each screen (its show() takes stack.ts's view). */
function show(screen: Screen) {
  win?.webContents
    .executeJavaScript(`window.show(${JSON.stringify(viewFor(screen))})`)
    .catch((error: unknown) => log(`startup page: ${String(error)}`))
}

/** Steps 2–8, and again on Retry. A new start ends the one before it, with its running docker command. */
async function start() {
  if (win === null) return
  launching?.abort()
  const controller = new AbortController()
  launching = controller
  await win.loadFile(STARTUP_FILE).catch((error: unknown) => log(`startup page: ${String(error)}`))
  if (DEV_URL !== null) return void win?.loadURL(DEV_URL).then(openPendingLink)
  const bin = locateDocker()
  const docker = bin === null ? null : dockerAt(bin, controller.signal)
  const screen = await launch(launchDeps(docker, bin, controller.signal), show).catch((error: unknown): Screen => {
    log(`the launch failed: ${String(error)}`)
    const failed: Screen = { kind: 'didnt-start', detail: String(error) }
    show(failed)
    return failed
  })
  if (launching !== controller || quitting) return
  launching = null
  if (screen.kind !== 'ready' || win === null) return
  await win.loadURL(screen.url).catch((error: unknown) => log(`loading PaperLab: ${String(error)}`))
  openPendingLink()
  if (docker !== null) void afterReady(docker)
}

/** Step 8: older versions' images go, then (packaged only) the update check. Failures are logged, never shown. */
async function afterReady(docker: Docker) {
  try {
    for (const tag of staleTags((await docker.imageTags()).stdout.split('\n'), app.getVersion())) await docker.removeImage(tag)
    if (!app.isPackaged) return
    const update = await checkForUpdate({
      enabled: settings.updatesEnabled,
      current: app.getVersion(),
      dismissed: settings.dismissedUpdate,
      fetch,
      log,
    })
    if (update !== null) await offerUpdate(update)
  } catch (error) {
    log(`after start: ${String(error)}`)
  }
}

async function offerUpdate(update: Update) {
  if (win === null) return
  const { response } = await dialog.showMessageBox(win, {
    type: 'info',
    message: `PaperLab ${update.version} is available`,
    detail: 'Download it, then install it over this one. Your library stays as it is.',
    buttons: ['Download', 'Later'],
    defaultId: 0,
    cancelId: 1,
  })
  if (response === 0) void shell.openExternal(update.url)
  else setSettings({ dismissedUpdate: update.version })
}

/** Window safety (spec §5): PaperLab and the startup page stay; other http(s) addresses open in the system browser. */
function guard(contents: WebContents) {
  // will-navigate misses a server-issued redirect and a sub-frame's own navigation (Electron ≥ 25); the same check
  // applies to all three. will-frame-navigate hands its url on the event itself, not as a second argument.
  const navigate = (event: { preventDefault: () => void }, url: string) => {
    const action = startupAction(url, STARTUP_URL)
    const where = navigationFor(url, APP_ORIGIN, STARTUP_URL)
    if (action === null && where === 'allow') return
    event.preventDefault()
    if (action !== null) onAction(action)
    else if (where === 'external') void shell.openExternal(url)
  }
  contents.on('will-navigate', (event, url) => navigate(event, url))
  contents.on('will-redirect', (event, url) => navigate(event, url))
  contents.on('will-frame-navigate', (event) => navigate(event, event.url))
  contents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) void shell.openExternal(url)
    return { action: 'deny' }
  })
}

function onAction(action: StartupAction) {
  if (action === 'retry') void start()
  else void shell.openPath(LOG_FILE)
}

function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: 'PaperLab',
    icon: join(resourcesDir(), 'icon.png'),
    webPreferences: { preload: join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true, nodeIntegration: false },
  })
  guard(win.webContents)
  win.on('close', (event) => {
    if (quitting) return
    event.preventDefault()
    if (settings.keepRunning) win?.hide()
    else app.quit() // before-quit stops the stack first
  })
  win.on('closed', () => {
    win = null
  })
}

function showWindow() {
  if (win === null) {
    createWindow()
    void start()
    return
  }
  if (win.isMinimized()) win.restore()
  win.show()
  win.focus()
}

/** A paperlab:// link waits here until PaperLab's own page is showing, then the window moves to its reader route. */
let pendingHash: string | null = null

function openPendingLink() {
  if (pendingHash === null || win === null) return
  let origin = ''
  try {
    origin = new URL(win.webContents.getURL()).origin
  } catch {
    return
  }
  if (origin !== APP_ORIGIN) return
  const hash = pendingHash
  pendingHash = null
  void win.loadURL(`${APP_ORIGIN}/${hash}`).catch((error: unknown) => log(`opening a link: ${String(error)}`))
}

function openLink(url: string | undefined) {
  if (url === undefined) return
  const hash = readerHash(url)
  if (hash === null) return log('ignored a paperlab:// link that is not a paper link')
  pendingHash = hash
  showWindow()
  openPendingLink()
}

type Switch = 'keepRunning' | 'updatesEnabled'
const isSwitch = (name: unknown): name is Switch => name === 'keepRunning' || name === 'updatesEnabled'

/** Only PaperLab's own page may read or flip the switches, and only with a boolean. */
function fromPaperLab(event: IpcMainInvokeEvent): boolean {
  try {
    return new URL(event.senderFrame?.url ?? '').origin === APP_ORIGIN
  } catch {
    return false
  }
}

function handleSwitches() {
  ipcMain.handle('paperlab:get', (event, name: unknown) => {
    if (!fromPaperLab(event) || !isSwitch(name)) throw new Error('refused')
    return settings[name]
  })
  ipcMain.handle('paperlab:set', (event, name: unknown, value: unknown) => {
    if (!fromPaperLab(event) || !isSwitch(name) || typeof value !== 'boolean') throw new Error('refused')
    setSettings(name === 'keepRunning' ? { keepRunning: value } : { updatesEnabled: value })
    syncTray()
  })
}

/** Packaged: electron-builder's extraResources. Unpackaged: build/, where npm run icons writes them. */
function resourcesDir() {
  return app.isPackaged ? process.resourcesPath : join(__dirname, '..', 'build')
}

/** macOS: the menu-bar template image, which follows the bar's colour. Elsewhere: the app icon at tray size. */
function trayImage() {
  const dir = resourcesDir()
  if (process.platform === 'darwin') return nativeImage.createFromPath(join(dir, 'trayTemplate.png'))
  return nativeImage.createFromPath(join(dir, 'icon.png')).resize({ width: 16, height: 16 })
}

/** The tray exists while Keep running is on: Open PaperLab and Quit PaperLab. */
function syncTray() {
  if (!settings.keepRunning) {
    tray?.destroy()
    tray = null
    return
  }
  if (tray !== null) return
  tray = new Tray(trayImage())
  tray.setToolTip('PaperLab')
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: 'Open PaperLab', click: showWindow },
      { label: 'Quit PaperLab', click: () => app.quit() },
    ]),
  )
}

/** Quit always stops PaperLab (spec §5): compose stop gets 30 s, then the app exits whatever happened, so logout and
 * shutdown are never held up. */
async function stopAndExit() {
  quitting = true
  launching?.abort()
  tray?.destroy()
  if (win !== null && DEV_URL === null) {
    await win.loadFile(STARTUP_FILE).catch(() => undefined)
    show({ kind: 'stopping' })
  }
  const bin = DEV_URL === null ? locateDocker() : null
  if (bin !== null) {
    const stopped = await dockerAt(bin).stop()
    if (stopped.code !== 0 || stopped.timedOut) log('compose stop failed or took over 30 s; exiting anyway')
  }
  app.exit(0)
}

if (!app.requestSingleInstanceLock()) {
  // A second launch: the first instance shows its window (second-instance below). Exit without stopping anything.
  app.exit(0)
} else {
  app.setAsDefaultProtocolClient('paperlab')
  app.on('open-url', (event, url) => {
    event.preventDefault()
    openLink(url)
  })
  app.on('second-instance', (_event, argv) => {
    if (linkIn(argv) === undefined) showWindow()
    else openLink(linkIn(argv))
  })
  pendingHash = readerHash(linkIn(process.argv) ?? '') // Windows/Linux: the first launch came from a link
  app.on('activate', showWindow)
  app.on('before-quit', (event) => {
    if (quitting) return
    event.preventDefault()
    void stopAndExit()
  })
  app.on('window-all-closed', () => app.quit())
  handleSwitches()
  void app.whenReady().then(() => {
    createWindow()
    syncTray()
    void start()
  })
}
