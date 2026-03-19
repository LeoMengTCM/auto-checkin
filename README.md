# AnyRouter Auto Check-in

AnyRouter 自动签到工具，支持多账号管理、余额查询、Web UI 管理面板。

## 功能

- 多账号自动签到（基于 Playwright 持久化浏览器会话）
- 一键刷新所有账号余额
- 按余额排序
- 签到历史记录
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
playwright install chromium

# 启动
python src/server.py
# 访问 http://localhost:8686
```

## CLI 用法

```bash
python src/checkin.py login <账号名>     # 打开浏览器手动登录
python src/checkin.py checkin [账号名]    # 自动签到
python src/checkin.py list               # 查看账号列表
```

## 部署到 VPS

详见 [部署指南](docs/deploy.md)。

## 更新

本地修改代码后：

```bash
git add -A && git commit -m "描述" && git push
```

VPS 上执行：

```bash
cd /opt/auto-checkin && git pull && sudo systemctl restart auto-checkin
```

或一键更新：

```bash
ssh root@你的VPS "cd /opt/auto-checkin && git pull && systemctl restart auto-checkin"
```
