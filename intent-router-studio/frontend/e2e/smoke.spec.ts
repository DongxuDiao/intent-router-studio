import { expect, test } from "@playwright/test"

/**
 * Web 冒烟 E2E（设计文档 19：Playwright E2E 通过）。
 * 前置：docker compose up（本地栈已在 127.0.0.1:8000 运行）。
 */
const BASE = process.env.E2E_BASE_URL || "http://127.0.0.1:8000"

test("首页重定向到项目页并可新建项目", async ({ page }) => {
  await page.goto("/")
  await expect(page.getByRole("heading", { name: "项目" })).toBeVisible()
  await page.getByRole("button", { name: "新建项目" }).click()
  const name = `e2e-项目-${Date.now()}`
  await page.getByLabel("项目名").fill(name)
  await page.getByRole("button", { name: "确 定" }).click()
  await expect(page.getByText(`项目 ${name} 已创建`)).toBeVisible()
  await expect(page.getByText(name).first()).toBeVisible()
})

test("上传向导：CSV 上传后进入预览与列映射", async ({ page }) => {
  // 前置：已有当前项目（上一个用例或栈内已有；没有则创建）
  await page.goto("/projects")
  const hasUse = await page.getByRole("button", { name: "设为当前" }).first().isVisible().catch(() => false)
  if (hasUse) {
    await page.getByRole("button", { name: "设为当前" }).first().click()
  } else {
    await page.getByRole("button", { name: "新建项目" }).click()
    await page.getByLabel("项目名").fill(`e2e-upload-${Date.now()}`)
    await page.getByRole("button", { name: "确 定" }).click()
    await expect(page.getByText(/已创建/)).toBeVisible()
  }

  await page.goto("/datasets/upload")
  await expect(page.getByText("预览与列映射")).toBeVisible()

  const csv = `text,label,group_id
怎么查上周的报销进度,information,g0
帮我删掉实验 exp-1,write_action,g1
`
  await page.locator("input[type=file]").setInputFiles({
    name: "e2e.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(csv, "utf-8"),
  })
  // 上传成功后自动进入第 2 步：显示预览表
  await expect(page.getByText(/行，编码/).first()).toBeVisible({ timeout: 15_000 })
})

test("主要页面可导航且无崩溃", async ({ page }) => {
  for (const [path, marker] of [
    ["/overview", "总览"],
    ["/datasets", "数据集"],
    ["/runs", "训练"],
    ["/models", "模型"],
    ["/playground", "Playground"],
    ["/system", "系统"],
  ] as const) {
    await page.goto(path)
    await expect(page.getByText(marker).first()).toBeVisible()
  }
  // API 健康检查（浏览器侧可达）
  const health = await page.request.get(`${BASE}/api/v1/health`)
  expect(health.ok()).toBeTruthy()
  expect((await health.json()).status).toBe("ok")
})
