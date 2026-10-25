import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// The player serves this build at / (src/tgvio_player/adapters/http/server.py).
export default defineConfig({
  base: "/",
  plugins: [react(), tailwindcss()],
  server: { proxy: { "/api": "http://127.0.0.1:8790" } },
  test: { environment: "jsdom" },
});
