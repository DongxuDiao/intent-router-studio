import { defineConfig, devices } from "@playwright/test"

/**
 * E2E 冒烟：直接对着本地 Docker 栈（docker compose up）跑，
 * 不由 Playwright 拉起服务；栈未启动时全部用例失败。
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://127.0.0.1:8000",
    trace: "off",
    locale: "zh-CN",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
})
