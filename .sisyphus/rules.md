# Sisyphus Rules

## 工作流程

**分析优先，确认后再动手。**

1. 用户提出需求 → 先分析问题 → 列出修改方案 → 等用户确认 → 再改代码
2. 不要跳过分析和确认步骤直接动手
3. 讨论时说"别改代码"或"先讨论"，严格只分析不动手
4. 用户说"列一下修改点"或"罗列" → 只列不改

## 项目习惯

- 数据目录在 `~/docker/ai-calendar-assistant/data/`，不在项目目录
- 本地开发用 `docker-compose.dev.yml`（在 docker 目录）
- 正式部署用 `docker-compose.yml` + ghcr 镜像
