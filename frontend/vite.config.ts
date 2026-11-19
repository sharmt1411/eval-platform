import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    host: '0.0.0.0', // 明确绑定 IPv4，确保 localhost、127.0.0.1 和局域网 IP 均可稳定访问
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_BACKEND_URL || 'http://127.0.0.1:9021',
        changeOrigin: true,
      },
    },
  },
  optimizeDeps: {
    include: ['mermaid'],
  },
});
