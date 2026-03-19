#!/usr/bin/env python3
"""AnyRouter Check-in Web Server"""

import json
import shutil
import threading
import uuid
import logging
from datetime import datetime, date
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from playwright.sync_api import sync_playwright

app = Flask(__name__, static_folder="static")
log = logging.getLogger("checkin")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config" / "accounts.json"
PROFILES_DIR = BASE_DIR / "data" / "profiles"
HISTORY_FILE = BASE_DIR / "data" / "history.json"
DEFAULT_URL = "https://anyrouter.top"
DEFAULT_TIMEOUT = 30

# Thread-safe status tracking
_lock = threading.Lock()
_account_status = {}
_busy = False
_refreshing = False

# Common browser launch args for both login and checkin
BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
]


def _launch_ctx(p, profile_dir, headless=False):
    """Launch a persistent browser context using system Chrome."""
    return p.chromium.launch_persistent_context(
        profile_dir,
        channel="chrome",
        headless=headless,
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
        args=BROWSER_ARGS,
        ignore_default_args=["--enable-automation"],
    )


# ── Config helpers ─────────────────────────────────────────

def load_config():
    if not CONFIG_FILE.exists():
        return {"accounts": [], "headless": True, "timeout": DEFAULT_TIMEOUT}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def has_session(name):
    d = PROFILES_DIR / name
    return d.exists() and any(d.iterdir())


def get_profile_dir(name):
    d = PROFILES_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def load_history():
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get("history", [])


def append_history(entry):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    history = load_history()
    history.insert(0, entry)
    history = history[:500]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"history": history}, f, indent=2, ensure_ascii=False)


def checked_today(name):
    today_str = date.today().isoformat()
    for entry in load_history():
        if entry["account"] == name and entry["time"][:10] == today_str and entry["status"] == "success":
            return True
    return False


def set_status(name, state, message="", balance=None):
    with _lock:
        entry = {
            "state": state,
            "message": message,
            "time": datetime.now().isoformat(),
        }
        if balance is not None:
            entry["balance"] = balance
        elif name in _account_status and "balance" in _account_status[name]:
            entry["balance"] = _account_status[name]["balance"]
        _account_status[name] = entry


# ── Username extraction ────────────────────────────────────
# (Moved inline with the JS constants above)


# ── Check-in toast/popup detection ─────────────────────────

# JS: Detect Semi Design toast for check-in result.
# The site uses Semi UI's Toast component with class "semi-toast".
# Success toast text example: "签到成功，获得 $25 额度"
DETECT_TOAST_JS = r"""
(() => {
    // Strategy 1: Semi Design Toast (primary detection)
    const semiToasts = document.querySelectorAll('.semi-toast-content-text');
    for (const el of semiToasts) {
        const text = el.innerText?.trim();
        if (!text) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;

        if (/签到/.test(text)) {
            const isSuccess = /成功|获得|领取/.test(text);
            const isFail = /失败|错误|已签|重复/.test(text);
            return { found: true, text, type: isFail ? 'fail' : (isSuccess ? 'success' : 'unknown') };
        }
    }

    // Strategy 2: Broader semi-toast search (fallback)
    const allToasts = document.querySelectorAll('.semi-toast');
    for (const el of allToasts) {
        const text = el.innerText?.trim();
        if (!text) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;

        if (/签到|check.?in|额度|credit/i.test(text)) {
            const isSuccess = /成功|获得|领取|\$\d/.test(text);
            return { found: true, text, type: isSuccess ? 'success' : 'unknown' };
        }
    }

    // Strategy 3: Generic toast/notification selectors
    const genericSelectors = [
        '[class*="toast"]', '[class*="Toast"]',
        '[class*="notification"]', '[class*="message"]',
        '[role="alert"]', '[role="status"]',
    ];
    const seen = new Set();
    for (const sel of genericSelectors) {
        try {
            for (const el of document.querySelectorAll(sel)) {
                if (seen.has(el)) continue;
                seen.add(el);
                const text = el.innerText?.trim();
                if (!text || text.length > 200) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) continue;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;

                if (/签到|check.?in|额度|credit/i.test(text)) {
                    const isSuccess = /成功|获得|领取|\$\d/.test(text);
                    const isFail = /失败|错误|error|fail|过期/i.test(text);
                    return { found: true, text: text.substring(0, 150), type: isFail ? 'fail' : (isSuccess ? 'success' : 'unknown') };
                }
            }
        } catch {}
    }

    // Strategy 4: Full page text scan
    const body = document.body?.innerText || '';
    const patterns = [
        /签到成功[^。\n]{0,30}/,
        /签到失败[^。\n]{0,30}/,
        /获得\s*\$\d+[^。\n]{0,20}/,
    ];
    for (const pat of patterns) {
        const m = body.match(pat);
        if (m) {
            const text = m[0].trim();
            const isSuccess = /成功|获得/.test(text);
            return { found: true, text, type: isSuccess ? 'success' : 'fail' };
        }
    }

    return { found: false, text: '', type: 'unknown' };
})()
"""

# JS: Extract username from localStorage (anyrouter stores user JSON there)
EXTRACT_USERNAME_JS = r"""
(() => {
    // Primary: localStorage "user" key (anyrouter specific)
    try {
        const raw = localStorage.getItem('user');
        if (raw) {
            const obj = JSON.parse(raw);
            const name = obj.display_name || obj.username || obj.name;
            if (name && typeof name === 'string') return name;
        }
    } catch {}

    // Fallback: other common storage patterns
    for (const store of [localStorage, sessionStorage]) {
        try {
            for (let i = 0; i < store.length; i++) {
                const key = store.key(i);
                if (/^user$|^profile$|^auth$|^userInfo$/i.test(key)) {
                    const val = store.getItem(key);
                    const obj = JSON.parse(val);
                    const name = obj.display_name || obj.username || obj.user_name ||
                                 obj.name || obj.nickname || obj.nick_name ||
                                 obj.user?.username || obj.user?.name;
                    if (name && typeof name === 'string' && name.length < 60) return name;
                }
            }
        } catch {}
    }
    return null;
})()
"""

# JS: Check if page is a login page
CHECK_LOGIN_PAGE_JS = r"""
(() => {
    // If URL has login/signin path
    if (/\/(login|signin|sign-in|auth)\b/i.test(location.pathname)) return true;
    // If password input is visible
    const pwInputs = document.querySelectorAll('input[type="password"]');
    for (const inp of pwInputs) {
        const rect = inp.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) return true;
    }
    return false;
})()
"""


def extract_username(page):
    """Extract username from page (primarily via localStorage)."""
    try:
        result = page.evaluate(EXTRACT_USERNAME_JS)
        if result and isinstance(result, str):
            name = result.strip()
            name = "".join(c for c in name if c not in r'\/:*?"<>|')
            if name:
                log.info(f"  Username detected: {name}")
                return name
    except Exception:
        pass
    return None


def verify_checkin(page, url):
    """Verify check-in by detecting the Semi Design success toast.

    Returns (success: bool, message: str).
    """
    # Check 1: Is this a login page? (session expired)
    try:
        if page.evaluate(CHECK_LOGIN_PAGE_JS):
            return False, "会话已过期，被重定向到登录页"
    except Exception:
        pass

    # Check 2: Detect check-in toast (the main check!)
    try:
        result = page.evaluate(DETECT_TOAST_JS)
        if result and result.get("found"):
            text = result["text"]
            if result["type"] == "success":
                return True, text
            elif result["type"] == "fail":
                return False, text
            else:
                return True, f"检测到提示: {text}"
    except Exception:
        pass

    # Check 3: No toast found - check if user is logged in at all
    username = extract_username(page)
    if username:
        return True, f"已登录: {username} (未检测到签到弹窗，可能已签过)"

    # Check 4: Fallback - page loaded but no login info
    try:
        title = page.title()
        body_length = page.evaluate("document.body?.innerText?.length || 0")
        if body_length > 100:
            return False, f"页面已加载但未检测到登录状态 ({title})"
    except Exception:
        pass

    return False, "无法确认签到状态"


# JS: Extract balance from the console page (anyrouter shows "当前余额 $xxx")
EXTRACT_BALANCE_JS = r"""
(() => {
    // Look for the text "当前余额" and get the dollar amount near it
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
        if (walker.currentNode.textContent?.trim() === '当前余额') {
            const parent = walker.currentNode.parentElement;
            const container = parent?.parentElement;
            if (container) {
                const match = container.innerText.match(/\$([\d,.]+)/);
                if (match) return match[0];
            }
        }
    }
    // Fallback: scan for "当前余额" followed by dollar amount
    const text = document.body?.innerText || '';
    const m = text.match(/当前余额[\s\S]{0,20}(\$[\d,.]+)/);
    if (m) return m[1];
    return null;
})()
"""


def extract_balance(page):
    """Extract current balance from the console page."""
    try:
        result = page.evaluate(EXTRACT_BALANCE_JS)
        if result and isinstance(result, str):
            return result.strip()
    except Exception:
        pass
    return None


# ── Core operations ────────────────────────────────────────

def do_checkin(name, url, headless=True, timeout=30000):
    set_status(name, "checking")

    if not has_session(name):
        set_status(name, "no_session", "未保存会话")
        append_history({"account": name, "time": datetime.now().isoformat(), "status": "skipped", "message": "未保存会话"})
        return

    profile_dir = get_profile_dir(name)
    balance = None
    try:
        with sync_playwright() as p:
            ctx = _launch_ctx(p, profile_dir, headless=headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_load_state("networkidle", timeout=timeout)

            # Wait for the check-in toast to appear.
            success = False
            message = ""
            for wait_ms in [2000, 3000, 5000]:
                page.wait_for_timeout(wait_ms)
                success, message = verify_checkin(page, url)
                if success or "过期" in message or "登录" in message:
                    break

            # Extract balance after check-in
            if success:
                balance = extract_balance(page)
                if balance:
                    message = f"{message} (余额: {balance})"

            ctx.close()

        if success:
            set_status(name, "success", message, balance=balance)
            append_history({"account": name, "time": datetime.now().isoformat(), "status": "success", "message": message, "balance": balance})
            log.info(f"[{name}] 签到成功 - {message}")
        else:
            set_status(name, "failed", message)
            append_history({"account": name, "time": datetime.now().isoformat(), "status": "failed", "message": message})
            log.warning(f"[{name}] 签到失败 - {message}")

    except Exception as e:
        msg = str(e)[:200]
        set_status(name, "failed", msg)
        append_history({"account": name, "time": datetime.now().isoformat(), "status": "failed", "message": msg})
        log.error(f"[{name}] 签到异常 - {msg}")


def do_refresh_balance(name, url):
    """Refresh balance for a single account without checking in."""
    with _lock:
        current = _account_status.get(name, {})
        if current.get("state") in ("checking", "logging_in", "refreshing"):
            return

    if not has_session(name):
        return

    set_status(name, "refreshing")
    profile_dir = get_profile_dir(name)

    try:
        with sync_playwright() as p:
            ctx = _launch_ctx(p, profile_dir, headless=True)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            console_url = url.rstrip("/") + "/console"
            page.goto(console_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=30000)
            page.wait_for_timeout(3000)

            balance = extract_balance(page)
            ctx.close()

        set_status(name, "idle", balance=balance)
        if balance:
            log.info(f"[{name}] 余额刷新: {balance}")
        else:
            log.warning(f"[{name}] 未能获取余额")
    except Exception as e:
        msg = str(e)[:200]
        set_status(name, "idle", message=msg)
        log.error(f"[{name}] 余额刷新异常 - {msg}")


def do_login(name, url):
    """Open browser for an existing account to re-login."""
    set_status(name, "logging_in", "浏览器已打开，请登录后关闭窗口")
    profile_dir = get_profile_dir(name)
    try:
        with sync_playwright() as p:
            ctx = _launch_ctx(p, profile_dir, headless=False)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded")
            _poll_login_then_wait_close(ctx, page, name)
            try:
                ctx.close()
            except Exception:
                pass
        set_status(name, "logged_in", "会话已保存")
    except Exception as e:
        set_status(name, "login_failed", str(e)[:200])


def _poll_login_then_wait_close(ctx, page, status_key):
    """Single-thread polling: check login on all pages every 2s.

    Playwright is NOT thread-safe, so we must poll from the same thread.
    Resilient to page navigations during OAuth flow.
    Returns detected username or None.
    """
    detected_name = None

    for _ in range(300):  # Max ~10 minutes
        # Sleep 2s via Playwright
        try:
            page.wait_for_timeout(2000)
        except Exception:
            # Page might have navigated or closed during OAuth
            # Try to get a valid page reference
            try:
                pages = ctx.pages
                if not pages:
                    break  # All pages closed = browser closed
                page = pages[0]  # Switch to first available page
                continue
            except Exception:
                break  # Context destroyed = browser closed

        # Check all open pages for login
        try:
            pages = ctx.pages
            if not pages:
                break
        except Exception:
            break

        for pg in pages:
            try:
                name = extract_username(pg)
                if name and name != detected_name:
                    detected_name = name
                    set_status(status_key, "logging_in",
                               f"已检测到用户「{name}」，可以关闭浏览器了")
                    log.info(f"  Login detected: {name}")
                    # Navigate main page to /console
                    try:
                        first_page = ctx.pages[0]
                        if '/console' not in first_page.url:
                            first_page.goto("https://anyrouter.top/console",
                                            wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    break
            except Exception:
                continue

        if detected_name:
            # Login confirmed - wait for user to close browser
            try:
                page = ctx.pages[0] if ctx.pages else page
                page.wait_for_event("close", timeout=0)
            except Exception:
                pass
            break

    return detected_name


def do_login_new(url):
    """Open browser with a temp profile, auto-detect username after login.

    Single-thread approach: poll for login every 2s on all pages,
    then wait for browser close. No threading (Playwright is not thread-safe).
    """
    temp_id = f"_temp_{uuid.uuid4().hex[:8]}"
    set_status(temp_id, "logging_in", "浏览器已打开，请登录后关闭窗口")
    profile_dir = get_profile_dir(temp_id)
    detected_name = None

    try:
        with sync_playwright() as p:
            ctx = _launch_ctx(p, profile_dir, headless=False)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded")

            detected_name = _poll_login_then_wait_close(ctx, page, temp_id)

            # Final extraction attempt if not detected yet
            if not detected_name:
                for pg in ctx.pages:
                    try:
                        detected_name = extract_username(pg)
                        if detected_name:
                            break
                    except Exception:
                        pass

            try:
                ctx.close()
            except Exception:
                pass

    except Exception as e:
        log.error(f"Login error: {e}")
        set_status(temp_id, "login_failed", str(e)[:200])
        # Cleanup temp dir
        temp_dir = PROFILES_DIR / temp_id
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        return

    # Determine final account name
    final_name = detected_name

    if not final_name:
        # Fallback: keep temp name but mark it clearly
        set_status(temp_id, "login_failed", "无法识别用户名，请手动添加")
        log.warning("Could not auto-detect username")
        # Cleanup temp dir
        temp_dir = PROFILES_DIR / temp_id
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        return

    # Check for duplicate
    config = load_config()
    if any(a["name"] == final_name for a in config["accounts"]):
        # Account already exists - just update its profile
        existing_dir = PROFILES_DIR / final_name
        temp_dir = PROFILES_DIR / temp_id
        if existing_dir.exists():
            shutil.rmtree(existing_dir)
        temp_dir.rename(existing_dir)
        set_status(final_name, "logged_in", "会话已更新")
        # Remove temp status
        with _lock:
            _account_status.pop(temp_id, None)
        log.info(f"Updated existing account: {final_name}")
        return

    # Rename temp profile dir to final name
    temp_dir = PROFILES_DIR / temp_id
    final_dir = PROFILES_DIR / final_name
    try:
        temp_dir.rename(final_dir)
    except Exception:
        # If rename fails, copy
        if final_dir.exists():
            shutil.rmtree(final_dir)
        shutil.copytree(str(temp_dir), str(final_dir))
        shutil.rmtree(temp_dir)

    # Add to config
    config["accounts"].append({"name": final_name, "url": url})
    save_config(config)

    # Update status: remove temp, add final
    with _lock:
        _account_status.pop(temp_id, None)
    set_status(final_name, "logged_in", "会话已保存")
    log.info(f"New account added: {final_name}")


# ── Routes ─────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/accounts")
def get_accounts():
    config = load_config()
    history = load_history()

    last_map = {}
    for entry in history:
        n = entry["account"]
        if n not in last_map:
            last_map[n] = entry

    result = []
    for a in config["accounts"]:
        n = a["name"]
        s = _account_status.get(n, {})
        last = last_map.get(n)
        result.append({
            "name": n,
            "url": a.get("url", DEFAULT_URL),
            "hasSession": has_session(n),
            "state": s.get("state", "idle"),
            "message": s.get("message", ""),
            "balance": s.get("balance") or (last.get("balance") if last else None),
            "checkedToday": checked_today(n),
            "lastCheckin": last["time"] if last else None,
            "lastStatus": last["status"] if last else None,
        })
    return jsonify(result)


@app.route("/api/accounts", methods=["POST"])
def add_account():
    data = request.json
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    config = load_config()
    if any(a["name"] == name for a in config["accounts"]):
        return jsonify({"error": "Already exists"}), 409
    config["accounts"].append({"name": name, "url": data.get("url", DEFAULT_URL)})
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/accounts/<name>", methods=["DELETE"])
def remove_account(name):
    config = load_config()
    config["accounts"] = [a for a in config["accounts"] if a["name"] != name]
    save_config(config)
    d = PROFILES_DIR / name
    if d.exists():
        shutil.rmtree(d)
    with _lock:
        _account_status.pop(name, None)
    return jsonify({"ok": True})


@app.route("/api/login-new", methods=["POST"])
def login_new():
    """Open browser for a brand new account. Auto-detects username."""
    # Check if another login is already in progress
    with _lock:
        for s in _account_status.values():
            if s.get("state") == "logging_in":
                return jsonify({"error": "另一个登录正在进行中，请先关闭浏览器"}), 409

    url = (request.json or {}).get("url", DEFAULT_URL)
    threading.Thread(target=do_login_new, args=(url,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/login/<name>", methods=["POST"])
def login_account(name):
    """Re-login an existing account."""
    config = load_config()
    acc = next((a for a in config["accounts"] if a["name"] == name), None)
    url = acc["url"] if acc else DEFAULT_URL
    threading.Thread(target=do_login, args=(name, url), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/checkin", methods=["POST"])
def checkin_all():
    global _busy
    if _busy:
        return jsonify({"error": "Already running"}), 409
    config = load_config()
    accounts = [a for a in config["accounts"] if has_session(a["name"])]
    if not accounts:
        return jsonify({"error": "没有可签到的账号（请先登录）"}), 400
    headless = config.get("headless", True)
    timeout = config.get("timeout", DEFAULT_TIMEOUT) * 1000

    def run():
        global _busy
        _busy = True
        try:
            for a in accounts:
                do_checkin(a["name"], a.get("url", DEFAULT_URL), headless, timeout)
        finally:
            _busy = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "count": len(accounts)})


@app.route("/api/checkin/<name>", methods=["POST"])
def checkin_one(name):
    config = load_config()
    acc = next((a for a in config["accounts"] if a["name"] == name), None)
    if not acc:
        return jsonify({"error": "Not found"}), 404
    if not has_session(name):
        return jsonify({"error": "请先登录此账号"}), 400
    url = acc.get("url", DEFAULT_URL)
    headless = config.get("headless", True)
    timeout = config.get("timeout", DEFAULT_TIMEOUT) * 1000
    threading.Thread(target=do_checkin, args=(name, url, headless, timeout), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/refresh-balance", methods=["POST"])
def refresh_balance_all():
    global _refreshing
    if _refreshing:
        return jsonify({"error": "Already refreshing"}), 409
    config = load_config()
    acc_map = {a["name"]: a for a in config["accounts"] if has_session(a["name"])}
    if not acc_map:
        return jsonify({"error": "没有可刷新的账号"}), 400

    # Frontend can send preferred processing order
    data = request.get_json(silent=True) or {}
    order = data.get("order")
    if order and isinstance(order, list):
        ordered = [acc_map[n] for n in order if n in acc_map]
        # Append any accounts not in the order list
        seen = {a["name"] for a in ordered}
        ordered += [a for a in acc_map.values() if a["name"] not in seen]
    else:
        ordered = list(acc_map.values())

    def run():
        global _refreshing
        _refreshing = True
        try:
            for a in ordered:
                do_refresh_balance(a["name"], a.get("url", DEFAULT_URL))
        finally:
            _refreshing = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "count": len(ordered)})


@app.route("/api/refresh-balance/<name>", methods=["POST"])
def refresh_balance_one(name):
    config = load_config()
    acc = next((a for a in config["accounts"] if a["name"] == name), None)
    if not acc:
        return jsonify({"error": "Not found"}), 404
    if not has_session(name):
        return jsonify({"error": "请先登录此账号"}), 400
    url = acc.get("url", DEFAULT_URL)
    threading.Thread(target=do_refresh_balance, args=(name, url), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/history")
def get_history():
    return jsonify(load_history()[:100])


if __name__ == "__main__":
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
    print("\n  AnyRouter 签到助手")
    print("  http://localhost:8686\n")
    app.run(host="127.0.0.1", port=8686, debug=False)
