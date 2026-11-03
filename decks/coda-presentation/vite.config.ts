import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// Refuse to build a deck whose copied results are missing or name a different
// run than results/tempo_no2/latest.json. Without latest.json (no run yet)
// the deck builds and the result slides show their "not yet run" panel.
function resultsGuard() {
  const here = new URL('.', import.meta.url).pathname
  const pointer = resolve(here, '../../results/tempo_no2/latest.json')
  const copy = resolve(here, 'public/results/summary.json')
  return {
    name: 'results-guard',
    buildStart() {
      if (!existsSync(pointer)) return
      const want = JSON.parse(readFileSync(pointer, 'utf8')).run_id
      const have = existsSync(copy) ? JSON.parse(readFileSync(copy, 'utf8')).run_id : undefined
      if (want !== have)
        throw new Error(`deck results are stale or missing: run analyze.py (latest.json=${want}, deck copy=${have})`)
    },
  }
}

export default {
  // Slidev 53's bundled CSS trips lightningcss minification. esbuild handles it.
  build: { cssMinify: 'esbuild' },
  plugins: [resultsGuard()],
}
