/** paperlab://papers/<uuid>[?page=N][&chunk=<uuid>] -> the reader's hash route. Strict: anything else is null. */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export function readerHash(url: string): string | null {
  let u: URL
  try {
    u = new URL(url)
  } catch {
    return null
  }
  const id = u.pathname.slice(1)
  if (u.protocol !== 'paperlab:' || u.hostname !== 'papers' || !UUID.test(id)) return null
  // Unknown params are ignored; a known one that is repeated or malformed rejects the whole link.
  const out: string[] = []
  for (const key of ['page', 'chunk']) {
    const values = u.searchParams.getAll(key)
    if (values.length === 0) continue
    const [v] = values
    const ok = key === 'page' ? /^[1-9]\d*$/.test(v) : UUID.test(v)
    if (values.length > 1 || !ok) return null
    out.push(`${key}=${v}`)
  }
  return `#/papers/${id}${out.length ? `?${out.join('&')}` : ''}`
}

/** The first paperlab:// argument of a command line, if any. */
export const linkIn = (argv: readonly string[]): string | undefined => argv.find((a) => a.startsWith('paperlab://'))
