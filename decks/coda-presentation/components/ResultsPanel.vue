<script setup>
import { onMounted, ref, computed } from 'vue'
import PhaseBars from './PhaseBars.vue'

const props = defineProps({ kind: { type: String, default: 'phases' } })
const summary = ref(null)
const missing = ref(false)

onMounted(async () => {
  try {
    const r = await fetch('results/summary.json')
    if (!r.ok) throw new Error(String(r.status))
    summary.value = await r.json()
  } catch {
    missing.value = true
  }
})

const oneDay = computed(() => (summary.value?.cells ?? []).filter(c => c.window === '1d' && c.cache_state === 'cold'))
const fixedCells = computed(() => (summary.value?.cells ?? []).filter(c =>
  c.cache_state === 'cold' && ((c.workload === 'point' && c.case_size === 10) || (c.workload === 'aoi' && c.case_size === 100))))
const headline = computed(() => (summary.value?.speedups ?? []).find(s =>
  s.baseline_arm === 'netcdf_user_stack' && s.workload === 'point' && s.case_size === 10 && s.window === '1d'))
const prov = computed(() => summary.value?.provenance ?? {})
const fmt = (x, d = 1) => (x == null ? '—' : Number(x).toFixed(d))
const fmtInt = x => (x == null ? '—' : Math.round(x).toLocaleString())
</script>

<template>
  <div v-if="missing" class="card not-run">
    <b>Benchmark not yet run</b>
    Results appear here after <code>analyze.py</code> runs in us-west-2. See the repository README.
  </div>
  <div v-else-if="summary">
    <template v-if="kind === 'phases'">
      <PhaseBars :cells="oneDay" />
      <p class="text-xs mt-2">
        n = {{ oneDay[0]?.n ?? '—' }}, host: {{ oneDay[0]?.host ?? '—' }},
        snapshot {{ prov.snapshot_tag ?? prov.snapshot_id ?? '—' }}, run {{ summary.run_id }}
      </p>
    </template>
    <template v-else>
      <table class="text-sm">
        <thead><tr><th>arm</th><th>workload</th><th>window</th><th>requests</th><th>bytes</th><th>end-to-end / floor</th></tr></thead>
        <tbody>
          <tr v-for="c in fixedCells" :key="c.arm + c.workload + c.window">
            <td>{{ c.arm }}</td><td>{{ c.workload }}/{{ c.case_size }}</td><td>{{ c.window }}</td>
            <td>{{ fmtInt(c.request_count) }}</td><td>{{ fmtInt(c.bytes_requested) }}</td><td>{{ fmt(c.floor_ratio) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="headline" class="mt-3">
        {{ headline.baseline_arm }} → virtual_icechunk: <b>{{ fmt(headline.speedup_median) }}×</b>
        (95% CI {{ fmt(headline.ci_low) }}–{{ fmt(headline.ci_high) }}, n={{ headline.n }})
      </p>
    </template>
  </div>
</template>

<style scoped>
.not-run { border: 2px dashed var(--slidev-theme-primary); padding: 1rem 1.25rem; }
.not-run b { display: block; margin-bottom: 0.4rem; }
table { border-collapse: collapse; }
th, td { padding: 0.2rem 0.6rem; border-bottom: 1px solid var(--vz-divider); text-align: left; }
</style>
