import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// The bot serves this build at / (src/tgvio/adapters/web/server.py).
export default defineConfig({
  base: "/",
  plugins: [react(), tailwindcss()],
  server: { proxy: { "/api": "http://127.0.0.1:8792" } },
  test: { environment: "jsdom" },
});
