# AnyRouter Auto Check-in

AnyRouter 自动签到工具，支持多账号管理、余额查询、Web UI 管理面板。

## 功能

- 多账号自动签到（基于 Patchright 持久化浏览器会话，抗 Cloudflare 检测）
- 一键刷新所有账号余额
- 一键检测账号可用性：用账号 API 令牌发送一条符合 Claude Code 格式的真实请求（Opus + 1M 上下文），验证令牌 / 余额 / 中转链路是否正常，支持单账号与批量，遇临时性错误（503/429）自动重试
- 按余额排序
- 账号可重命名 / 加备注
- 签到历史记录
- 打开已保存会话的浏览器，手动检查 key 状态和签到情况
- 暗色模式
- Web UI 管理面板（Claude/Anthropic 设计风格）

## 项目结构

```
auto-checkin/
├── src/
│   ├── server.py          # Flask Web 服务
│   ├── checkin.py          # CLI 签到工具
│   └── static/
│       └── index.html      # 前端页面
├── config/
│   └── accounts.json       # 账号配置（gitignore）
├── data/
│   ├── profiles/           # 浏览器会话（gitignore）
│   └── history.json        # 签到历史（gitignore）
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

## CLI 用法

```bash
python src/checkin.py login <账号名>     # 打开浏览器手动登录
python src/checkin.py checkin [账号名]    # 自动签到
python src/checkin.py open <账号名>      # 打开浏览器手动检查状态
python src/checkin.py list               # 查看账号列表
```
