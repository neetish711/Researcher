import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      // vite dev server → FastAPI on :8000
      '^/(runs|providers|sources|config|dryrun|health|api|docs|openapi.json).*': 'http://127.0.0.1:8000',
    },
  },
})
