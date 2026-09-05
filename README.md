# PaperSpider

每周一、周四北京时间 **08:17** 自动生成 AI 论文精选，每期目标 10 篇，包含领域、简短摘要和原文链接。只获取标题与摘要等元数据，**不下载 PDF**。

[查看报告](reports/) · [运行状态与手动补跑](https://github.com/Solaris-celeste/PaperSpider/actions/workflows/twice-weekly-report.yml)

## 选题与来源

- 合并 arXiv 新投稿和 Hugging Face Daily Papers 社区精选；两条获取路径独立运行，单个来源失败会记录在报告中。
- 定时出报回看最近七个 UTC 日期，按原始投稿日期筛选，避免把刚上热门的旧论文当作新论文。曾推荐论文按去版本号的 arXiv ID 去重，同期再按标题合并。
- 覆盖大模型与智能体、生成模型、强化学习、世界模型与具身智能、多模态、视觉和自然语言处理。优先给不同领域保留名额，再按相关性和社区信号补齐。
- Hugging Face 推荐票、作者代码、GitHub stars、Semantic Scholar 引用与 HN 讨论辅助排序；热度不等于质量，星级只表示本期相对优先级。
- 配置兼容接口后，只根据摘要生成 2–3 句中文速览；未配置或接口失败时，明确标注原文节选并优先摘取方法或结论句，避免虚构中文结果。

## 自动运行与容错

主任务为周一、周四 08:17（北京时间），周二、周五同一时间补试。已成功发布的当期报告会跳过，不重复消耗接口或重复推荐。GitHub 调度可能延迟，不能保证精确到分钟。

报告自动提交到 `reports/YYYY-MM-DD.md`，同时保存至 Actions 运行摘要及附件（30 天）。Git 提交配置机器人身份，并串行执行避免并发覆盖。所有来源失败或没有未推荐的新论文时，任务明确失败，不写空报告、不更新去重状态；不足五篇时如实注明。附件上传故障不阻止 Git 提交。

手动使用 Actions 的 **Run workflow** 可补最近一期。修改程序、测试、配置或工作流并推送 `main` 也会运行测试及补报。默认交付渠道是本仓库，尚未配置邮箱或聊天软件推送。

## 配置与本地运行

Python 3.10+，无第三方运行依赖；GitHub 使用 Python 3.11。修改 `config.toml` 可调整主题、arXiv 分类、候选上限与目标篇数（5–15）。实际篇数取决于来源可用性及去重后的候选量。

```bash
python -m paper_spider --scheduled
python -m paper_spider --date 2026-09-03 --scheduled
python -m paper_spider --start 2026-08-31 --end 2026-09-05 --no-ai
python -m unittest discover -s tests -v
```

`--scheduled` 回看七天并跳过已经发布的一期；不带该参数时，`--date` 使用最近一个周一或周四及其前一个出报日之间的窗口。`--start` 与 `--end` 可指定任意窗口，不能与 `--scheduled` 混用。历史补跑可以配合 `--output-dir` 与 `--state-file` 使用单独目录；`--include-seen` 允许重新选入已推荐论文。

GitHub 仓库 Secrets 可设置 `LLM_API_KEY`，并按需设置 `LLM_BASE_URL`、`LLM_MODEL` 来生成中文摘要。`GITHUB_TOKEN` 由 Actions 自动提供，本地可选配置以提升查询限额。`.env.example` 只是示例，程序不会自动加载 `.env`，需将变量导入环境。不要将密钥写进配置或报告。
