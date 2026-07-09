# 五家船厂新船项目雷达

定向采集沪东中华、江南造船、上海外高桥造船、厦门船舶重工、武昌船舶重工，以及中国船舶集团和扩展情报源中涉及这五家船厂的民用新船/海工项目动态。

当前核心来源为经白名单核验的微信公众号原文；扩展情报源可在 `config.yaml` 中以 `group_source: true` 加入。官网采集已关闭，历史官网记录仍可保留在数据库中用于追溯。

## 功能

- 打价啦 API 按白名单公众号名称定向发现公开文章，并优先获取公众号正文。
- 跟踪签约/立项、开工、铺龙骨、下水/出坞、试航、交付/完工。
- 排除常见军用舰艇关键词。
- SQLite 去重、断点续跑、项目合并和完整里程碑。
- 没有模型密钥时使用本地规则；配置 OpenAI 兼容接口后启用结构化抽取。
- 提供项目主表、里程碑、来源明细和来源采集状态网页。
- Excel 导出仍作为兼容命令保留，不再作为主要查看方式。
- 支持人工补充微信公众号原文链接。

## 安装

```bash
git clone <repository> /opt/shipyard-radar-main
cd /opt/shipyard-radar-main
python3.12 -m venv venv
venv/bin/pip install -e .
cp .env.example .env
venv/bin/python -m shipwatch.cli init
```

根据实际网络情况核验并调整 `config.yaml` 中的公众号名称。密钥只写入 `.env`。

## 模型配置

默认使用 Responses API 风格的 OpenAI 兼容接口：

```dotenv
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-5-mini
OPENAI_API_MODE=responses
```

如果兼容服务只实现 Chat Completions：

```dotenv
OPENAI_API_MODE=chat_completions
```

不配置 `OPENAI_API_KEY` 时，系统仍可运行，但复杂字段的准确率较低，结果更容易进入“待人工复核”。

## 使用

首次回溯近 12 个月：

```bash
venv/bin/python -m shipwatch.cli collect --since 2025-06-18
venv/bin/python -m shipwatch.cli extract
venv/bin/python -m shipwatch.cli export
```

每日运行：

```bash
venv/bin/python -m shipwatch.cli daily
```

每日任务直接更新 SQLite，网页会即时读取最新数据；只有手工执行
`venv/bin/python -m shipwatch.cli export` 时才生成 Excel。

启动网页：

```bash
venv/bin/python -m shipwatch.cli web --host 127.0.0.1 --port 8080
```

访问 `http://127.0.0.1:8080`。当前云主机部署示例由
`deploy/shipwatch-web.service` 直接监听 `7890` 端口；如以后配置域名和
HTTPS，再使用 `deploy/shipwatch-nginx.conf` 反向代理。

人工补充原文链接：

```bash
venv/bin/python -m shipwatch.cli add-url \
  'https://mp.weixin.qq.com/s/ARTICLE_ID' \
  --source hudong_zhonghua \
  --title '文章标题'
venv/bin/python -m shipwatch.cli extract
```

来源 ID：`hudong_zhonghua`、`jiangnan`、`waigaoqiao`、`xiamen`、`wuchang`、`cssc_group`、`ship_offshore`、`imarine`、`zpmc`。

## Linux 部署与定时任务

当前云主机使用 systemd 统一管理 Web 看板和定时采集：

```text
shipwatch-web.service       Web 看板，监听 7890
shipwatch-daily.service     单次采集任务
shipwatch-daily.timer       每 2 天 07:30 触发采集
```

安装或更新 systemd 单元：

```bash
sudo cp deploy/shipwatch-web.service /etc/systemd/system/
sudo cp deploy/shipwatch-daily.service /etc/systemd/system/
sudo cp deploy/shipwatch-daily.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shipwatch-web.service
sudo systemctl enable --now shipwatch-daily.timer
```

常用管理命令：

```bash
systemctl status shipwatch-web
systemctl restart shipwatch-web
systemctl status shipwatch-daily.timer
systemctl list-timers | grep shipwatch
systemctl start shipwatch-daily.service
journalctl -u shipwatch-daily -f
```

`shipwatch-daily.timer` 使用 `OnCalendar=*-*-1/2 07:30:00`，在服务器时区下每隔 2 个日期日的 07:30 执行；当前云主机为北京时间。旧版 `deploy/shipwatch.cron.example` 和 `scripts/shipwatch-daily.sh` 仅作为兼容参考，不再作为推荐部署方式。

## 数据与输出

- SQLite：`data/shipwatch.db`
- Excel：`outputs/船厂新船项目_YYYY-MM-DD.xlsx`
- 定时采集日志：`data/daily.log`

每条项目记录都带微信公众号原文 URL 或历史来源 URL。抓取失败、验证码、公众号不匹配、字段缺失和来源冲突会保留在来源明细或待复核表中，不会静默猜测。

`来源采集状态` 会逐项列出各公众号来源的成功、部分失败、失败、未执行或“发现成功 / 正文获取失败”状态，以及最后执行时间、发现数量和规范化失败原因。

## 增量发现与接口日志

`post_history` 是分页接口。系统对每个公众号从 `page=1` 开始读取，遇到连续
`discovery_stop_existing_count` 篇已存在且不晚于当前游标的旧文章后停止翻页。
当前默认值为 `5`，可在 `config.yaml` 的 `app.discovery_stop_existing_count`
中调整。

系统同时保存两个时间字段：

- `published_at`：日期，用于页面展示和抽取上下文。
- `published_at_ts`：秒级发布时间，用于增量游标和翻页停止判断。

`crawl_state.cursor` 会记录 `last_seen_published_at`、`last_seen_published_at_ts`
和 `last_seen_url`。旧游标没有秒级字段时，系统会兼容回退到日期判断；下一轮
成功采集后会自动写入秒级游标。

接口用量页会展示 `request_meta` 参数。`post_history` 会记录 `page=1`、
`page=2` 等页码，因此同一公众号一天出现多条 `post_history` 调用通常表示
正常翻页，而不是同一页重复请求。历史调用记录在该字段上线前没有页码参数。

## 公众号采集边界

微信公众号没有稳定的公开文章列表 API。系统优先通过打价啦 API 发现并获取公众号文章；若正文获取失败，会记录错误并继续处理其他来源，不自动破解或绕过验证，也不保存或使用微信 Cookie。可以用 `add-url` 补充漏掉的官方原文。

## 测试

```bash
venv/bin/pip install -e '.[dev]'
venv/bin/pytest
```
