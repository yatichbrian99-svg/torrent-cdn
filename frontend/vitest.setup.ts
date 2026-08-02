import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// RTL 16 only auto-cleans when a global afterEach exists (vitest globals).
// Clean up explicitly so DOM never leaks between tests in a file.
afterEach(() => {
  cleanup()
})
