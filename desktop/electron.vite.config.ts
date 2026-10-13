import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { resolve } from 'node:path'

export default defineConfig({
  main: { plugins: [externalizeDepsPlugin()] },
  preload: { plugins: [externalizeDepsPlugin()], build: { rollupOptions: { output: { format: 'cjs' } } } },
  renderer: {
    resolve: { alias: { '@': resolve('src/renderer') } },
    optimizeDeps: { esbuildOptions: { target: 'esnext' } },
    plugins: [react(), tailwindcss()]
  }
})
