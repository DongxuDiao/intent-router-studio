import { expect, test } from "@playwright/test"

/**
 * V2 §4.5：项目列表"进入"必须先切换当前项目再导航。
 * 前置：docker compose up（API 在 127.0.0.1:8000，vite dev 代理或容器直连均可）。
 */
const BASE = process.env.E2E_BASE_URL || "http://127.0.0.1:8000"

test("点击「进入」先切换当前项目再跳转总览", async ({ page }) => {
  // 经 API 建两个项目，避免 UI 建表单的时序抖动
  const first = await page.request.post(`${BASE}/api/v1/projects`, {
    data: { name: `e2e-进入-甲-${Date.now()}`, description: "" },
  })
  const second = await page.request.post(`${BASE}/api/v1/projects`, {
    data: { name: `e2e-进入-乙-${Date.now()}`, description: "" },
  })
  expect(first.ok()).toBeTruthy()
  expect(second.ok()).toBeTruthy()
  const secondJson = (await second.json()) as { id: string; name: string }
  const secondId = secondJson.id

  await page.goto("/projects")
  // 找到"乙"项目那一行（按本次创建的唯一名称精确匹配），点"进入"
  const row = page.locator(".ant-list-item", { hasText: secondJson.name })
  await expect(row).toBeVisible()
  await row.getByRole("button", { name: /进\s*入/ }).click()

  // 已导航到总览，且当前项目已切换为"乙"（总览副标题显示 projectId）
  await expect(page).toHaveURL(/\/overview$/)
  await expect(page.locator(".ant-page-header-heading-sub, .ant-typography").filter({ hasText: secondId }).first()).toBeVisible()
  // localStorage 与服务端视角一致
  const stored = await page.evaluate(() => localStorage.getItem("irs.projectId"))
  expect(stored).toBe(secondId)
})
