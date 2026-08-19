# PaperSpider

面向大模型、生成模型、强化学习、世界模型、智能体和多模态等方向的 AI 论文双报工具。它在每周一、周四生成 5-15 篇 Markdown 精选：包含论文链接、中文速览、领域、代码链接、热度信号和相对星级。

## 工作方式

1. 以报告日前一个出报日为起点。例如周一报告覆盖上周四至本周一；周四报告覆盖本周一至周四，均按 UTC 日期。
2. 从 arXiv 的 `cs.AI`、`cs.CL`、`cs.CV`、`cs.LG`、`stat.ML` 新投稿抓取候选，并按 `config.toml` 主题词筛选。
3. 识别 arXiv 备注中的 ICLR、ICML、NeurIPS、ACL、EMNLP、CVPR；再查询 Semantic Scholar 引用、匹配的 GitHub 仓库 stars 和 Hacker News 讨论分数。
4. 用主题相关性、顶会标记和外部热度信号排序。代码与讨论信号是辅助判断，星级只表示本期候选的相对阅读优先级。
5. 若配置了 OpenAI 兼容接口，选中的论文使用 AI 生成 2-3 句中文速览；否则从原始摘要生成保守的提要。

arXiv API 要求调用方缓存结果并避免高频请求；本项目每期只做一次有限查询。外部信号 API 限流或不可用时会自动降级，仍会产出报告。

## 本地运行

要求 Python 3.10+，没有第三方运行时依赖：

```bash
cd PaperSpider
cp .env.example .env
# 可选：将 .env 中的变量导入当前 shell，或直接在环境中设置 LLM_API_KEY
python -m paper_spider --date 2026-08-17
```

`--date` 用于周一或周四的定期出报。需要随时手动生成时，使用 `--start` 和 `--end` 指定任意日期窗口。输出是 `reports/YYYY-MM-DD.md`，已出报论文记录在 `data/seen.json`，默认不会重复入选。常用选项：

```bash
python -m paper_spider --date 2026-08-17 --no-ai
python -m paper_spider --start 2026-08-13 --end 2026-08-14 --no-ai
python -m paper_spider --date 2026-08-17 --include-seen
python -m unittest discover -s tests
```

`.env` 不会被程序自动读取，以避免引入依赖；可由 shell、GitHub Actions Secrets 或部署平台注入。配置 OpenAI 兼容服务时需要设置 `LLM_API_KEY`，可选设置 `LLM_BASE_URL` 与 `LLM_MODEL`。

## 自动出报

`.github/workflows/twice-weekly-report.yml` 会在每周一、周四北京时间 00:00 运行一次、生成报告并提交到仓库，任务结束后没有常驻进程。将该目录初始化并推送到 GitHub 后，在仓库 Secrets 中按需添加：

- `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`：启用 AI 中文摘要；不配置时使用摘要降级模式。
- `GITHUB_TOKEN`：GitHub Actions 已自动提供；本地运行可设置个人 token 以提升 GitHub 搜索限额。

可在 `config.toml` 调整主题、arXiv 分类、顶会名称和目标篇数。目标篇数始终限制为 5-15，保证双报可读性。
