# Changelog

## [Unreleased]

### Added
- 账号支持自定义备注，并可直接修改账号名（重命名会同步迁移浏览器会话目录）
- 账号列表新增“打开浏览器查看”能力，可直接用已保存会话手动确认 key 状态和签到情况
- CLI 新增 `open` 命令，用于本地打开指定账号的浏览器会话

### Changed
- 浏览器自动化从 `playwright` 切换到 `patchright`，按官方最佳实践精简启动参数（使用系统 Chrome + `no_viewport`），修复 LinuxDo OAuth 反复跳 Cloudflare 挑战的问题
- 批量签到和余额刷新会跳过正在登录、手动查看或执行中的账号，避免同一会话被并发占用

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
