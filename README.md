# AnyRouter Auto Check-in

AnyRouter 自动签到工具，支持多账号管理、定时自动签到、余额查询、可用性检测、Web UI 管理面板。

## 功能

- 多账号自动签到（基于 Patchright 持久化浏览器会话，抗 Cloudflare 检测）
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
│   └── accounts.json       # 账号与设置（gitignore）
├── data/
│   ├── profiles/           # 浏览器会话（gitignore）
│   └── history.json        # 签到历史（gitignore）
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
