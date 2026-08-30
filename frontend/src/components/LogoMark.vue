<script setup>
defineProps({
  size: { type: [Number, String], default: 16 },
})
</script>

<template>
  <div class="logo-mark">
    <span class="logo-c">C</span><span class="logo-rest">Auth</span>
  </div>
</template>

<style scoped>
.logo-mark {
  display: inline-flex;
  align-items: baseline;
  font-weight: 700;
  font-size: 16px;
  letter-spacing: 0.5px;
  line-height: 1;
}

.logo-c {
  color: var(--accent);
}

.logo-rest {
  color: var(--text-secondary);
}
</style>
