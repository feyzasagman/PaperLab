/** E2E runs only against a test stack (D273): a compose project whose name ends in -e2e, never the owner's library. */
export function assertTestStack(stack: unknown, base: string): void {
  if (typeof stack === 'string' && stack.endsWith('-e2e')) return
  throw new Error(
    `${base} is the stack ${JSON.stringify(stack ?? null)}, not a test stack. E2E never runs against the owner's ` +
      'library: start the test stack with scripts/e2e-stack.sh up and run against http://localhost:5280.',
  )
}
