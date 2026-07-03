import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Set VITE_BASE at build time to your repo name for GitHub Pages project
// sites, e.g. `VITE_BASE=/litcovid-explorer/ npm run build`. Leave unset
// (defaults to '/') for a user/org page or a custom domain.
export default defineConfig({
  base: process.env.VITE_BASE || '/',
  plugins: [react()],
})
