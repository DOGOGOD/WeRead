# WeRead（微阅）

微信公众号文章定时爬取与 AI 智能摘要工具。订阅你关注的公众号，定时抓取最新文章，由 AI 自动生成结构化阅读简报。

## 功能特性

- **扫码登录** — 微信公众平台扫码认证，安全获取文章数据
- **多公众号订阅** — 通过任意一篇历史文章链接即可订阅目标公众号
- **关键词粗筛** — 先按关键词快速过滤，再交给 AI 精细判断相关性
- **定时抓取** — 基于 Cron 表达式定时执行（默认每天 4 次：10:00 / 14:00 / 18:00 / 22:00）
- **正文与图片提取** — Playwright WebKit 渲染抓取完整正文，自动下载文中图片
- **AI 摘要** — 由 OpenClaw/Hermes 内置 AI 根据用户关注点生成个性化阅读简报
- **自动清理** — Blog 目录超过 100 个文件夹时自动删除最旧的 50 个

## 系统架构

```
用户文章链接 → 提取 biz/fakeid → 微信 MP API 拉取文章列表
    → Playwright WebKit 抓取正文 → 关键词粗筛
    → 输出结构化 Markdown → AI 生成阅读简报
```

| 模块 | 文件 | 职责 |
|------|------|------|
| 认证 | `seek/auth.py` | 扫码登录、Token/Cookie 提取、fakeid 解析 |
| 爬取 | `seek/crawler.py` | 微信 API 文章列表、Playwright 正文抓取、图片下载 |
| 调度 | `seek/scheduler.py` | APScheduler 定时任务、时间窗口计算、Markdown 输出 |
| 摘要 | `seek/summarizer.py` | OpenAI/Anthropic 兼容接口 AI 摘要 |
| 模型 | `seek/models.py` | 数据模型 (Article, Subscription, TokenStore) |

## 快速开始

### 环境要求

- Python 3.10+
- Playwright WebKit 浏览器

### 安装与运行方式

#### 方式一：下载 skill 后与 Agent 交互实现安装（推荐）
下载项目提供的 skill 文件，与 AI Agent 交互即可自动完成环境配置、依赖安装及项目运行。例如："请帮我安装 WeRead, 按照README.md中的安装步骤操作"。

#### 方式二：常规手动安装

```bash
# 克隆仓库
git clone https://github.com/your-username/WeRead.git
cd WeRead

# 安装依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器
playwright install webkit
```

### 配置订阅

编辑 `config.yaml`，在 `subscriptions` 中添加你要订阅的公众号：

```yaml
subscriptions:
  - name: "机器之心"
    url: "https://mp.weixin.qq.com/s/xxxxx"   # 该公众号任意一篇文章链接
    keywords:                                   # 粗筛关键词（可选）
      - "AI"
      - "大模型"
    interest: "关注AI行业动态、大模型技术进展和产品发布"
```

> **获取文章链接**：在微信中打开目标公众号的任意一篇文章，点击右上角「复制链接」即可。

### 登录认证

```bash
# 生成二维码并等待扫码（需在 5 分钟内完成）
python main.py login
```

> **注意**：扫码者需是目标公众号的管理员或运营者。

### 测试运行

```bash
# 检查配置和登录状态
python main.py test

# 立即抓取一次
python main.py once
```

### 启动定时任务

```bash
python main.py start
```

## CLI 命令参考

| 命令 | 说明 |
|------|------|
| `python main.py qrcode` | 仅生成登录二维码，不阻塞等待 |
| `python main.py login` | 生成二维码 + 等待扫码登录 + 初始化 fakeid |
| `python main.py once` | 立即执行一次爬取，输出到 `Blog/` 目录 |
| `python main.py start` | 启动后台定时调度（按 cron 表达式执行） |
| `python main.py test` | 检查配置和认证状态 |

## 配置说明

```yaml
# 公众号订阅列表
subscriptions:
  - name: "公众号名称"
    url: "https://mp.weixin.qq.com/s/xxxxx"
    keywords: ["关键词1", "关键词2"]       # 可选，为空则全部保留
    interest: "用户关注点描述"             # AI 总结时的判断依据

# 浏览器配置
browser:
  type: webkit        # Docker 环境推荐 webkit
  headless: true      # 无头模式

# 调度配置
schedule:
  cron_expressions:   # 定时执行时间
    - "0 10 * * *"
    - "0 14 * * *"
    - "0 18 * * *"
    - "0 22 * * *"
  max_pages: 2        # 每次爬取页数（每页约 5 篇）
  request_interval: 10 # 请求间隔（秒），避免限流

# 输出配置
output:
  blog_dir: "./Blog"  # 文章和图片保存目录
```

### 时间窗口规则

| 执行时间 | 抓取范围 |
|----------|----------|
| 10:00 | 前日 22:00 ~ 今日 10:00 |
| 14:00 | 今日 10:00 ~ 14:00 |
| 18:00 | 今日 14:00 ~ 18:00 |
| 22:00 | 今日 18:00 ~ 22:00 |

## 输出结构

```
Blog/
├── index_20260518_1000.md          # 索引文件（AI 阅读入口）
├── 20260518_文章标题_a1b2c3/
│   ├── article.md                  # 文章正文（Markdown）
│   ├── 01.jpg                      # 正文图片
│   └── 02.png
└── ...
```

## Docker 部署

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt && \
    playwright install webkit && \
    playwright install-deps webkit

COPY . .
RUN mkdir -p data Blog

VOLUME ["/app/data", "/app/Blog"]
CMD ["python", "main.py", "start"]
```

```bash
docker build -t weread .
docker run -d \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/Blog:/app/Blog \
  --name weread \
  weread
```

## 故障排查

| 问题 | 原因 | 解决 |
|------|------|------|
| fakeid 获取失败 | 链接无效或网络问题 | 重新运行 `python main.py login` |
| API 返回 200003 | Token 过期 | 重新登录 |
| API 返回 200013 | 请求频率过高 | 增大 `schedule.request_interval` |
| 0 篇文章 | fakeid 不正确 | 检查 fakeid 是否为 base64 格式 |

## 依赖项

- **PyYAML** — 配置文件解析
- **requests / aiohttp** — HTTP 请求
- **Playwright** — 浏览器自动化，文章正文渲染
- **BeautifulSoup4 + lxml** — HTML 解析
- **APScheduler** — 定时任务调度
- **Pydantic** — 数据验证
- **AnyIO** — 异步 I/O 支持

## License

MIT
