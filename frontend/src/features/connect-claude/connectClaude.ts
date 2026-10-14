/** What the Connect Claude page fills in for each system: Claude Desktop's config, Claude Code's command, the config file. */
export type SetupOs = 'macos' | 'windows' | 'wsl' | 'linux'

/** The system to preselect, from `navigator.userAgentData.platform` or `navigator.platform`. WSL looks like Windows
 * from the browser, so it is never guessed. */
export function detectOs(platform: string): SetupOs {
  if (/win/i.test(platform)) return 'windows'
  if (/mac|iphone|ipad/i.test(platform)) return 'macos'
  return 'linux'
}

/** The folder as typed, without surrounding spaces or one trailing `/` or `\`. */
const trimmed = (folder: string) => folder.trim().replace(/[/\\]$/, '')

/** Where `scripts/paperlab-mcp --install` and the desktop app put the launcher, the same for every user on macOS, Linux
 * and inside WSL. */
export const LAUNCHER = '$HOME/.local/bin/paperlab-mcp'

const COMPOSE_EXEC = ['exec', '-T', 'api', 'python', '-m', 'mcp_server']

/** The `mcpServers` entry to paste into claude_desktop_config.json. Claude Desktop doesn't expand $HOME in `command`,
 * so the launcher goes through sh (or wsl's `~`). Windows calls `docker compose` directly: Windows apps get the system
 * PATH Docker Desktop fills, and a `.cmd` wrapper won't start without a shell. */
export function desktopConfig(folder: string, os: SetupOs): string {
  const paperlab =
    os === 'windows'
      ? { command: 'docker', args: ['compose', '-f', `${trimmed(folder)}\\docker-compose.yml`, ...COMPOSE_EXEC] }
      : os === 'wsl'
        ? { command: 'wsl', args: ['--cd', '~', '-e', '.local/bin/paperlab-mcp'] }
        : { command: '/bin/sh', args: ['-c', `exec "${LAUNCHER}"`] }
  return JSON.stringify({ mcpServers: { paperlab } }, null, 2)
}

/** The one-line `claude mcp add` for Claude Code, run in a terminal (inside WSL for `wsl`). */
export function claudeCodeCommand(folder: string, os: SetupOs): string {
  if (os === 'windows') {
    return `claude mcp add -s user paperlab -- docker compose -f "${trimmed(folder)}\\docker-compose.yml" ${COMPOSE_EXEC.join(' ')}`
  }
  return `claude mcp add -s user paperlab -- "${LAUNCHER}"`
}

/** Where Claude Desktop keeps its config on each system. */
export function configFileHint(os: SetupOs): string {
  if (os === 'macos') return '~/Library/Application Support/Claude/claude_desktop_config.json'
  if (os === 'linux') return 'Open it from Claude Desktop: Settings → Developer → Edit Config.'
  return '%APPDATA%\\Claude\\claude_desktop_config.json'
}
