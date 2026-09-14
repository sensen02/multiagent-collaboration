---
name: game-playtest
description: 可组合的游戏试玩指导：使用现有 shell 与 Playwright 运行游戏、探索关键交互，并记录可复现的事实证据；不规定流水线、评分或硬性通过门槛。
when-to-use: 当需要试玩网页游戏、检查交互体验或记录视觉/运行时事实时；按任务需要选择其中步骤组合。
metadata:
  kind: guidance
  optional: true
  composable: true
---

# Game playtest（可选指导）

这是帮助模型试玩和记录事实的提示，不是验收流程。根据任务选择步骤，避免把建议当成业务门禁。

## 可组合步骤

1. **建立基线**：用现有 shell 启动目标（不要另起服务覆盖用户环境），确认 URL/入口和运行命令；记录浏览器、视口、时间。
2. **观察资源**：Playwright 监听 `pageerror`、`console`、`requestfailed`，并检查响应状态（尤其脚本、样式、图片、音频）；记录 URL、状态和错误文本。不要静默吞掉失败资源。
3. **探索交互**：按任务目标点击、键盘操作、暂停/重启、移动端视口等；每一步记录操作、前置状态和观察到的结果。
4. **收集证据**：使用截图、可访问名称、DOM 文本或页面暴露的 debug 状态；证据应标明时间/视口，避免只写“看起来正常”。
5. **整理事实**：区分“观察到的事实”“推测”和“未覆盖”；报告复现步骤与已知限制。不设固定分数、固定顺序或强制通过条件。

## 最小 Playwright 观察片段

```python
resource_failures = []
page.on("requestfailed", lambda r: resource_failures.append({"url": r.url, "error": r.failure}))
page.on("response", lambda r: resource_failures.append({"url": r.url, "status": r.status}) if r.status >= 400 else None)
page.on("pageerror", lambda e: errors.append(str(e)))
```

根据目标决定哪些状态需要报告；试玩 skill 本身不自动阻止后续业务操作。
