# MatrixOne Issue Trial: #23587

- Issue: `https://github.com/matrixorigin/matrixone/issues/23587`
- Title: `[Feature Request]: MO 需要能够处理 USR1 信号来 rotate 日志文件`
- Repo: `/Users/moukeyu/matrixone`
- Trial Type: 第一批真实 issue 试跑（代表题）
- Recommended Order: 第一批里的第 2 题或第 3 题

---

## 为什么选这个 Issue

这个 issue 很适合拿来验证 AMC 的真实能力，因为它同时具备：
- 需求明确：处理 `SIGUSR1`，触发日志 rotate
- 改动方向相对清晰：信号处理 + 日志 writer
- 验收可观察：不仅能看编译，还能看运行时行为
- 能暴露 AMC 的关键短板：`context` 是否够准、timeline 是否够有用

它的风险在于：
- 不是最简单的小 bug，而是 feature request
- 可能跨两个层次：`cmd/mo-service` 和 `pkg/logutil`
- 最终验收不能只靠 build，还要做运行时验证

---

## 已确认的高相关代码区域

### 1. 信号处理
- `cmd/mo-service/main.go`
- 当前只监听：`SIGTERM`, `SIGINT`
- 还没有看到 `SIGUSR1` 处理逻辑

### 2. 日志写入 / rotate
- `pkg/logutil/internal.go`
- 当前文件日志通过 `lumberjack.Logger` 写入
- 这是实现 rotate / reopen 行为的关键线索之一

---

## 这道题理想的 AMC 表现

创建任务后，AMC 生成的 `context.md` 至少应该帮助 agent 收敛到：
- `cmd/mo-service/main.go`
- `pkg/logutil/internal.go`
- 可能的其他辅助日志初始化代码

理想的执行时间线应当大致像这样：
1. 读取 issue / compiled context
2. 查看 `cmd/mo-service/main.go` 的信号处理
3. 查看 `pkg/logutil/internal.go` 的日志 writer
4. 判断如何在收到 `USR1` 时执行日志 rotate / reopen
5. 修改后至少进行编译级验证
6. 进一步尝试运行时验证

如果 agent 大量进入无关目录，说明：
- `candidate files` 不够准
- 或 `context compiler` 还需要增强

---

## 最小验收标准

### A. 机械验收（最低门槛）
至少满足：
- 能完成任务创建
- 能生成 `context/context.md`
- 能启动 `opencode`
- 修改集中在合理文件范围
- 能通过最小编译检查

### B. 编译级建议
优先从轻量验证开始：
- `go build ./cmd/mo-service`

如果环境允许，再考虑更完整的：
- `make build`

### C. 运行时验收（这个 issue 最关键）
理想验收方式：
1. 启动 `mo-service`
2. 观察当前日志文件
3. 给进程发送 `USR1`
4. 确认日志发生 rotate / reopen
5. 再触发一些日志输出
6. 确认新的日志继续正常写入

如果只通过了 build，但没有验证运行时行为，那么这个 issue 只能算“部分通过”。

---

## 推荐试跑流程

### 第 1 轮：纯自动
输入：
- Repo: `/Users/moukeyu/matrixone`
- Issue: `https://github.com/matrixorigin/matrixone/issues/23587`
- 不给额外提示

重点观察：
- `context.md` 是否把范围收敛到正确文件
- agent 是否快速进入 `main.go` / `logutil`
- timeline 是否能看懂 agent 的推理与动作
- 是否能自动走到合理的修改方案

### 第 2 轮：少量人工提示
如果第 1 轮跑偏，建议只补一条提示，例如：
- `优先查看 cmd/mo-service/main.go 的 signal.Notify 逻辑，以及 pkg/logutil/internal.go 的文件日志 syncer。`

重点观察：
- 补这 1 条提示后，成功率是否明显上升
- 是否说明当前 AMC 主要短板是 context compiler，而不是 backend

---

## Issue 级评分建议

### 任务理解
- agent 是否准确理解“处理 USR1 信号”而不是泛泛谈 rotate

### 文件定位
- 是否能迅速定位到 `cmd/mo-service/main.go` 和 `pkg/logutil/internal.go`

### 执行透明度
- 你是否能从 timeline 清楚知道它为何这么改

### 输出可用性
- 最终 diff 是否像一个合理的人类改动

### 验收完整性
- 是否只停在 build，还是走到了运行时验证

---

## 推荐的 Trial 结论标准

### 强通过
- 正确定位核心文件
- 修改方向正确
- 编译通过
- 运行时行为也验证成功

### 弱通过
- 定位基本正确
- 编译通过
- 但运行时验收没有完整做完

### 失败
- 定位跑偏
- 上下文不够
- timeline 帮助不大
- 或无法形成接近可提交的改动

---

## 下一步最可能暴露的问题

如果这题失败，最可能的原因通常是：
1. `context compiler` 没把关键文件排前面
2. `opencode` 对大型 Go 仓库的路径探索太发散
3. 验证链只有 build，没有连接到运行时验收
4. AMC 目前还缺“更明确的目标约束”层

## 推荐的最小 `.amc.yaml` 样例

可参考：`docs/examples/matrixone-23587.amc.yaml`

建议第一轮先只启用轻量验证：
- `go build ./cmd/mo-service`

更重的完整验证可以放到后面：
- `make build`
- 运行 `./mo-service -launch ./etc/launch/launch.toml`
- 手动发送 `USR1` 验证 rotate 行为
