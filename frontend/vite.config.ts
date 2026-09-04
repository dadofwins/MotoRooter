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
    /**
     * Process the app stylesheet, and only it, so a test can ask what a rule actually resolves
     * to.
     *
     * Vitest stubs CSS imports by default, which is right for nearly everything here — no test
     * needs a colour. It is wrong for the handful of behaviours CSS alone decides: whether a
     * newline in the transcript is drawn as a line break is one, and there is nothing in the
     * DOM to assert it on. With this, `import '../index.css'` injects the real sheet into the
     * document and `getComputedStyle` answers through the selector the component renders.
     *
     * Scoped by `include` rather than switched on wholesale: nothing else in `src` imports a
     * stylesheet today, so this changes the blast radius from every test to the one file that
     * asks for it, and leaves every other test running against no styles at all.
     */
    css: { include: [/index\.css$/] },
  },
})
