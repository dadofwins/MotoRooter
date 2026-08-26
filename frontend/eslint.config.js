import js from '@eslint/js'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  {
    ignores: [
      'dist',
      'node_modules',
      // Generated from the backend OpenAPI document, never hand-edited.
      'src/api/schema.ts',
      // Vitest writes these next to vite.config.ts while loading it and deletes them
      // straight after. Without this, a lint run overlapping a test run intermittently
      // dies with ENOENT on a file that no longer exists — a CI flake, not a code defect.
      'vite.config.*.timestamp-*',
    ],
  },
  js.configs.recommended,
  {
    // Type-aware rules only for src/, which is what tsconfig.json includes. Applying them
    // repo-wide would fail on the config files themselves, which have no TS project.
    files: ['src/**/*.{ts,tsx}'],
    extends: [...tseslint.configs.recommendedTypeChecked],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
      parserOptions: {
        project: ['./tsconfig.json'],
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // Prefixed args stay allowed so interface-conforming signatures read naturally.
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
    },
  },
  {
    // Tests legitimately assert on types and use non-null assertions on fixtures.
    files: ['src/**/*.test.{ts,tsx}'],
    rules: {
      '@typescript-eslint/no-non-null-assertion': 'off',
      '@typescript-eslint/no-unsafe-assignment': 'off',
      '@typescript-eslint/no-unsafe-member-access': 'off',
    },
  },
  {
    // Build and lint config: plain JS/TS, no type-aware rules.
    files: ['*.{js,mjs,ts}'],
    extends: [tseslint.configs.disableTypeChecked],
    languageOptions: { globals: globals.node },
  },
  {
    // Manual tooling under scripts/: Node rather than browser, and outside tsconfig, so the
    // type-aware rules have nothing to work from. Linted rather than ignored — `fetch` and
    // `process` being undefined is a config answer, not a licence to skip the file.
    files: ['scripts/**/*.{js,mjs,ts}'],
    extends: [tseslint.configs.disableTypeChecked],
    languageOptions: { globals: globals.node },
  },
)
