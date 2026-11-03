<script setup>
// One figure from public/. The src is bound at runtime, as in FigureGrid, so the build does not
// try to resolve a file that only exists after analyze.py runs; a missing figure shows its alt text.
defineProps({ src: { type: String, required: true }, alt: { type: String, default: '' } })
</script>

<template>
  <img class="figure" :src="src" :alt="alt" />
</template>
