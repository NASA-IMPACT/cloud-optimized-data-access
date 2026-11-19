<script setup>
// One figure from public/. The src is bound at runtime so the build does not try to resolve it:
// a figure that has not been copied into public/figures/ yet shows its alt text instead of
// failing the build.
defineProps({ src: { type: String, required: true }, alt: { type: String, default: '' } })
</script>

<template>
  <img class="figure" :src="src" :alt="alt" />
</template>
