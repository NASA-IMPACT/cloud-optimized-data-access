<script setup>
// Figures written by analyze.py into public/figures/. Paths are bound at runtime so the
// build does not try to resolve files that do not exist until the first run; a missing
// figure hides itself.
const names = ['phases', 'requests', 'floor_ratio', 'speedup']
const hide = e => { e.target.style.display = 'none' }
</script>

<template>
  <div class="grid grid-cols-2 gap-2">
    <img v-for="n in names" :key="n" :src="`figures/${n}.svg`" :alt="n" @error="hide" />
  </div>
</template>
