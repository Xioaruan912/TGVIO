import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// Served under /next/ next to the current front end until the cut-over (phase 4).
export default defineConfig({
  base: "/next/",
  plugins: [react(), tailwindcss()],
  server: { proxy: { "/api": "http://127.0.0.1:8790" } },
  test: { environment: "jsdom" },
});
