import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

/** A PNG's width and height, from its IHDR chunk. */
function pngSize(file: string): [number, number] {
  const png = readFileSync(file)
  return [png.readUInt32BE(16), png.readUInt32BE(20)]
}

describe('packaging', () => {
  it("has the installers' icon and the menu-bar template images", () => {
    expect(pngSize('build/icon.png')).toEqual([1024, 1024])
    expect(pngSize('build/trayTemplate.png')).toEqual([16, 16])
    expect(pngSize('build/trayTemplate@2x.png')).toEqual([32, 32])
  })

  const config = () => JSON.parse(readFileSync('package.json', 'utf8'))

  it('signs macOS builds ad hoc, since there is no Developer ID yet, and builds every installer the release lists', () => {
    const { version, build } = config()

    expect(version).toMatch(/^\d+\.\d+\.\d+$/)
    expect(build.mac).toMatchObject({ identity: '-', hardenedRuntime: false, target: [{ target: 'dmg', arch: ['arm64', 'x64'] }] })
    expect(build.win.target).toEqual([{ target: 'nsis', arch: ['x64'] }])
    expect(build.linux.target).toEqual([
      { target: 'AppImage', arch: ['x64'] },
      { target: 'deb', arch: ['x64'] },
    ])
    expect(build.linux.maintainer).toBeTruthy() // a .deb needs one; it keeps an email out of the package
  })

  it('tells macOS why it may ask for the camera (Hands mode), or the system would refuse it', () => {
    const { build } = config()

    expect(build.mac.extendInfo).toEqual({
      NSCameraUsageDescription:
        'PaperLab uses the camera only while Hands mode is on, to follow your hands. The picture stays on this computer.',
    })
  })

  it('ships the startup page, the compose file, the MCP launcher and the tray images', () => {
    const { build } = config()

    expect(build.files).toContain('src/startup.html')
    expect(build.extraResources.map((resource: { to: string }) => resource.to)).toEqual([
      'docker-compose.yml',
      'scripts/paperlab-mcp',
      'icon.png',
      'trayTemplate.png',
      'trayTemplate@2x.png',
    ])
  })
})
