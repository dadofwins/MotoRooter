import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  server: {
    // Dev only. In production the FastAPI container serves this bundle from the same
    // origin, so there is no proxy and no CORS.
    proxy: { '/api': 'http://localhost:8000' },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
  },
})
