# 五家船厂新船项目雷达

定向采集沪东中华、江南造船、上海外高桥造船、厦门船舶重工、武昌船舶重工，以及中国船舶集团和扩展情报源中涉及这五家船厂的民用新船/海工项目动态。

默认核心来源为企业官网和经白名单核验的官方微信公众号原文；扩展情报源可在 `config.yaml` 中以 `group_source: true` 加入。搜狗微信仅用于发现公开文章，不会作为“消息来源”写入结果。

## 功能

- 官网固定域名巡检、列表翻页和正文提取。
- 搜狗微信按白名单公众号名称/微信号定向发现，文章落地页再次校验公众号名称。
- 跟踪签约/立项、开工、铺龙骨、下水/出坞、试航、交付/完工。
- 排除常见军用舰艇关键词。
- SQLite 去重、断点续跑、项目合并和完整里程碑。
- 没有模型密钥时使用本地规则；配置 OpenAI 兼容接口后启用结构化抽取。
- 提供项目主表、里程碑、来源明细和来源采集状态网页。
- Excel 导出仍作为兼容命令保留，不再作为主要查看方式。
- 支持人工补充官网或微信原文链接。

## 安装

```bash
git clone <repository> /opt/shipwatch
cd /opt/shipwatch
python3.11 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
.venv/bin/shipwatch init
```

根据实际网络情况核验并调整 `config.yaml` 中的官网入口和公众号名称。密钥只写入 `.env`。

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
.venv/bin/shipwatch collect --since 2025-06-18
.venv/bin/shipwatch extract
.venv/bin/shipwatch export
```

每日运行：

```bash
.venv/bin/shipwatch daily
```

每日任务直接更新 SQLite，网页会即时读取最新数据；只有手工执行
`.venv/bin/shipwatch export` 时才生成 Excel。

启动网页：

```bash
.venv/bin/shipwatch web --host 127.0.0.1 --port 8080
```

访问 `http://127.0.0.1:8080`。当前云主机部署示例由
`deploy/shipwatch-web.service` 直接监听 `7890` 端口；如以后配置域名和
HTTPS，再使用 `deploy/shipwatch-nginx.conf` 反向代理。

只测试官网或公众号：

```bash
.venv/bin/shipwatch collect --website-only
.venv/bin/shipwatch collect --wechat-only
```

人工补充原文链接：

```bash
.venv/bin/shipwatch add-url \
  'https://mp.weixin.qq.com/s/ARTICLE_ID' \
  --source hudong_zhonghua \
  --title '文章标题'
.venv/bin/shipwatch extract
```

来源 ID：`hudong_zhonghua`、`jiangnan`、`waigaoqiao`、`xiamen`、`wuchang`、`cssc_group`、`ship_offshore`、`imarine`、`zpmc`。

## Linux 定时任务

```bash
chmod +x scripts/shipwatch-daily.sh
crontab -e
```

参考 `deploy/shipwatch.cron.example`。确保服务器时区为 `Asia/Shanghai`，或按服务器时区换算 cron 时间。

## 数据与输出

- SQLite：`data/shipwatch.db`
- Excel：`outputs/船厂新船项目_YYYY-MM-DD.xlsx`
- 日志：`logs/daily.log`

每条项目记录都带官网或微信公众号原文 URL。抓取失败、验证码、公众号不匹配、字段缺失和来源冲突会保留在来源明细或待复核表中，不会静默猜测。

`来源采集状态` 会逐项列出各船厂官网和公众号的成功、部分失败、失败、未执行或“发现成功 / 正文获取失败”状态，以及最后执行时间、发现数量和规范化失败原因。

## 公众号采集边界

微信公众号没有稳定的公开文章列表 API。系统通过搜狗微信发现文章，并只接受最终跳转到 `mp.weixin.qq.com` 且公众号名称符合白名单的页面。若触发验证码，任务记录错误并继续处理其他来源，不尝试绕过验证；可以用 `add-url` 补充漏掉的官方原文。

为避免单个公众号连续验证码或反爬拖慢整轮任务，`app.wechat_consecutive_block_limit` 控制同一公众号连续正文受限后的本轮熔断阈值，默认 8 次。

## 测试

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
