import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Relative base: the app is served under /<owner-secret>/, which the build never knows.
// `npm run dev` proxies the API to a local gateway: GATEWAY_DEV=http://localhost:8080/<secret>
const target = process.env.GATEWAY_DEV || "http://localhost:8080";

export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
  server: { proxy: { "/api": { target, changeOrigin: true } } },
});
