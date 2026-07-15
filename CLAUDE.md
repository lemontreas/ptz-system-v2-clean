# Claude Code 项目规则

## 启动规则

**每次新会话开始时，必须先读取 `agent.md`，再进行任何代码修改或分析操作。**

`agent.md` 包含了项目的架构概览、核心阅读入口、状态管理硬规则、常用命令速查等关键信息，是 AI 接手项目的会话启动入口。

## 阅读顺序

1. `agent.md` — 项目总指南（必读）
2. `idea/STATUS.md` — 当前进度与任务
3. `rule/AI修改入口指南.md` — 代码修改行动指南
4. `rule/状态管理规则.md` — 状态管理硬规则（涉及状态变更时必读）
5. `idea/ARCHITECTURE.md` — 系统架构文档（需要深入时阅读）
6. `rule/设备测试指南.md` — **仅测试/部署任务时读取**（设备 IP、路径、SSH、部署流程、验证清单）

## Agent skills

### Issue tracker

常规软件 issues 和 PRDs 使用本仓库 GitHub Issues；用户明确指定本地交付位置时，以用户指定位置为准。详见 `docs/agents/issue-tracker.md`。

### Triage labels

使用 `needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix` 五个标准角色。详见 `docs/agents/triage-labels.md`。

### Domain docs

本仓库采用 single-context 布局，领域词汇位于根目录 `CONTEXT.md`。详见 `docs/agents/domain.md`。
