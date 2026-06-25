import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Renderer build. base './' so the packaged app loads assets over file://.
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: { port: 5173, strictPort: true },
  build: { outDir: "dist", emptyOutDir: true },
  // Keep a single React instance (pdfjs dep-optimization was pulling in a 2nd copy →
  // "Invalid hook call"). Dedupe + pre-bundle React explicitly.
  resolve: { dedupe: ["react", "react-dom"] },
  optimizeDeps: {
    include: ["react", "react-dom", "react-dom/client", "react/jsx-runtime", "pdfjs-dist"],
  },
});
