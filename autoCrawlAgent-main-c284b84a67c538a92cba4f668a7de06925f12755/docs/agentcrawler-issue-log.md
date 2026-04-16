# AgentCrawler 问题与修改日志

本文件用于在后续改代码时**持续记录**：线上/本地跑出来的问题、根因、以及对应改动（便于复盘与给他人说明）。

**维护约定（给维护者）**

- 每次因 **bug / 行为不符合预期 / 重要行为变更** 而改代码时，在下方 **按日期追加一节**（新的在上）。
- 建议包含：**现象** → **原因** → **修改**（涉及文件与要点）→ **验证方式**（可选）。
- 不必记录纯重构、无关紧要的格式化。

---

## 2026-04-13 — 抽取 JSON `Unterminated string`：topic 过多单次输出被截断

- **现象**  
  - `last_error` 含 `抽取失败: Unterminated string starting at...`，或 `json.loads` 相关错误；topic 很多时易出现。

- **原因**  
  - 单次 `node_extract` 要求模型输出包含全部 topic 的超大 JSON，**超出 API 输出上限或模型截断**，字符串未闭合。

- **修改**  
  - `crawler/graph.py`：按 **`EXTRACT_TOPIC_BATCH_SIZE`（默认 12）** 分批调用抽取；单批仍失败则 **对半递归** 直至单 topic。成功返回时 **`last_error: None`** 清除旧错误。  
  - `crawler/llm_client.py`：为 DeepSeek `ChatOpenAI`、通义 `ChatTongyi` 设置 **`LLM_MAX_OUTPUT_TOKENS`（默认 8192）** 可环境变量调大。

---

## 2026-04-13 — 快照前自动展开折叠区（details / 手风琴）

- **现象**  
  - 录取页、文档站大量使用 `<details>`、`aria-expanded` 手风琴；未展开时 `body.inner_text()` 与 `:visible` 链接采不到面板内正文。

- **原因**  
  - `visible_text()` 依赖渲染可见文本；`collect_interactives()` 依赖 `:visible`，折叠节点默认不在结果中。

- **修改**  
  - `crawler/browser_session.py` 新增 **`BrowserSession.expand_collapsed_content`**：  
    - 用 **`details.open = true`** 展开所有未打开的 `<details>`；  
    - 在 **`main` / `[role="main"]` / `#main-content` / `article` / `#primary` / `.entry-content`** 等容器内，轮询点击 **`button` / `[role="button"]` 且 `aria-expanded="false"`**（每轮最多若干次，多轮以应对懒渲染）。  
  - `crawler/graph.py` 的 **`node_snapshot`** 在 **`visible_text` / `collect_interactives` 之前** 调用上述方法。  
  - stderr 打印展开统计（details 次数累计、按钮点击次数）。

- **验证**  
  - 对含折叠「入学要求」的 artsci 录取页跑一轮，确认 `page_text` 中出现展开后才可见的段落；若某站把手风琴放在无 `main` 的容器内，可再补选择器。

---

## 2026-04-13 — 顶栏「Undergraduate Calendar」等外链：新标签打开导致误判「无页面跳转」

- **现象**  
  - 在 `https://www.artsci.utoronto.ca/future/ready-apply/admission-categories/computer-science` 等处，Agent 点击导航里的 **Undergraduate Calendar**（指向 `https://artsci.calendar.utoronto.ca/`）后，图状态认为 **当前页 URL 未变**。  
  - 终端/JSON 中出现 `plan_reason` 带 **`[已忽略：该序号已尝试且无页面跳转]`**，同一链接被记入 `no_nav_signatures` 后无法再点。  
  - 人工在浏览器中点击能进入日历站（常见为新标签页打开）。

- **原因**  
  - 链接多为 **`target="_blank"`**：导航发生在 **新 Browser tab**，而 Playwright 只持有 **原 `Page`**，`page.url` 不变。  
  - `crawler/graph.py` 的 `node_click` 用「点击前后 **`self.page` 的 URL** 是否变化」判断是否跳转，因而误判为「点了但无导航」，并写入 `no_nav_signatures`。  
  - `scoped`（顶栏/导航区）路径原先仅 `loc.click()`，成功打开新标签时不会切换 `self.page`。

- **修改**（`crawler/browser_session.py`）  
  - 新增 **`_norm_url_for_cmp` / `_host_for_cmp`**（与 `graph._norm_url` 语义对齐，用于比较是否已离开当前文档）。  
  - 新增 **`BrowserSession._link_click_follow`**：在 **`scoped` / 正文 `link`（同站）/ `menulink`** 且带 `href` 时：  
    1. 点击后若 **`context.pages` 数量增加**，视为新标签：将 **`self.page` 切到最新页**、`wait_for_load_state(domcontentloaded)`，并 **关闭旧页**（避免多标签堆积）。  
    2. 若页数未增但 **规范化 URL 已变**，视为同页导航成功。  
    3. 若仍 **URL 未变** 且 **href 与当前页不同主机**，则 **`page.goto(abs_href)`** 作为回退（应对弹窗被拦、或未触发同页导航等情况）。  
  - 无 `href` 的 `scoped` / `menulink` 仍走原 `click()`。

- **验证建议**  
  - 对上述计算机科学录取页运行爬虫，观察 stderr 是否出现 **`点击导航链接（新标签）`** 或 **`外链 goto`**，且 `page.url` / 结果 JSON 能进入 `artsci.calendar.utoronto.ca` 域。

---

（以下由后续修改继续追加）
