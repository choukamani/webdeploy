import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 3500,
    proxy: {
      "/api": {
        target: "http://localhost:8500",
        changeOrigin: true,
        timeout: 600000,
        proxyTimeout: 600000,
      },
      "/ws": {
        target: "ws://localhost:8500",
        ws: true,
      },
    },
  },
});
