import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 빌드 결과는 escrow/admin/web 으로 → FastAPI 가 그대로 내보냄 (같은 주소라 쿠키·CSRF 그대로)
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: { outDir: "../escrow/admin/web", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8090", "/login": "http://127.0.0.1:8090", "/logout": "http://127.0.0.1:8090" } },
});
