<script setup>
import { computed } from 'vue'
const props = defineProps({ cells: { type: Array, default: () => [] } })
const phases = ['catalog', 'open', 'retrieval', 'processing']
const colors = { catalog: '#952f7c', open: '#c93d80', retrieval: '#e34b75', processing: '#e58077' }
const rows = computed(() => props.cells.map(c => ({
  label: `${c.workload}/${c.case_size} ${c.arm}`,
  segs: phases.map(p => ({ p, v: c.phases?.[p] ?? 0 })),
  total: phases.reduce((a, p) => a + (c.phases?.[p] ?? 0), 0),
})))
const max = computed(() => Math.max(1e-9, ...rows.value.map(r => r.total)))
const W = 520, H = 18, L = 230
</script>

<template>
  <svg :width="W + L + 60" :height="rows.length * (H + 6) + 30" font-size="11" font-family="Inter, sans-serif">
    <g v-for="(r, i) in rows" :key="r.label" :transform="`translate(0, ${i * (H + 6)})`">
      <text :x="L - 6" :y="H - 5" text-anchor="end">{{ r.label }}</text>
      <template v-for="(s, j) in r.segs" :key="s.p">
        <rect :x="L + r.segs.slice(0, j).reduce((a, q) => a + q.v, 0) / max * W" :y="0" :width="s.v / max * W" :height="H" :fill="colors[s.p]" />
      </template>
      <text :x="L + r.total / max * W + 4" :y="H - 5">{{ r.total.toFixed(1) }} s</text>
    </g>
    <g :transform="`translate(${L}, ${rows.length * (H + 6) + 12})`">
      <template v-for="(p, i) in phases" :key="p">
        <rect :x="i * 110" y="0" width="10" height="10" :fill="colors[p]" />
        <text :x="i * 110 + 14" y="9">{{ p }}</text>
      </template>
    </g>
  </svg>
</template>
