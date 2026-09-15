# AutoCrawlAgent

**主题驱动的智能网页爬虫** — 输入起始 URL 与一组待回答问题，Agent 自动浏览网站、抽取信息并输出结构化 JSON。

基于 [LangGraph](https://github.com/langchain-ai/langgraph) 构建，采用 ReAct 式 **加载 → 快照 → 抽取 → 规划 → 点击** 循环；支持通义千问、DeepSeek、Kimi 等大模型基座。

> 项目源码位于 [`autoCrawlAgent/`](autoCrawlAgent/) 目录。

---

## 特性

- **主题驱动抽取**：从 `topic` 文件读取问题列表，逐页 LLM 抽取并累积答案
- **智能页面导航**：自动识别可交互元素，规划下一步点击；支持站内 DFS 与退回起始页重试
- **多模型支持**：通义（默认）、DeepSeek、Kimi / Moonshot，通过环境变量切换
- **爬后外搜补抽**：对仍未找到的 topic 调用搜索引擎（Google / Bing / DuckDuckGo）补充抓取
- **结果精炼**：多轮合并后可选 LLM 去重压缩
- **可视化输出**：URL 结构树（JSON + HTML）、各页字段映射、LangGraph 流程图导出

---

## 快速开始

### 1. 安装依赖

```bash
cd autoCrawlAgent
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置 API Key

在 `autoCrawlAgent/` 目录创建 `.env` 文件：

```env
# 默认使用通义千问
DASHSCOPE_API_KEY=your_dashscope_key
TONGYI_MODEL=qwen-plus

# 可选：切换 LLM 基座
# LLM_PROVIDER=deepseek
# DEEPSEEK_API_KEY=your_deepseek_key

# LLM_PROVIDER=kimi
# MOONSHOT_API_KEY=your_moonshot_key
```

### 3. 准备 topic 文件

默认读取 `autoCrawlAgent/topic` 文件，每行一条待回答问题，`#` 开头为注释：

```text
# 多伦多大学 Computer Science 专业
该专业的学制是几年？
中国学生申请要求是什么？
```

### 4. 运行爬虫

```bash
cd autoCrawlAgent
python main.py "https://future.utoronto.ca/" -o ./results --max-iter 30
```

结果写入 `./results/crawl_YYYYMMDD_HHMMSS.json`，并在终端输出完整 JSON。

---

## 常用参数

| 参数 | 说明 |
|------|------|
| `url` | 起始页面 URL（必填） |
| `-o`, `--output` | 结果输出目录 |
| `--topic-file` | topic 列表文件路径（默认 `topic`） |
| `--max-iter` | 最大页面交互轮次（默认 6） |
| `--max-home-retreats` | 子页无路可走时退回起始页重试次数（默认 6） |
| `--headed` | 有头模式，显示浏览器窗口 |
| `--llm` | 指定 LLM：`tongyi` / `deepseek` / `kimi` |
| `--enable-search` | 爬后对外搜未找到的 topic（需 `--max-iter >= 30`） |
| `--search-engine` | 搜索引擎：`google` / `bing` / `duckduckgo` |
| `--no-refine-results` | 跳过爬取结束后的 LLM 精炼 |
| `--export-graph FILE` | 导出 LangGraph 流程图（`.mmd` / `.png` / `.txt`） |
| `--skill PATH` | 注入本地 Markdown 领域说明到 LLM 系统提示 |

更多说明见 [`autoCrawlAgent/instruct.md`](autoCrawlAgent/instruct.md)。

---

## 运行配置

可在 `autoCrawlAgent/crawl_runtime.json` 放置默认参数（可用 `--no-runtime-config` 禁用）：

```json
{
  "start_url": "https://example.edu/program",
  "topic_file": "topic",
  "output_dir": "./results",
  "max_iter": 30,
  "natural_language_search": true,
  "refine_results": true
}
```

---

## 输出文件

使用 `-o` 指定目录时，每次运行生成：

| 文件 | 内容 |
|------|------|
| `crawl_*.json` | 主结果：各 topic 答案、耗时、token 用量等 |
| `crawl_*_url_fields.json` | 各 URL 页面对应抽取到的字段 |
| `crawl_*_url_tree.json` / `.html` | 站点导航结构树（可视化） |

---

## 架构概览

```
START → load → snapshot → extract → nav_dfs
                                      ├─→ snapshot（新子页）
                                      ├─→ plan → click → snapshot
                                      └─→ END
```

- **snapshot**：Playwright 采集页面可见文本与可交互元素
- **extract**：LLM 从当前页抽取 / 合并 topic 答案
- **plan**：LLM 选择下一步点击目标（ReAct 规划）
- **nav_dfs**：多链接站内深度优先导航

流程图源文件：[`autoCrawlAgent/docs/crawler_graph.mmd`](autoCrawlAgent/docs/crawler_graph.mmd)，可用 [Mermaid Live](https://mermaid.live) 预览。

---

## 项目结构

```
.
├── README.md
└── autoCrawlAgent/
    ├── main.py              # CLI 入口
    ├── topic                # 默认待回答问题列表
    ├── crawl_runtime.json   # 可选运行配置
    ├── requirements.txt
    ├── crawler/
    │   ├── graph.py         # LangGraph 状态图与核心节点
    │   ├── state.py         # 图状态定义
    │   ├── browser_session.py
    │   ├── llm_client.py    # LLM 调用与结构化输出
    │   ├── search_agent.py  # 搜索引擎客户端
    │   └── post_crawl_search.py
    ├── docs/                # 流程图、问题记录
    └── evaluator.py         # 爬取结果评估（独立脚本）
```

---

## 环境变量参考

| 变量 | 说明 | 默认 |
|------|------|------|
| `LLM_PROVIDER` | LLM 基座 | `tongyi` |
| `TONGYI_MODEL` | 通义模型名 | `qwen-plus` |
| `EXTRACT_TOPIC_BATCH_SIZE` | 每批抽取 topic 数 | `12` |
| `LLM_MAX_OUTPUT_TOKENS` | 模型输出 token 上限 | `8192` |
| `SEARCH_ENGINE` | 搜索引擎 | 自动检测 |
| `EXPORT_URL_FIELDS` | 是否输出 URL 字段映射 | 开启 |
| `EXPORT_URL_TREE` | 是否输出 URL 结构树 | 开启 |

---

## 路线图

- [ ] 从自然语言出发进行搜索
- [ ] 搜索框等检索控件的智能交互
- [ ] 基于 RAG 的网页导航库

---

## 许可证

本项目仅供学习与研究使用。爬取网站时请遵守目标站点的 robots.txt 与服务条款。
