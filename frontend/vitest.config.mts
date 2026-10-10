// FR-54's test lane: the frontend's minimal vitest step, wired into CI's
// existing frontend job and scoped to the renderer-config and reducer cases
// FR-54/FR-55 mandate — a security control whose test has no harness is an
// assumption with extra words.
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname) },
  },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
