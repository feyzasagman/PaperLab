import { describe, expect, it } from 'vitest'
import { claudeCodeCommand, configFileHint, desktopConfig, detectOs } from './connectClaude'

const MAC = '/Users/me/research-note'
const WINDOWS = 'C:\\Users\\me\\PaperLab'

describe('detectOs', () => {
  it('reads Windows, macOS and Linux from the platform the browser reports, and never guesses WSL', () => {
    expect(['Win32', 'Windows'].map(detectOs)).toEqual(['windows', 'windows'])
    expect(['MacIntel', 'macOS', 'iPhone', 'iPad'].map(detectOs)).toEqual(['macos', 'macos', 'macos', 'macos'])
    expect(['Linux x86_64', 'Linux', 'Chrome OS', ''].map(detectOs)).toEqual(['linux', 'linux', 'linux', 'linux'])
  })
})

describe('desktopConfig', () => {
  it('runs ~/.local/bin/paperlab-mcp through sh on macOS and Linux, the same for every user and folder', () => {
    const expected = `{
  "mcpServers": {
    "paperlab": {
      "command": "/bin/sh",
      "args": [
        "-c",
        "exec \\"$HOME/.local/bin/paperlab-mcp\\""
      ]
    }
  }
}`
    expect(desktopConfig(MAC, 'macos')).toBe(expected)
    expect(desktopConfig('/opt/PaperLab', 'linux')).toBe(expected)
  })

  it('starts wsl in the Linux home and runs .local/bin/paperlab-mcp from there on Windows + WSL', () => {
    expect(JSON.parse(desktopConfig('/home/me/PaperLab', 'wsl'))).toEqual({
      mcpServers: { paperlab: { command: 'wsl', args: ['--cd', '~', '-e', '.local/bin/paperlab-mcp'] } },
    })
  })

  it('calls docker compose directly on Windows', () => {
    expect(desktopConfig(WINDOWS, 'windows')).toBe(`{
  "mcpServers": {
    "paperlab": {
      "command": "docker",
      "args": [
        "compose",
        "-f",
        "C:\\\\Users\\\\me\\\\PaperLab\\\\docker-compose.yml",
        "exec",
        "-T",
        "api",
        "python",
        "-m",
        "mcp_server"
      ]
    }
  }
}`)
  })

  it('drops one trailing separator from the folder', () => {
    expect(desktopConfig(`${WINDOWS}\\`, 'windows')).toBe(desktopConfig(WINDOWS, 'windows'))
  })
})

describe('claudeCodeCommand', () => {
  it('runs ~/.local/bin/paperlab-mcp on macOS, Linux and WSL, the same for every user and folder', () => {
    const expected = 'claude mcp add -s user paperlab -- "$HOME/.local/bin/paperlab-mcp"'
    expect((['macos', 'linux', 'wsl'] as const).map((os) => claudeCodeCommand(MAC, os))).toEqual([
      expected,
      expected,
      expected,
    ])
  })

  it('calls docker compose directly on Windows', () => {
    expect(claudeCodeCommand(`${WINDOWS}\\`, 'windows')).toBe(
      'claude mcp add -s user paperlab -- docker compose -f "C:\\Users\\me\\PaperLab\\docker-compose.yml" exec -T api python -m mcp_server',
    )
  })
})

describe('configFileHint', () => {
  it("says where each system keeps Claude Desktop's config", () => {
    expect((['macos', 'windows', 'wsl', 'linux'] as const).map(configFileHint)).toEqual([
      '~/Library/Application Support/Claude/claude_desktop_config.json',
      '%APPDATA%\\Claude\\claude_desktop_config.json',
      '%APPDATA%\\Claude\\claude_desktop_config.json',
      'Open it from Claude Desktop: Settings → Developer → Edit Config.',
    ])
  })
})
