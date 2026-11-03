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

// Headline cells only: default arm settings, and not the once-per-session checker arm.
const plain = computed(() => (summary.value?.cells ?? []).filter(c =>
  c.cache_state === 'cold' && c.arm !== 'virtual_manifeststore' && Object.keys(c.arm_settings ?? {}).length === 0))
const oneDay = computed(() => plain.value.filter(c => c.window === '1d'))
const fixedCells = computed(() => plain.value.filter(c =>
  (c.workload === 'point' && c.case_size === 10) || (c.workload === 'aoi' && c.case_size === 100)))
const speedupLines = computed(() => (summary.value?.speedups ?? []).filter(s =>
  ['netcdf_user_stack', 'netcdf_tuned'].includes(s.baseline_arm) && s.workload === 'point' && s.case_size === 10 && s.window === '1d'))
const requests = c => (c.request_count == null ? '—' : (c.request_count_kind === 'static-minimum' ? '≥ ' : '') + fmtInt(c.request_count))
const kindLabel = { 'static-minimum': 'static minimum', 'http-calls': 'HTTP calls', 'client-ranges': 'client ranges' }
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
        <thead><tr><th>arm</th><th>workload</th><th>window</th><th>requests</th><th>counted as</th><th>bytes</th><th>end-to-end / floor</th></tr></thead>
        <tbody>
          <tr v-for="c in fixedCells" :key="c.arm + c.workload + c.window">
            <td>{{ c.arm }}</td><td>{{ c.workload }}/{{ c.case_size }}</td><td>{{ c.window }}</td>
            <td>{{ requests(c) }}</td><td>{{ kindLabel[c.request_count_kind] ?? c.request_count_kind }}</td><td>{{ fmtInt(c.bytes_requested) }}</td><td>{{ fmt(c.floor_ratio) }}</td>
          </tr>
        </tbody>
      </table>
      <p class="text-xs mt-1">"≥" is a static minimum from the store's chunk grid. "Client ranges" are Python calls into obstore, one per byte range; Rust may coalesce, split or retry them, so the HTTP request count can differ. "HTTP calls" are completed S3 operations.</p>
      <p v-for="s in speedupLines" :key="s.baseline_arm" class="mt-2">
        {{ s.baseline_arm }} → virtual_icechunk: <b>{{ fmt(s.speedup_median) }}×</b>
        (95% CI {{ fmt(s.ci_low) }}–{{ fmt(s.ci_high) }}, n={{ s.n }}<span v-if="!s.detectable">; inside session spread, no detectable difference</span>)
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
