<script setup>
import DnsSettings from '../components/DnsSettings.vue'
import DnsBindings from '../components/DnsBindings.vue'
</script>

<template>
  <div class="dns-view">
    <DnsSettings />
    <DnsBindings />
  </div>
</template>

<style scoped>
.dns-view {
  padding: 20px 22px 30px;
  max-width: 980px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
</style>
