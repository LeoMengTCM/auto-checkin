# 部署 & 更新指南

## 前置要求

- Ubuntu 22.04+ VPS（最低 1GB RAM）
- 已安装 Nginx Proxy Manager（或其他反代工具）
- 域名已解析到 VPS IP

## 首次部署

### 1. 安装系统依赖

```bash
apt update && apt install -y python3 python3-pip python3-venv git

# 安装 Google Chrome
wget -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
apt install -y /tmp/chrome.deb
rm /tmp/chrome.deb
```

### 2. 克隆项目

```bash
cd /opt
git clone git@github.com:LeoMengTCM/auto-checkin.git
cd auto-checkin
```

> 如果是 private repo，需要先在 VPS 上生成 SSH key 并添加为 GitHub Deploy Key。

### 3. 安装 Python 依赖

```bash
python3 -m venv venv
source venv/bin/activate
pip install flask playwright
playwright install chromium
playwright install-deps chromium
```

### 4. 初始化数据目录

```bash
mkdir -p config data/profiles
```

如果是从本地迁移，将以下文件复制到 VPS：

```bash
# 从本地执行
rsync -avz config/accounts.json root@VPS:/opt/auto-checkin/config/
rsync -avz data/profiles/ root@VPS:/opt/auto-checkin/data/profiles/
rsync -avz data/history.json root@VPS:/opt/auto-checkin/data/
```

### 5. 创建 systemd 服务

```bash
cat > /etc/systemd/system/auto-checkin.service << 'EOF'
[Unit]
Description=AnyRouter Auto Check-in Service
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/auto-checkin
ExecStart=/opt/auto-checkin/venv/bin/python src/server.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable auto-checkin
systemctl start auto-checkin
```

### 6. 配置 Nginx Proxy Manager

1. 添加 Proxy Host
2. Domain: `anyrouter.你的域名`
3. Scheme: `http`
4. Forward Hostname: `127.0.0.1`
5. Forward Port: `8686`
6. SSL 标签页：勾选 Force SSL，申请 Let's Encrypt 证书

## 更新代码

在 VPS 上执行：

```bash
cd /opt/auto-checkin
git pull
systemctl restart auto-checkin
```

从本地一键更新：

```bash
# 先推送代码
git push

# 再远程更新
ssh root@你的VPS "cd /opt/auto-checkin && git pull && systemctl restart auto-checkin"
```

## 账号会话管理

浏览器会话保存在 `data/profiles/<账号名>/` 中，不纳入 Git。

**添加新账号：**
- 在本地运行 Web UI → 点击「添加账号」→ 浏览器会打开登录页
- 登录后关闭浏览器，会话自动保存
- 将 profiles 同步到 VPS：
  ```bash
  rsync -avz data/profiles/<账号名>/ root@VPS:/opt/auto-checkin/data/profiles/<账号名>/
  ```

**会话过期重新登录：**
- 同上，在本地登录后 rsync 到 VPS

## 常用命令

```bash
# 查看服务状态
systemctl status auto-checkin

# 查看日志
journalctl -u auto-checkin -f

# 重启服务
systemctl restart auto-checkin

# 停止服务
systemctl stop auto-checkin
```

## 故障排查

| 问题 | 解决方案 |
|------|----------|
| 签到失败「会话已过期」 | 在本地重新登录该账号，rsync profiles 到 VPS |
| 余额刷新失败 | 检查 VPS 内存 `free -h`，Chrome 需要约 200MB |
| 服务无法启动 | `journalctl -u auto-checkin -n 50` 查看错误 |
| 页面无法访问 | 检查 NPM 配置和 `systemctl status auto-checkin` |
