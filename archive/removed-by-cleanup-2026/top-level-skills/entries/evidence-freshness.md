# 知识条目：验证证据会过期（ink-duel 复核）

**来源**：`run_0917f0202d86`（ink-duel，2026-09-13）的独立复核 + 修复。
**落点**：`unison/runtime.py`（证据绑定与弱证据档）、`unison/skillcheck.py`（工作区级对账）、
`unison/skills/skill-validator/`（检查入口）、ink-duel 自己的 `serve.py` / `tests/smoke.py` / `README.md`。

## 一句话

**"退出码 0"+"证据文件写着 passed"** 可能来自**两次不同的执行**。
证据必须能自证"是哪一次、验的是哪个版本"，否则它不是证据，只是一份好看的文本。

## 事故现场（原版 `ink-duel/tests/smoke.py`）

原版**只在跑到最后**才写 `tests/results.json`：

| 场景 | 退出码 | 证据文件 | 后果 |
|---|---|---|---|
| 第一次失败（无旧文件） | 1 | **不存在** | 失败没留下任何痕迹 |
| 失败但旧文件还在 | 1 | **上一轮的 `passed: true`** | 证据与失败并存，无人能分辨 |

而且断言是固定 sleep 的：`keyboard.press('Space')` 后只等 70ms 就断言
`action=='dodge'`——实测 **11 次挂 1 次**（约 9%），失败信息看起来像"测试写错了"，
其实是被测系统在冷却/收招窗口里**合法拒绝**了一次按键。

## 修法（三层，都要有）

**① 证据文件本身：无条件写 + 运行级字段 + 产物指纹**

```json
{"passed": true, "exit_code": 0, "created": "2026-09-13T13:44:49", "duration_seconds": 20.3,
 "url": "http://127.0.0.1:8001", "failure": null,
 "checks": [{"name": "dodge", "ok": true}],
 "artifacts": {"game.js": "c161ba59…", "assets/hero.svg": "ac99ec0a…"}}
```

`exit_code` + `created` 让"这次"和"上次"可分辨；`artifacts` 让"验的是哪个版本"可回答
（没有它，报告只能说"跑过测试"，不能说"跑过**这个**版本"）。

**② 测试断言：把时间窗给足，而不是赌采样时刻**

`press_until()`：按键 → 轮询条件（动作**或**冷却，因为动作只维持 0.48s、冷却立刻记账）
→ 没成立就再按一次，直到超时。改完后 12 次连续运行 0 失败。

**③ 运行时：证据与交付物的绑定不再靠自述**

`runtime.verification()` 现在对每条命令证据判定三件事，并给出新档位：

| 判定 | 含义 | 结果 |
|---|---|---|
| 退出码 0、未超时 | 命令成功了 | 前提 |
| `exit_code_masked` | 退出码可能被 shell 结构吞掉（heredoc / 管道 / `sed -i` / 多行无 `set -e`） | 降级 |
| `fresh` / `stale` | 执行时的清单指纹 vs 交付时的清单指纹 | 不一致 → `files-changed-after-command` |

- 三者都过 → `verified_by_command`；
- 有命令成功、但没有一条能证明"验的是这个版本" → **`verified_by_command_weak`** + `unconfirmed`；
- 报告里新增 `trustworthy_commands` / `masked_exit_commands` / `stale_commands` /
  `changed_after_evidence`，控制台直接显示"命令证据（弱：可能过期或被掩盖）"
  与"有 N 个文件在最后一条命令之后又改过"。

真机形状（素材任务）：报告 `verification_status: verified`，而
`verification_evidence.verified = verified_by_inspection`、`command_executions: 0`——
它的 SVG 校验**确实跑了**，但写成 `python3 - <<'PY' ... ET.parse(...) ... PY`，
运行时只按命令字符串记账，看不到块内的退出码。**只要把命令包进 heredoc，命令级证据就永远拿不到。**

## 一条可复用的判据

1. 任何"测试通过"的结论，都要能回答：**哪一次执行**、**哪个版本**、**退出码是什么**。
2. 证据文件是**每次运行重写**的，不是"最后写一次的荣誉证书"。
3. 断言失败要先问："这是被测系统的合法拒绝，还是真的坏了？"——冷却、收招、动画时长都会造成合法拒绝；
   用轮询窗口消掉它，比把 flake 当成"测试写错了"便宜得多。
4. 别用 heredoc 把测试命令包起来：要么拆成单独的脚本文件（`bash tests/run.sh`，脚本里 `set -e`），
   要么显式登记退出码。运行时看不见的退出码，等于没有退出码。
