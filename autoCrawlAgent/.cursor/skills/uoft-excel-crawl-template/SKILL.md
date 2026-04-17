---
name: uoft-excel-crawl-template
description: >-
  Aligns LangGraph/Playwright crawler output (AgentCrawler main.py) with the
  「大模型爬取示例模板-多伦多大学.xlsx」 field definitions, official source URLs,
  and extraction rules. Use when working on UofT/多伦多大学 program data scraping,
  Excel template columns (字段名称/提示词), AgentCrawler topics, or comparing crawl
  JSON to the spreadsheet expectations.
---

# 多伦多大学 Excel 模板 × AgentCrawler

## 模板结构（必读）

工作簿含 **两个工作表**，字段行结构相同，**入口 URL 与部分取值逻辑不同**：

| 工作表 | 专业列表入口（表头第 1 行） |
|--------|-----------------------------|
| 大模型爬取示例表格-多大主校-**本科** | `https://future.utoronto.ca/undergraduate-programs?campus=UTSG&program_type`（及筛选说明） |
| 大模型爬取示例表格-多大主校-**硕士** | `https://www.sgs.utoronto.ca/programs/` |

- 第 2 行：列名（字段名称、链接、截图、示例值、取值逻辑补充、备注、提示词-IT / 提示词-业务等）。
- 自第 3 行起：每一行对应一个 **「字段名称」**（共约 65 行有效字段，至「是否需要 ATAS」等）。

与 `instruct.md` 中的示例一致：本科可从 `https://www.artsci.utoronto.ca/` 或 `https://future.utoronto.ca/` 起爬；硕士以 SGS 项目列表/项目页为主。

## 与代码的对应关系

- **入口**：`python main.py <起始URL> -t <主题1> <主题2> ...`（`main.py`）。
- **`--topics`**：每项是一条 **需要用正文回答的自然语言问题**，不是关键词检索；应用 Excel **「提示词」列** 的意图，写成完整问句或明确指令句。
- **输出**：默认打印 JSON；`-o <目录>` 写入带时间戳的 `crawl_*.json`，含 `results`（主题→答案）、`pending_topics`、`finish_reason` 等。
- **浏览器**：Playwright Chromium；`--headed` 有头；`--max-iter` 控制页内交互轮次（复杂站点可提高，如 `instruct.md` 中 100）。
- **LLM**：默认通义（`DASHSCOPE_API_KEY`）；可选 `--llm deepseek` 或环境变量 `LLM_PROVIDER`。
- **后处理**：默认会对多轮合并结果做 LLM 精炼；`--no-refine-results` 可跳过。

图逻辑（`crawler/graph.py`）要点：规划 + 点击/导航、按 schema 抽取、合并多轮答案；若某答案主要为「去别处点击/详见」类导航套话，可能 **不并入** `results`（需答案实质内容）。

## 字段清单（两表共用）

按 Excel 第 1 列顺序（从「专业链接」到「是否需要 ATAS」）：专业链接、专业名称、学位名称、专业代码、专业所在校区、专业小方向、学院名称、课程简介、学习模式、学制（时长）、学费、是否有带薪实习、OSSD/美高/A-Level/IB 成绩要求、入学要求、先修课程要求、学术要求、完整入学要求、中国学生入学要求描述、工作经验、转专业、开学日期、申请轮次、申请开始/截止日期、雅思、托福（旧/新）、PTE、多邻国、免语言条件、申请时是否需语言成绩、GRE、GMAT、课程结构、合作相关、申请专业个数限制、简历/PS/推荐信/作品集/面试/竞赛等要求、申请方式、申请链接、邮寄材料、申请费及支付方式、申请邮箱规则、语言送分 Code、主课与语言课关联、语言课截止、WES、ATAS。

**硕士表** 部分行的「链接」列或备注与 SGS Calendar（`https://sgs.calendar.utoronto.ca/degree/...`）相关；**本科** 常与 `future.utoronto.ca/program/...`、`artsci.calendar.utoronto.ca`、各学院站并列出现。

## 模板中的高优先级取值规则（摘录）

下列规则来自模板「取值逻辑补充 / 提示词」列，用于写 `--topics` 与验收爬取结果：

### 专业链接（本科）

- 从 future 专业列表进入 **St. George**，再进 **Learn more about** 等到 **专业详情**；模板验收曾强调与 `future.utoronto.ca/program/...` 概览页区分，需按业务要求区分 **admission-categories** 类链接与 program 介绍页（以模板当期说明为准）。

### 专业代码

- 非澳洲/英爱线可 **留空**；勿编造不明编码。

### 学院名称

- 取 **Faculty** 级全称；硕士示例中避免只输出 **Department** 级或非常规倒装格式。

### 课程简介

- **本科**：优先 **课程日历** `https://*.calendar.utoronto.ca/section/*`，取 **Introduction** 与下一加粗小标题之间的段落。
- **硕士**：优先 **SGS 项目页** 对应当前学位的段落；若首段混合 MEng/MASc/PhD，需 **只保留与当前项目匹配** 部分，禁止擅自改写概括。

### 学习模式 / 学制

- 官网未写学习模式时，模板示例用 **Full-time**（按列说明执行）。
- **硕士** 学制：只取当前学位 **full-time** 信息，优先 SGS Calendar；standard 与 extended 可同时时按模板示例格式合并；未写明则 **留空**。

### 学费

- 使用 **Tuition Fee Lookup Tool**（`planningandbudget.utoronto.ca`）等官方工具时：保留 **货币与单位**，避免仅裸数字；本科示例常带 **Year 1** 等前缀；硕士示例可为 **CA$ … total** 形式（以模板列为准）。

### 带薪实习（本科）

- 通过列表筛选 **Work-Integrated Learning** 等判断是否「可选」；注意 **校区差异**（例如部分项目仅在另一校区有 Co-op，勿误标）。

### 成绩与入学要求

- **OSSD 等**：按 Ontario High School / Recommended Range 等 **分块** 摘录；硕士「入学要求」需覆盖学历、学科背景、最低 GPA、特殊要求等（见模板长说明）。

### PG 不适用行

- 硕士表中 **OSSD/美高/A-Level/IB** 等标为 PG 不适用时，填 **不适用/空**，勿强行爬 US 高中体系内容。

## 推荐工作流

1. 在 Excel 中选定 **本科或硕士** 表，确认该表第一行的 **专业列表 URL**。
2. 为当前批次字段，从 **提示词-业务 / 提示词-IT** 列整理成 **一条条可执行的问题**（对应 `--topics`）。
3. 运行：`python main.py "<起始URL>" -t "问题1" "问题2" ... -o results`（必要时调大 `--max-iter`）。
4. 将 JSON 中 `results` 各键与 Excel **字段名称** 行对齐；对照 **模板示例值/期望值** 与 **新旧爬取结果对比** 列做回归说明。

## 与现有「多伦多大学项目检索」skill 的分工

- 若任务是 **官方网页检索 + 人工结构化总结**（无浏览器自动化），用用户技能 **uoft-program-search**。
- 若任务是 **本仓库 AgentCrawler 跑网页 + 对齐 Excel 模板字段**，用本 skill。
