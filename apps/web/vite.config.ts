import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiPort = process.env.TITLES_API_PORT ?? "8400";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${apiPort}`,
        changeOrigin: true,
      },
    },
  },
});
