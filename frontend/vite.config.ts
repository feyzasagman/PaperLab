/// <reference types="vitest/config" />
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  // The hand tracker's bundle has no imports of its own, so it is served as is: pre-bundling would only make the dev
  // server discover it on first use and reload the page in the middle of hands mode.
  optimizeDeps: { exclude: ['@mediapipe/tasks-vision'] },
  server: {
    // API_URL is http://api:8000 inside compose; the host default is for `npm run dev` on the host.
    proxy: { '/api': process.env.API_URL ?? 'http://localhost:8000' },
  },
  test: {
    include: ['src/**/*.test.{ts,tsx}', 'e2e/**/*.vitest.ts'],
    setupFiles: ['./src/test/setup.ts'],
  },
})
