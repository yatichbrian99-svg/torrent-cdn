import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'path'

// Unit tests for frontend logic + components (vitest + jsdom + Testing Library).
// The '@' alias mirrors tsconfig.json paths (frontend root).
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, '.'),
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['**/*.test.{ts,tsx}'],
    // Coverage gate (v8 provider): every file under lib/ and components/ counts
    // toward the thresholds, tested or not — vitest 4 counts all files matched
    // by `include` (the old `all` flag was removed), so a new untested file or
    // a removed test drops coverage and fails CI.
    coverage: {
      provider: 'v8',
      include: ['lib/**', 'components/**'],
      // VideoPlayer is WebRTC/webtorrent glue — not meaningfully unit-testable
      // in jsdom (covered by next build + the embed e2e). Everything else in
      // lib/ and components/ counts, tested or not.
      exclude: ['**/VideoPlayer.tsx'],
      reporter: ['text-summary'],
      // Thresholds = measured baseline with all untested components counted
      // (Navbar/AuthModal/UploadForm at 0%) — raise them as coverage grows.
      thresholds: {
        statements: 25,
        branches: 33,
        functions: 14,
        lines: 32,
      },
    },
  },
})
