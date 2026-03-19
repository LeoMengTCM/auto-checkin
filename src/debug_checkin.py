#!/usr/bin/env python3
"""诊断脚本：打开网站，截图 + 抓取签到提示的 DOM 结构。

用法：
  python src/debug_checkin.py [profile_name]

如果不指定 profile_name，会列出已有的 profile 供选择。
"""

import sys, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
PROFILES_DIR = BASE_DIR / "data" / "profiles"
DEBUG_DIR = BASE_DIR / "data" / "debug"
URL = "https://anyrouter.top"


def pick_profile():
    profiles = [d.name for d in PROFILES_DIR.iterdir() if d.is_dir() and not d.name.startswith("_")]
    if not profiles:
        print("没有已保存的 profile，将创建临时 profile")
        return "_debug_temp"
    print("可用的 profile：")
    for i, p in enumerate(profiles, 1):
        print(f"  {i}. {p}")
    choice = input("\n选择序号（直接回车用第一个）：").strip()
    idx = int(choice) - 1 if choice.isdigit() else 0
    return profiles[max(0, min(idx, len(profiles) - 1))]


def dump_page(page, tag):
    """截图 + 抓取页面信息"""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    # 截图
    ss_path = DEBUG_DIR / f"{tag}.png"
    page.screenshot(path=str(ss_path), full_page=True)
    print(f"  截图已保存: {ss_path}")

    # 抓取页面所有可见文本元素
    info = page.evaluate(r"""
    (() => {
        const results = {
            url: location.href,
            title: document.title,
            bodyTextLength: document.body?.innerText?.length || 0,
            // 所有可见的 toast / notification / popup / alert 元素
            toasts: [],
            // 页面上包含"签到"相关文字的所有元素
            checkinElements: [],
            // 所有可见的 overlay / modal / dialog
            overlays: [],
            // localStorage 中与用户相关的 key
            storageKeys: [],
        };

        // 1. 搜索所有可能的 toast / notification 元素
        const toastSelectors = [
            '.el-message', '.el-notification', '.el-message-box',
            '.ant-message-notice', '.ant-notification-notice',
            '.n-message', '.n-notification',
            '.v-snack__content',
            '.toast', '.notification', '.notice', '.snackbar',
            '[class*="toast"]', '[class*="Toast"]',
            '[class*="notification"]', '[class*="Notification"]',
            '[class*="message"]', '[class*="Message"]',
            '[class*="notice"]', '[class*="Notice"]',
            '[class*="popup"]', '[class*="Popup"]',
            '[class*="alert"]', '[class*="Alert"]',
            '[class*="snack"]', '[class*="Snack"]',
            '[class*="tip"]', '[class*="Tip"]',
            '[role="alert"]', '[role="status"]',
            '[class*="dialog"]', '[class*="Dialog"]',
            '[class*="modal"]', '[class*="Modal"]',
        ];
        const seen = new Set();
        for (const sel of toastSelectors) {
            try {
                for (const el of document.querySelectorAll(sel)) {
                    if (seen.has(el)) continue;
                    seen.add(el);
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    const text = el.innerText?.trim();
                    if (!text) continue;
                    results.toasts.push({
                        selector: sel,
                        tag: el.tagName,
                        className: el.className?.toString()?.substring(0, 200),
                        id: el.id,
                        text: text.substring(0, 300),
                        visible: rect.width > 0 && rect.height > 0
                            && style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && parseFloat(style.opacity) > 0.05,
                        rect: { x: rect.x, y: rect.y, w: Math.round(rect.width), h: Math.round(rect.height) },
                        opacity: style.opacity,
                    });
                }
            } catch {}
        }

        // 2. 搜索包含"签到"的所有元素
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        const checkinTexts = new Set();
        while (walker.nextNode()) {
            const t = walker.currentNode.textContent?.trim();
            if (t && /签到|check.?in|打卡|额度|积分|余额|credit|balance/i.test(t)) {
                const parent = walker.currentNode.parentElement;
                if (parent && !checkinTexts.has(parent)) {
                    checkinTexts.add(parent);
                    const rect = parent.getBoundingClientRect();
                    results.checkinElements.push({
                        tag: parent.tagName,
                        className: parent.className?.toString()?.substring(0, 200),
                        id: parent.id,
                        text: parent.innerText?.trim()?.substring(0, 300),
                        outerHTML: parent.outerHTML?.substring(0, 500),
                        rect: { x: rect.x, y: rect.y, w: Math.round(rect.width), h: Math.round(rect.height) },
                    });
                }
            }
        }

        // 3. localStorage keys
        try {
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                const val = localStorage.getItem(key);
                results.storageKeys.push({
                    key,
                    valuePreview: val?.substring(0, 200),
                });
            }
        } catch {}

        return results;
    })()
    """)

    info_path = DEBUG_DIR / f"{tag}.json"
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)
    print(f"  页面信息已保存: {info_path}")

    return info


def main():
    profile_name = sys.argv[1] if len(sys.argv) > 1 else pick_profile()
    profile_dir = str(PROFILES_DIR / profile_name)
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    print(f"\n使用 profile: {profile_name}")
    print(f"正在打开浏览器访问 {URL} ...\n")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile_dir,
            channel="chrome",
            headless=False,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")

        print("=" * 50)
        print("浏览器已打开！")
        print("请等待页面加载完成（包括签到弹窗出现）。")
        print("=" * 50)

        # 等待页面 networkidle
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

        # 多次采样，捕捉弹窗
        for i, wait in enumerate([2, 3, 5, 8]):
            print(f"\n--- 第 {i+1} 次采样（等待 {wait} 秒后）---")
            page.wait_for_timeout(wait * 1000)
            info = dump_page(page, f"sample_{i+1}")

            # 打印关键发现
            visible_toasts = [t for t in info.get("toasts", []) if t.get("visible")]
            if visible_toasts:
                print(f"  发现 {len(visible_toasts)} 个可见的 toast/notification:")
                for t in visible_toasts:
                    print(f"    [{t['className'][:60]}] → \"{t['text'][:80]}\"")

            checkin_els = info.get("checkinElements", [])
            if checkin_els:
                print(f"  发现 {len(checkin_els)} 个签到相关元素:")
                for e in checkin_els:
                    print(f"    <{e['tag']} class=\"{e['className'][:60]}\"> → \"{e['text'][:80]}\"")

        input("\n按回车键关闭浏览器并查看完整结果...")
        ctx.close()

    print(f"\n诊断结果保存在: {DEBUG_DIR}/")
    print("请将 sample_*.json 的内容发给我，我来分析具体的 DOM 结构。")


if __name__ == "__main__":
    main()
