import js from "@eslint/js";
import babelParser from "@babel/eslint-parser";
import reactHooks from "eslint-plugin-react-hooks";

/**
 * Flat config for ESLint 9 over `web/src`.
 *
 * TypeScript here is parsed by **Babel**, not typescript-eslint. This package
 * pins `typescript@7` — the native (Go) compiler, which exposes no JS API —
 * and typescript-eslint's peer range stops below it (`>=4.8.4 <6.1.0`), so the
 * standard pairing cannot run at all. Babel strips types without touching the
 * compiler, which is enough for every rule this config enables: the
 * react-hooks pair is purely syntactic. Type-level guarantees stay with
 * `tsc` (`npm run typecheck`), which understands types and already enforces
 * unused-locals with `noUnusedLocals`/`noUnusedParameters`.
 *
 * The point of having ESLint here at all is the hook-dependency rule: it
 * catches mechanically the bug class where a `useCallback` pins a stale
 * closure (review 08-frontend, MINOR 7 / MAJOR 1).
 */
export default [
  { ignores: ["dist/**", "node_modules/**"] },
  js.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      parser: babelParser,
      parserOptions: {
        requireConfigFile: false,
        // The fast path (babelrc/configFile both false) parses directly with
        // @babel/parser. Its plugins must be explicit: @babel/parser no longer
        // auto-enables TypeScript or JSX from the `.tsx` filename, and the
        // `estree` plugin the parser prepends would otherwise suppress them.
        // `typescript` + `jsx` is the whole parser story — no preset, which
        // the fast path would ignore anyway.
        babelOptions: {
          babelrc: false,
          configFile: false,
          parserOpts: { plugins: ["typescript", "jsx"] },
        },
        sourceType: "module",
      },
    },
    plugins: { "react-hooks": reactHooks },
    rules: {
      // ESLint has no type information here, so the core rules that guess at
      // types would false-positive: `no-unused-vars` cannot see type-only
      // imports used in type positions, and `no-undef` cannot see the DOM and
      // browser globals the type-checker knows. Both are `tsc`'s job.
      "no-undef": "off",
      "no-unused-vars": "off",
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",
    },
  },
];
