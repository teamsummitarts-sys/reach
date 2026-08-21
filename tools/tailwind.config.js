/**
 * Tailwind v3, pinned to match what cdn.tailwindcss.com served while these
 * screens were designed.
 *
 * The only theme extension is a text tone. REACH's neutral text scale used
 * gray-500 and gray-600, which measured 3.5-4.1:1 and 2.2-2.6:1 against the
 * surfaces it paints them on — both below the WCAG AA floor of 4.5:1, on every
 * screen. gray-600 in particular carried the explanatory copy, so the text
 * saying *why* REACH decided something was the least readable text in the
 * product.
 *
 * The scale is now gray-300 (high) / gray-400 (secondary) / faint (tertiary).
 * `faint` is the dimmest tone that still clears AA with margin on the lightest
 * surface REACH paints text on (#1a1a1c, a white/5 panel over #111113), so the
 * tier below gray-400 survives rather than collapsing into it.
 *
 * tools/audit-contrast.py measures this against the running app and fails if
 * any screen regresses.
 */
module.exports = {
  darkMode: 'class',
  content: ['./templates/**/*.html'],
  theme: {
    extend: {
      colors: {
        faint: '#868d99', // 5.20:1 on #1a1a1c, 5.64:1 on #111113, 5.93:1 on #0a0a0a
      },
    },
  },
  plugins: [],
}
