import { describe, expect, it } from 'vitest'
import { readerHash } from './deepLink'

const P = '3f2504e0-4f89-41d3-9a0c-0305e82c3301'
const C = '6ba7b810-9dad-11d1-80b4-00c04fd430c8'

describe('readerHash', () => {
  it('maps a paper link to the reader route', () => {
    expect(readerHash(`paperlab://papers/${P}`)).toBe(`#/papers/${P}`)
    expect(readerHash(`paperlab://papers/${P}?page=4`)).toBe(`#/papers/${P}?page=4`)
    expect(readerHash(`paperlab://papers/${P}?page=4&chunk=${C}`)).toBe(`#/papers/${P}?page=4&chunk=${C}`)
    expect(readerHash(`paperlab://papers/${P}?chunk=${C}`)).toBe(`#/papers/${P}?chunk=${C}`)
  })

  it('ignores unknown params but rejects bad known ones', () => {
    expect(readerHash(`paperlab://papers/${P}?utm=x&page=2`)).toBe(`#/papers/${P}?page=2`)
    for (const q of ['page=0', 'page=-1', 'page=1.5', 'page=abc', 'page=', 'chunk=nope', 'page=1&page=2']) {
      expect(readerHash(`paperlab://papers/${P}?${q}`)).toBeNull()
    }
  })

  it('refuses anything else', () => {
    for (const url of [
      'paperlab://papers/not-a-uuid',
      `paperlab://papers/${P}/extra`,
      `paperlab://other/${P}`,
      `https://papers/${P}`,
      `paperlab:papers/${P}`,
      'paperlab://papers/',
      'garbage',
    ]) {
      expect(readerHash(url)).toBeNull()
    }
  })
})

describe('linkIn', () => {
  it('finds the paperlab:// argument', async () => {
    const { linkIn } = await import('./deepLink')
    expect(linkIn(['app', '--x', `paperlab://papers/${P}`])).toBe(`paperlab://papers/${P}`)
    expect(linkIn(['app'])).toBeUndefined()
  })
})
