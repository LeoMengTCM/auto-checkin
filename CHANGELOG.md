# Changelog

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
- systemd 服务部署支持
