cd d:\MutiAgent\AgentCrawler
pip install -r requirements.txt
playwright install chromium
# 设置 DASHSCOPE_API_KEY（或复制 .env.example 为 .env）
# topic 列表默认从项目根目录 `topic` 文件读取（每行一条）；可用 --topic-file 指定其他路径
# 问题/原因/修改记录：docs/agentcrawler-issue-log.md
# 可选环境变量：EXTRACT_TOPIC_BATCH_SIZE=12（每批抽取 topic 数） LLM_MAX_OUTPUT_TOKENS=8192（模型输出上限）

# 导出 LangGraph 流程图（无需 url）：Mermaid 可用 https://mermaid.live 粘贴预览
python main.py --export-graph docs/crawler_graph.mmd
# ASCII / PNG 需额外依赖：pip install grandalf 或 pip install pygraphviz（并安装 Graphviz）


python main.py "https://future.utoronto.ca/" -o D:\MutiAgent\AgentCrawler\results --max-iter=100

python main.py "https://www.artsci.utoronto.ca/future/ready-apply/admission-categories/computer-science" -o D:\MutiAgent\AgentCrawler\results --max-iter=100 --max-home-retreats=10
