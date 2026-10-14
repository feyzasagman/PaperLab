import { describe, expect, it } from 'vitest'
import { assertTestStack } from './stackGuard'

describe('assertTestStack', () => {
  it.each(['paperlab', 'paperlab-app', '', undefined, 42])('refuses %s', (stack) => {
    expect(() => assertTestStack(stack, 'http://localhost:5180')).toThrow(/scripts\/e2e-stack\.sh up/)
  })
  it.each(['paperlab-e2e', 'paperlab-release-e2e'])('accepts %s', (stack) => {
    expect(() => assertTestStack(stack, 'http://localhost:5280')).not.toThrow()
  })
})
