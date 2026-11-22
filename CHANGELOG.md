# Changelog

## [Unreleased]

### Added
- **AgentRouter 标签页**：新增 agentrouter.org 的独立管理页，支持添加/登录账号、签到、刷新余额、可用性检测、编辑备注与令牌、查看签到历史
- AgentRouter 使用独立的 `config/agentrouter_accounts.json`、`data/profiles/agentrouter/` 和 `data/agentrouter_history.json`，与 AnyRouter 的账号与会话互不影响
- 定时签到与通知同时覆盖两个站点，通知摘要里用 `[AR]` 前缀区分 AgentRouter 账号
- AgentRouter 标签页添加账号支持「从 AnyRouter 账号复制登录环境」：复制浏览器 profile（LinuxDO 会话一并带过去），复用已登录的 LinuxDo 身份，无需重新输入账号密码

### Changed
- AgentRouter 的签到按站点的真实机制实现：清除 agentrouter 的 httpOnly 登录 cookie → 重放 LinuxDO OAuth 登录 → 读取 `/api/log/self` 系统日志确认「每日签到成功」。已实测普通访问不会触发签到；登出重登才会发放当日额度
- headless 访问 connect.linux.do 时使用普通 Chrome UA，避免 Cloudflare 因 `HeadlessChrome` 标识拦截；OAuth 状态接口 429 时直接报错而不是重试加剧限流

## [1.2.0] - 2026-07-10

### Added
- 定时自动签到：设置面板可配置每天签到时间与随机延迟（0~120 分钟）；若开启时当天时间已过且尚有账号未签到，会在几分钟内自动补签；已签到的账号自动跳过，不会重复打开浏览器
- 签到结果通知：支持 Bark、Server酱 或任意 JSON Webhook，定时签到完成后推送每个账号的结果摘要，可在设置里发送测试通知
- 设置面板（右上角齿轮）：定时签到、通知、修改管理员密码（改密 API 早已存在，这次补上了 UI 入口）
- 一键检测账号可用性：为账号填入 API 令牌后，向 anyrouter 发送一条符合 Claude Code 格式的真实请求（Opus `claude-opus-4-8[1m]` + 1M 上下文）来验证令牌、余额与中转链路；支持单账号检测与「全部检测」，遇 503 / 429 / 网络抖动自动重试，结果按类别展示（可用 / 令牌失效 / 余额不足 / 限流 / 模型不可用 / 服务端错误等）。令牌存于 `config/accounts.json` 的 `token` 字段，接口只返回脱敏值
- 已保存的 API 令牌可在编辑账号时一键清除（点击「✓ 已设置」标记）
- CLI 新增 `probe [账号名]` 命令，用 API 令牌批量检测可用性
- 账号支持自定义备注，并可直接修改账号名（重命名会同步迁移浏览器会话目录与健康状态）
- 账号列表新增“打开浏览器查看”能力，可直接用已保存会话手动确认 key 状态和签到情况
- CLI 新增 `open` 命令，用于本地打开指定账号的浏览器会话
- 服务端日志写入 `logs/server.log`（1MB 滚动，保留 3 份），定时签到有据可查
- 监听地址与端口可通过 `config/accounts.json` 的 `host` / `port` 配置
- 单元测试（`tests/test_server.py`，覆盖探测分类、调度计算、输入校验等纯函数）

### Changed
- 浏览器自动化从 `playwright` 切换到 `patchright`，按官方最佳实践精简启动参数（使用系统 Chrome + `no_viewport`），修复 LinuxDo OAuth 反复跳 Cloudflare 挑战的问题
- 批量签到和余额刷新会跳过正在登录、手动查看或执行中的账号，避免同一会话被并发占用
- 签到结果检测改为以 `POST /api/user/sign_in` 的接口响应为权威信号，DOM 提示检测降级为回退手段；会话有效且当天历史已有成功记录时判定为「今日已签到」而非「待确认」

### Fixed
- 可用性检测的并发竞态：快速重复点击不会再触发两次探测（检测状态改为原子占用）
- 账号状态与健康状态读取加锁快照，消除轮询期间的读写竞争
- 删除账号时同步清理健康状态；`history.json` 损坏时按空历史降级而非报错

### Removed
- 移除 VPS 部署相关文档与 systemd 服务指引（该工具依赖图形界面登录，不适合无头服务器部署）

## [1.0.0] - 2026-03-19

### Added
- Web UI 管理面板（Claude/Anthropic 设计风格）
  - 居中布局 + 顶部导航栏
  - 温暖米色/赤陶色配色方案
  - 暗色模式支持
  - 统计卡片（今日签到/总余额/账号数量）
- 多账号自动签到（Playwright 持久化浏览器上下文）
- 一键刷新所有账号余额（不签到，仅读取）
- 单个账号余额刷新
- 按余额排序（前端排序 + 后端按排序顺序刷新）
- 签到历史记录查看
- 自动检测用户名（添加账号时）
- LinuxDo OAuth 登录支持
- CLI 工具（login / checkin / list / add / remove）
