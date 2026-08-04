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
      // Thresholds = measured app-code baseline (v8 skips test files by
      // default, so these numbers exclude the spec files themselves). Raised
      // after lib/security.ts landed fully tested: measured 46.7/50.7/35.4/54.7
      // — thresholds sit ~4-5pts under to absorb minor run-to-run variance
      // (Navbar/AuthModal/UploadForm remain untested components).
      thresholds: {
        statements: 42,
        branches: 45,
        functions: 30,
        lines: 48,
      },
    },
  },
})
