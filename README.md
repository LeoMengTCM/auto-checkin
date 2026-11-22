# AnyRouter Auto Check-in

AnyRouter 自动签到工具，支持多账号管理、定时自动签到、余额查询、可用性检测、Web UI 管理面板。

**AgentRouter 标签页**：同一套工具里新增了 agentrouter.org 的独立管理页，账号、浏览器会话、余额和签到记录与 AnyRouter 完全分开。

## 功能

- 多账号自动签到（基于 Patchright 持久化浏览器会话，抗 Cloudflare 检测）
- **AgentRouter 独立标签页**：单独的账号列表、登录、签到、余额刷新、可用性检测与历史记录
- **定时自动签到**：每天在设定时间（加随机延迟）自动批量签到；错过时间点会自动补签；已签到的账号自动跳过
- **签到结果通知**：支持 Bark / Server酱 / 通用 JSON Webhook，定时签到完成后推送结果摘要
- 一键检测账号可用性：用账号 API 令牌发送一条符合 Claude Code 格式的真实请求（Opus + 1M 上下文），验证令牌 / 余额 / 中转链路是否正常，支持单账号与批量，遇临时性错误（503/429）自动重试
- 一键刷新所有账号余额，按余额排序
- 账号可重命名 / 加备注 / 管理 API 令牌（存储脱敏展示，可一键清除）
- 签到历史记录
- 打开已保存会话的浏览器，手动检查 key 状态和签到情况
- 设置面板：定时签到、通知、修改管理员密码
- 暗色模式，Web UI 管理面板（Claude/Anthropic 设计风格）

## 项目结构

```
auto-checkin/
├── src/
│   ├── server.py           # Flask Web 服务（含调度器与通知）
│   ├── checkin.py          # CLI 签到工具
│   └── static/
│       └── index.html      # 前端页面
├── config/
│   ├── accounts.json       # AnyRouter 账号与全局设置（gitignore）
│   └── agentrouter_accounts.json  # AgentRouter 账号（gitignore）
├── data/
│   ├── profiles/           # 浏览器会话（gitignore）
│   │   └── agentrouter/    # AgentRouter 的独立会话目录
│   ├── history.json        # AnyRouter 签到历史（gitignore）
│   └── agentrouter_history.json   # AgentRouter 签到历史（gitignore）
├── logs/                   # 运行日志（server.log 滚动保留）
├── tests/
│   └── test_server.py      # 单元测试
└── requirements.txt
```

## 本地开发

```bash
# 安装依赖
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# 使用系统已安装的 Chrome；如需补丁版 Chromium 可选:
# patchright install chromium

# 启动
python src/server.py
# 访问 http://localhost:8686
```

监听地址与端口可在 `config/accounts.json` 中通过 `host` / `port` 字段修改（默认 `127.0.0.1:8686`）。

## AgentRouter 标签页

顶部导航的 **AgentRouter** 标签页管理 agentrouter.org 的账号，数据与 AnyRouter 分开存放。

- 首次使用：切到 AgentRouter 标签，点「添加账号」，在打开的浏览器里用 LinuxDO（或 GitHub）登录，关闭窗口后会话自动保存。
- **签到机制与 AnyRouter 不同**：agentrouter 没有独立的签到接口，每日签到是在一次成功的登录里发放的，并记一条系统日志（「每日签到成功，增加额度 $25」）。已登录状态下普通访问不会重复触发，所以自动签到会先清掉 agentrouter 自己的 httpOnly 登录 cookie（不影响 LinuxDo 的登录），再重放一次 LinuxDO OAuth 登录；授权页会自动授权，不需要输入密码。
- 登录完成后读取当日的系统日志确认结果，因此「签到成功」和「今日已签到」都能准确区分。
- 这依赖 profile 里仍然有效的 LinuxDo 会话。过期时会提示手动重新登录；点账号的「重新登录」即可。
- 定时签到会同时覆盖两个站点的账号（共用设置面板里的时间与通知配置）。
- 可用性检测默认用 `deepseek-v4-flash`（agentrouter 的 Claude 模型仅对 svip/test 分组开放）。

## 定时签到与通知

在 Web UI 右上角齿轮打开设置：

- **定时自动签到**：设定每天的签到时间和随机延迟（0~120 分钟，避免每天固定同一秒触发）。服务启动或开启开关时，若当天设定时间已过且尚有账号未签到，会在几分钟内自动补签。
- **通知**：填入 Bark（`https://api.day.app/你的Key`）、Server酱（`https://sctapi.ftqq.com/xxx.send`）或任意接收 `{title, body}` JSON POST 的 Webhook 地址，定时签到完成后会推送每个账号的结果，可先发测试通知验证。

注意：定时签到需要 `python src/server.py` 保持运行（例如挂在常开的 Mac 上）。

## CLI 用法

```bash
python src/checkin.py login <账号名>     # 打开浏览器手动登录
python src/checkin.py checkin [账号名]    # 自动签到
python src/checkin.py open <账号名>      # 打开浏览器手动检查状态
python src/checkin.py probe [账号名]     # 用 API 令牌检测账号可用性
python src/checkin.py list               # 查看账号列表
```

## 测试

```bash
python3 -m unittest discover tests -v
```
