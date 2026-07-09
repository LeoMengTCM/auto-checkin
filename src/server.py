#!/usr/bin/env python3
"""AnyRouter Check-in Web Server"""

import json
import re
import shutil
import threading
import uuid
import logging
import time
import urllib.request
import urllib.error
from datetime import datetime, date
from pathlib import Path

from functools import wraps

from flask import Flask, jsonify, request, send_from_directory, session
from patchright.sync_api import sync_playwright
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder="static")
log = logging.getLogger("checkin")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config" / "accounts.json"
PROFILES_DIR = BASE_DIR / "data" / "profiles"
HISTORY_FILE = BASE_DIR / "data" / "history.json"
DEFAULT_URL = "https://anyrouter.top"
DEFAULT_TIMEOUT = 30
CHECKIN_RESULT_WAIT_MS = 20000
CHECKIN_RESULT_POLL_MS = 500

# One-shot diagnostic toggle: dump all check-in network responses to the log
# so we can see anyrouter's real sign-in endpoint & payload shape, then write
# detection heuristics against real data instead of guessing.
CHECKIN_DIAG_LOG = False
_DIAG_URL_RE = re.compile(
    r"check.?in|sign.?in|attendance|daily|reward|credit|quota|user|account|api",
    re.I,
)

# Real sign-in endpoint discovered via diagnostics:
#   POST https://anyrouter.top/api/user/sign_in
#   → success:  {"message":"","success":true}
#   → repeat:   {"message":"已签到...","success":false}  (assumed shape)
SIGNIN_URL_RE = re.compile(r"/api/user/sign_in\b", re.I)
SIGNIN_ALREADY_TOKENS = ("已签", "重复", "already", "duplicate")

# Thread-safe status tracking
_lock = threading.Lock()
_account_status = {}
_account_health = {}
_busy = False
_refreshing = False
_health_busy = False
_io_lock = threading.RLock()
ACTIVE_ACCOUNT_STATES = {"checking", "logging_in", "refreshing", "inspecting"}


def _launch_ctx(p, profile_dir, headless=False):
    """Launch a persistent browser context using system Chrome.

    Follows patchright's stealth best practices: system Chrome via channel,
    natural viewport, no custom locale/UA/headers. Any extra fingerprinting
    option we add here becomes a signal Cloudflare Turnstile can score.
    """
    return p.chromium.launch_persistent_context(
        profile_dir,
        channel="chrome",
        headless=headless,
        no_viewport=True,
    )


# ── Config helpers ─────────────────────────────────────────

def load_config():
    with _io_lock:
        if not CONFIG_FILE.exists():
            return {"accounts": [], "headless": True, "timeout": DEFAULT_TIMEOUT}
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


def save_config(config):
    with _io_lock:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)



# ── Auth helpers ──────────────────────────────────────────

def _ensure_secret_key():
    config = load_config()
    if "secret_key" not in config:
        import secrets
        config["secret_key"] = secrets.token_hex(32)
        save_config(config)
    app.secret_key = config["secret_key"]

_ensure_secret_key()


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        config = load_config()
        if not config.get("password"):
            return jsonify({"error": "请先设置管理员密码"}), 403
        if not session.get("authed"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def _safe_name(name):
    """Validate account name to prevent path traversal."""
    if not name or not isinstance(name, str):
        return False
    name = name.strip()
    if not name or len(name) > 100:
        return False
    # Block path traversal and special characters
    if "/" in name or "\\" in name or "\0" in name:
        return False
    if name in (".", "..") or name.startswith("."):
        return False
    return True


def has_session(name):
    if not _safe_name(name):
        return False
    d = PROFILES_DIR / name
    return d.exists() and any(d.iterdir())


def get_profile_dir(name):
    if not _safe_name(name):
        raise ValueError("Invalid account name")
    d = PROFILES_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def load_history():
    with _io_lock:
        if not HISTORY_FILE.exists():
            return []
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("history", [])


def append_history(entry):
    with _io_lock:
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


def _find_last_history(name):
    """Find the most recent history entry for an account that has a balance."""
    for entry in load_history():
        if entry["account"] == name and entry.get("balance"):
            return entry
    return None


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


def set_health(name, state, message="", category=None):
    """Record the latest Claude Code availability probe result for an account.

    Kept in a separate dict from _account_status so a probe never overwrites the
    account's check-in state (and vice versa) — availability and check-in are two
    independent dimensions shown side by side in the UI.
    """
    with _lock:
        _account_health[name] = {
            "state": state,
            "message": message,
            "category": category,
            "time": datetime.now().isoformat(),
        }


def get_account_state(name):
    with _lock:
        return (_account_status.get(name) or {}).get("state")


def get_account_busy_error(name):
    state = get_account_state(name)
    if state == "checking":
        return "该账号正在签到"
    if state == "logging_in":
        return "该账号正在登录，请先关闭浏览器"
    if state == "refreshing":
        return "该账号正在刷新余额"
    if state == "inspecting":
        return "该账号浏览器已打开，请先关闭窗口"
    return None


def _account_or_error(name, require_session=True):
    """Common pre-flight for per-account endpoints.

    Validates name charset, checks existence, optionally requires a saved
    session, and rejects if the account is currently busy. Returns
    (account_dict, None) on success or (None, (response, status_code)).
    """
    if not _safe_name(name):
        return None, (jsonify({"error": "账号名包含非法字符"}), 400)
    config = load_config()
    acc = next((a for a in config["accounts"] if a["name"] == name), None)
    if not acc:
        return None, (jsonify({"error": "Not found"}), 404)
    if require_session and not has_session(name):
        return None, (jsonify({"error": "请先登录此账号"}), 400)
    busy = get_account_busy_error(name)
    if busy:
        return None, (jsonify({"error": busy}), 409)
    return acc, None


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
            const isSuccess = /成功|获得|领取|\$\d/.test(text);
            const isFail = /失败|错误|重复/.test(text);
            return { found: true, text, type: isSuccess ? 'success' : (isFail ? 'fail' : 'unknown') };
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

    // Strategy 3: Generic toast/notification/modal selectors
    const genericSelectors = [
        '[class*="toast"]', '[class*="Toast"]',
        '[class*="notification"]', '[class*="message"]',
        '[class*="modal"]', '[class*="Modal"]',
        '[class*="dialog"]', '[class*="Dialog"]',
        '[class*="popup"]', '[class*="Popup"]',
        '[class*="checkin"]', '[class*="check-in"]',
        '[class*="banner"]', '[class*="reward"]',
        '[role="alert"]', '[role="status"]',
        '[role="dialog"]', '[role="alertdialog"]',
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

    // Strategy 4: Full page text scan with flexible patterns
    const body = document.body?.innerText || '';
    const successPatterns = [
        /签到成功[^。\n]{0,40}/,
        /已签到[^。\n]{0,40}/,
        /今日.{0,5}签到[^。\n]{0,40}/,
        /(?:获得|领取|增加|奖励|\+)\s*\$?\d+(?:\.\d+)?\s*(?:美元|额度|刀|credit|quota)?[^。\n]{0,30}/i,
        /(?:checked.?in|sign(?:ed)?.?in)[^.\n]{0,40}/i,
    ];
    const failPatterns = [
        /签到失败[^。\n]{0,40}/,
        /(?:check.?in|sign.?in)[^.\n]{0,5}(?:fail|error)[^.\n]{0,30}/i,
    ];
    for (const pat of successPatterns) {
        const m = body.match(pat);
        if (m) return { found: true, text: m[0].trim().slice(0, 150), type: 'success' };
    }
    for (const pat of failPatterns) {
        const m = body.match(pat);
        if (m) return { found: true, text: m[0].trim().slice(0, 150), type: 'fail' };
    }

    return { found: false, text: '', type: 'unknown' };
})()
"""

CHECKIN_MONITOR_INIT_JS = r"""
(() => {
    const key = '__checkinSignal';
    const runId = window.__checkinMonitorRunId || 'default';
    const storageKey = `__checkinSignalStore:${runId}`;
    const existing = window[key];
    if (existing && existing.__installed) return;

    const now = Date.now();
    const loadPersisted = () => {
        try {
            const raw = sessionStorage.getItem(storageKey);
            if (!raw) return {};
            const parsed = JSON.parse(raw);
            if (!parsed || typeof parsed !== 'object') return {};
            if (Array.isArray(parsed.events)) {
                parsed.events = parsed.events
                    .filter((event) => event && event.ts && (now - event.ts) < 5 * 60 * 1000)
                    .slice(-20);
            }
            if (Array.isArray(parsed.network)) {
                parsed.network = parsed.network
                    .filter((event) => event && event.ts && (now - event.ts) < 5 * 60 * 1000)
                    .slice(-10);
            }
            return parsed;
        } catch {
            return {};
        }
    };
    const persisted = loadPersisted();

    const selectors = [
        '.semi-toast-content-text', '.semi-toast',
        '.toast', '.notification', '.notice', '.snackbar',
        '[class*="toast"]', '[class*="Toast"]',
        '[class*="notification"]', '[class*="Notification"]',
        '[class*="message"]', '[class*="Message"]',
        '[class*="notice"]', '[class*="Notice"]',
        '[class*="popup"]', '[class*="Popup"]',
        '[class*="alert"]', '[class*="Alert"]',
        '[class*="snack"]', '[class*="Snack"]',
        '[class*="tip"]', '[class*="Tip"]',
        '.semi-modal', '.semi-modal-content', '.semi-modal-confirm',
        '[class*="modal"]', '[class*="Modal"]',
        '[class*="dialog"]', '[class*="Dialog"]',
        '[class*="drawer"]', '[class*="Drawer"]',
        '[class*="checkin"]', '[class*="checkIn"]', '[class*="CheckIn"]', '[class*="check-in"]',
        '[class*="signin"]', '[class*="signIn"]', '[class*="SignIn"]', '[class*="sign-in"]',
        '[class*="reward"]', '[class*="Reward"]',
        '[class*="banner"]', '[class*="Banner"]',
        '[role="alert"]', '[role="status"]', '[role="dialog"]', '[role="alertdialog"]',
    ];

    const state = window[key] = {
        __installed: true,
        events: Array.isArray(existing && existing.events) ? existing.events : (Array.isArray(persisted.events) ? persisted.events : []),
        network: Array.isArray(existing && existing.network) ? existing.network : (Array.isArray(persisted.network) ? persisted.network : []),
        lastStrongType: existing && existing.lastStrongType || persisted.lastStrongType || null,
        lastStrongText: existing && existing.lastStrongText || persisted.lastStrongText || '',
        updatedAt: existing && existing.updatedAt || persisted.updatedAt || Date.now(),
    };
    const seen = new Set(state.events.map(event => event.key).filter(Boolean));
    const seenNetwork = new Set(state.network.map(event => event.key).filter(Boolean));

    const persist = () => {
        try {
            sessionStorage.setItem(storageKey, JSON.stringify({
                events: state.events.slice(-20),
                network: state.network.slice(-10),
                lastStrongType: state.lastStrongType || null,
                lastStrongText: state.lastStrongText || '',
                updatedAt: state.updatedAt || null,
            }));
        } catch {}
    };

    const normalize = (text) => (text || '')
        .replace(/\s+/g, ' ')
        .trim()
        .slice(0, 150);

    const signalRegex = /签到|check.?in|额度|credit|领取|已签到/i;
    const failRegex = /失败|错误|过期|error|fail/i;
    const successRegex = /成功|获得|领取|\$\d+|已签到/i;

    const classify = (text) => {
        const normalized = normalize(text);
        if (!normalized) return null;
        if (/已签到/i.test(normalized)) {
            return { type: 'success', text: normalized };
        }
        if (!signalRegex.test(normalized)) return null;
        if (failRegex.test(normalized)) {
            return { type: 'fail', text: normalized };
        }
        if (successRegex.test(normalized)) {
            return { type: 'success', text: normalized };
        }
        return null;
    };

    const extractSnippet = (text) => {
        const normalized = normalize(text);
        if (!normalized) return '';
        const patterns = [
            /签到成功[^。,\n]{0,60}/i,
            /签到失败[^。,\n]{0,60}/i,
            /已签到[^。,\n]{0,60}/i,
            /获得\s*\$\d+(?:\.\d+)?\s*额度[^。,\n]{0,40}/i,
        ];
        for (const pattern of patterns) {
            const match = normalized.match(pattern);
            if (match) return normalize(match[0]);
        }
        if (/^[\[{]/.test(normalized)) {
            return '';
        }
        return normalized;
    };

    const pickFirstText = (...values) => {
        for (const value of values) {
            if (typeof value === 'string') {
                const normalized = normalize(value);
                if (normalized) return normalized;
            }
        }
        return '';
    };

    const parseJsonPayload = (payload, url = '') => {
        try {
            const data = JSON.parse(payload);
            if (!data || typeof data !== 'object' || Array.isArray(data)) return null;

            const message = pickFirstText(
                data.message,
                data.msg,
                data.error,
                data.detail,
                data.data && data.data.message,
                data.data && data.data.msg,
                data.data && data.data.error,
                data.data && data.data.detail,
            );

            const classifiedMessage = message ? classify(message) : null;
            if (classifiedMessage) return classifiedMessage;

            const success = data.success === true ||
                data.code === 0 || data.status === 'success' || data.status === 'ok';
            const failed = data.success === false || data.code === -1 || data.status === 'failed' || data.status === 'error';

            if (success) {
                return {
                    type: 'success',
                    text: extractSnippet(message) || '签到成功',
                };
            }

            if (failed) {
                return {
                    type: 'fail',
                    text: extractSnippet(message) || '签到失败',
                };
            }

            if (/已签到/i.test(message)) {
                return { type: 'success', text: extractSnippet(message) || '已签到' };
            }
        } catch {}
        return null;
    };

    const classifyPayload = (text, url = '') => {
        if (!text || typeof text !== 'string') return null;
        const payload = text.slice(0, 4000);
        const signUrl = /check.?in|sign.?in|attendance|daily|reward|credit|quota/i.test(url || '');
        const parsedJson = parseJsonPayload(payload, url);
        if (parsedJson) return parsedJson;
        const snippet = extractSnippet(payload);
        const combined = `${url || ''} ${snippet}`;
        if (/已签到/i.test(combined)) {
            return { type: 'success', text: snippet || '已签到' };
        }
        if (!(signalRegex.test(combined) || signUrl)) return null;
        if (failRegex.test(combined)) {
            return { type: 'fail', text: snippet || normalize(url) || '签到失败' };
        }
        if (successRegex.test(combined)) {
            return { type: 'success', text: snippet || normalize(url) || '签到成功' };
        }
        if (signUrl && /"success"\s*:\s*true|"code"\s*:\s*0|"status"\s*:\s*"success"/i.test(payload)) {
            return { type: 'success', text: snippet || `接口返回成功: ${normalize(url)}` };
        }
        return null;
    };

    const remember = (match, source, element) => {
        if (!match) return;
        const keyValue = `${match.type}:${match.text}`;
        if (seen.has(keyValue)) return;
        seen.add(keyValue);
        let className = '';
        let tagName = '';
        if (element instanceof Element) {
            try {
                tagName = element.tagName ? element.tagName.toLowerCase() : '';
                const raw = typeof element.className === 'string'
                    ? element.className
                    : (element.className && element.className.baseVal) || '';
                className = String(raw).slice(0, 200);
            } catch {}
        }
        const event = {
            key: keyValue,
            type: match.type,
            text: match.text,
            source,
            tagName,
            className,
            ts: Date.now(),
        };
        state.events.push(event);
        state.events = state.events.slice(-20);
        state.updatedAt = event.ts;
        state.lastStrongType = match.type;
        state.lastStrongText = match.text;
        persist();
    };

    const rememberNetwork = (match, source, url) => {
        if (!match) return;
        remember(match, source);
        const keyValue = `${match.type}:${normalize(url)}:${match.text}`;
        if (seenNetwork.has(keyValue)) return;
        seenNetwork.add(keyValue);
        state.network.push({
            key: keyValue,
            type: match.type,
            text: match.text,
            url: normalize(url).slice(0, 160),
            source,
            ts: Date.now(),
        });
        state.network = state.network.slice(-10);
        persist();
    };

    const candidateElements = (root) => {
        const candidates = [];
        if (!(root instanceof Element)) return candidates;
        if (root.matches && selectors.some((selector) => root.matches(selector))) {
            candidates.push(root);
        }
        if (root.querySelectorAll) {
            for (const selector of selectors) {
                for (const node of root.querySelectorAll(selector)) {
                    candidates.push(node);
                }
            }
        }
        return candidates;
    };

    const inspect = (node, source) => {
        let root = node;
        if (root && root.nodeType === Node.TEXT_NODE) {
            root = root.parentElement;
        }
        if (!(root instanceof Element)) return;
        const visited = new Set();
        for (const element of candidateElements(root)) {
            if (!(element instanceof Element) || visited.has(element)) continue;
            visited.add(element);
            const match = classify(element.innerText || element.textContent || '');
            if (match) {
                remember(match, source, element);
            }
        }
        const fallbackInspect = (el) => {
            if (!(el instanceof Element) || visited.has(el)) return;
            visited.add(el);
            const text = el.innerText || el.textContent || '';
            if (!text || text.length > 1000) return;
            if (!signalRegex.test(text)) return;
            const match = classify(text);
            if (match) {
                remember(match, source + ':fallback', el);
            }
        };
        fallbackInspect(root);
        try {
            let count = 0;
            for (const el of root.querySelectorAll('*')) {
                if (count++ > 300) break;
                fallbackInspect(el);
            }
        } catch {}
    };

    const scanExistingSignals = () => {
        if (!document.documentElement) return;
        for (const selector of selectors) {
            for (const element of document.querySelectorAll(selector)) {
                inspect(element, 'scan');
            }
        }
    };

    const observer = new MutationObserver((mutations) => {
        for (const mutation of mutations) {
            if (mutation.type === 'childList') {
                for (const addedNode of mutation.addedNodes) {
                    inspect(addedNode, 'childList');
                }
            } else if (mutation.type === 'characterData') {
                inspect(mutation.target, 'characterData');
            }
        }
    });

    const wrapFetch = () => {
        if (typeof window.fetch !== 'function' || window.fetch.__checkinWrapped) return;
        const originalFetch = window.fetch.bind(window);
        const wrappedFetch = async (...args) => {
            const response = await originalFetch(...args);
            try {
                const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || response.url || '';
                const contentType = response.headers.get('content-type') || '';
                if (/json|text|javascript/i.test(contentType) || /check.?in|sign.?in|attendance|daily|reward|credit|quota/i.test(url)) {
                    response.clone().text().then((text) => {
                        const match = classifyPayload(text, url);
                        if (match) {
                            rememberNetwork(match, 'fetch', url);
                        }
                    }).catch(() => {});
                }
            } catch {}
            return response;
        };
        wrappedFetch.__checkinWrapped = true;
        window.fetch = wrappedFetch;
    };

    const wrapXHR = () => {
        if (window.XMLHttpRequest && window.XMLHttpRequest.prototype.__checkinWrapped) return;
        const proto = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
        if (!proto) return;
        const originalOpen = proto.open;
        const originalSend = proto.send;
        proto.open = function(method, url, ...rest) {
            this.__checkinUrl = url || '';
            return originalOpen.call(this, method, url, ...rest);
        };
        proto.send = function(...args) {
            this.addEventListener('loadend', () => {
                try {
                    const match = classifyPayload(this.responseText || '', this.__checkinUrl || '');
                    if (match) {
                        rememberNetwork(match, 'xhr', this.__checkinUrl || '');
                    }
                } catch {}
            }, { once: true });
            return originalSend.apply(this, args);
        };
        proto.__checkinWrapped = true;
    };

    try {
        observer.observe(document.documentElement, {
            subtree: true,
            childList: true,
            characterData: true,
        });
    } catch {}

    scanExistingSignals();
    wrapFetch();
    wrapXHR();
    window.addEventListener('beforeunload', persist);
    document.addEventListener('DOMContentLoaded', scanExistingSignals, { once: true });
    window.addEventListener('load', () => setTimeout(scanExistingSignals, 0), { once: true });
    persist();
})();
"""

READ_CHECKIN_MONITOR_JS = r"""
(() => {
    const state = window.__checkinSignal || {};
    return {
        lastStrongType: state.lastStrongType || null,
        lastStrongText: state.lastStrongText || '',
        updatedAt: state.updatedAt || null,
        events: Array.isArray(state.events) ? state.events.slice(-5) : [],
        network: Array.isArray(state.network) ? state.network.slice(-5) : [],
    };
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


def extract_username(page, log_detect=False):
    """Extract username from page (primarily via localStorage)."""
    try:
        result = page.evaluate(EXTRACT_USERNAME_JS)
        if result and isinstance(result, str):
            name = result.strip()
            name = "".join(c for c in name if c not in r'\/:*?"<>|')
            if name and _safe_name(name):
                if log_detect:
                    log.info(f"  Username detected: {name}")
                return name
    except Exception:
        pass
    return None


def prepare_checkin_monitor(ctx):
    """Inject the check-in monitor before page scripts run."""
    try:
        run_id = f"checkin_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        ctx.add_init_script(f"window.__checkinMonitorRunId = {json.dumps(run_id)};")
        ctx.add_init_script(CHECKIN_MONITOR_INIT_JS)
    except Exception:
        pass


def read_checkin_monitor(page):
    try:
        result = page.evaluate(READ_CHECKIN_MONITOR_JS)
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    return {}


def attach_response_recorder(ctx, max_bodies=30):
    """Diagnostic listener: collect responses during check-in.

    Records URL/status/content-type for every response; reads body (up to
    1500 chars) only for URLs matching `_DIAG_URL_RE` or JSON content-types,
    capped at `max_bodies` reads to avoid pulling huge payloads.
    """
    records = []
    state = {"body_count": 0}
    lock = threading.Lock()

    def on_response(response):
        try:
            url = response.url or ""
            status = response.status
            try:
                method = response.request.method
            except Exception:
                method = ""
            try:
                ct = (response.headers or {}).get("content-type", "")
            except Exception:
                ct = ""
            entry = {
                "method": method,
                "status": status,
                "url": url[:240],
                "ct": ct[:80],
            }
            want_body = (
                (bool(_DIAG_URL_RE.search(url)) or "json" in ct.lower())
                and status < 400
            )
            if want_body:
                with lock:
                    can_read = state["body_count"] < max_bodies
                    if can_read:
                        state["body_count"] += 1
                if can_read:
                    try:
                        body = response.text()
                        if body:
                            entry["body"] = body[:1500]
                    except Exception as e:
                        entry["body_err"] = str(e)[:80]
            with lock:
                records.append(entry)
        except Exception as e:
            with lock:
                records.append({"err": str(e)[:120]})

    ctx.on("response", on_response)
    return records


def dump_response_records(name, records, limit=50):
    if not records:
        log.info(f"[{name}] diag: 未捕获任何 response 事件")
        return
    with_body = [r for r in records if "body" in r]
    log.info(
        f"[{name}] diag: 共 {len(records)} 个 response，其中 {len(with_body)} 条带响应体，"
        f"显示最近 {min(limit, len(records))} 条"
    )
    for r in records[-limit:]:
        try:
            line = json.dumps(r, ensure_ascii=False)
        except Exception:
            line = str(r)
        log.info(f"  diag → {line[:1800]}")


def attach_signin_capture(ctx):
    """Capture POST /api/user/sign_in responses (authoritative check-in signal).

    Returns a dict updated in-place by the response listener. We keep the latest
    successful read because the very first sign_in response is often unreadable
    ("Network.getResponseBody: No resource with given") — the page tends to
    re-fire the request, and a later read succeeds.
    """
    state = {"captured": False, "success": None, "message": "", "status": None}
    lock = threading.Lock()

    def on_response(response):
        try:
            url = response.url or ""
            if not SIGNIN_URL_RE.search(url):
                return
            req = response.request
            if req and req.method and req.method.upper() != "POST":
                return
            try:
                data = response.json()
            except Exception:
                return
            if not isinstance(data, dict):
                return
            with lock:
                state["captured"] = True
                state["status"] = response.status
                state["success"] = bool(data.get("success"))
                msg = data.get("message")
                state["message"] = msg if isinstance(msg, str) else ""
        except Exception:
            pass

    ctx.on("response", on_response)
    return state


def _append_balance_message(message, balance):
    if balance:
        return f"{message} (余额: {balance})"
    return message


def _build_uncertain_checkin_message(wait_seconds, username=None, balance=None):
    if username:
        return f"已登录: {username} (会话有效，但{wait_seconds}秒内未捕获签到结果，请手动确认)"
    if balance:
        return f"已进入控制台，但{wait_seconds}秒内未捕获签到结果，请手动确认"
    return f"{wait_seconds}秒内未捕获签到结果，请手动确认"


def _parse_balance_value(balance):
    if not balance:
        return None
    try:
        return float(balance.replace("$", "").replace(",", ""))
    except (AttributeError, ValueError):
        return None


def detect_checkin_status(page, url):
    """Inspect the current page state without waiting indefinitely."""
    # Check 1: Is this a login page? (session expired)
    try:
        if page.evaluate(CHECK_LOGIN_PAGE_JS):
            return {"state": "failed", "message": "会话已过期，被重定向到登录页"}
    except Exception:
        pass

    # Check 2: Any short-lived signal captured by the monitor?
    monitor = read_checkin_monitor(page)
    if monitor.get("lastStrongType") == "success":
        return {"state": "success", "message": monitor.get("lastStrongText", "签到成功"), "source": "monitor"}
    if monitor.get("lastStrongType") == "fail":
        return {"state": "failed", "message": monitor.get("lastStrongText", "签到失败"), "source": "monitor"}

    # Check 3: Detect current toast/notification state
    try:
        result = page.evaluate(DETECT_TOAST_JS)
        if result and result.get("found"):
            text = result["text"]
            if result["type"] == "success":
                return {"state": "success", "message": text, "source": "current-dom"}
            elif result["type"] == "fail":
                return {"state": "failed", "message": text, "source": "current-dom"}
            else:
                return {"state": "signal", "message": f"检测到提示: {text}", "source": "current-dom"}
    except Exception:
        pass

    # Check 4: Logged in means session is valid, but not a final success signal.
    username = extract_username(page, log_detect=False)
    if username:
        return {"state": "logged_in", "message": f"已登录: {username}", "username": username}

    # Check 5: Fallback - page loaded but no login info
    try:
        title = page.title()
        body_length = page.evaluate("document.body?.innerText?.length || 0")
        if body_length > 100:
            return {"state": "loaded", "message": f"页面已加载，但尚未确认签到结果 ({title})"}
    except Exception:
        pass

    return {"state": "unknown", "message": "无法确认签到状态"}


def verify_checkin(page, url):
    """Single-shot check-in verification without treating login as final success."""
    status = detect_checkin_status(page, url)
    return status["state"] == "success", status["message"]


def wait_for_checkin_result(page, url, prev_balance=None,
                            signin_state=None, account_name=None,
                            wait_timeout_ms=CHECKIN_RESULT_WAIT_MS,
                            poll_interval_ms=CHECKIN_RESULT_POLL_MS):
    """Wait a bounded amount of time for a real check-in signal.

    Priority order:
      1. `signin_state` from `attach_signin_capture` — authoritative API response
      2. Page-side monitor / DOM toast detection (legacy fallback)
      3. Balance delta after timeout (heuristic fallback)
      4. History check — if today already has a success entry and session is
         valid, treat as "already checked in today" instead of uncertain.
    """
    try:
        page.evaluate(CHECKIN_MONITOR_INIT_JS)
    except Exception:
        pass

    deadline = time.monotonic() + (wait_timeout_ms / 1000)
    last_status = {"state": "unknown", "message": "无法确认签到状态"}
    username = None
    wait_seconds = max(1, round(wait_timeout_ms / 1000))

    def _format_success(msg_text, balance_now):
        delta_text = ""
        if balance_now and prev_balance:
            new = _parse_balance_value(balance_now)
            old = _parse_balance_value(prev_balance)
            if new is not None and old is not None and new > old:
                delta_text = f"，本次增加 ${new - old:.2f}"
        base = f"签到成功{delta_text}"
        if msg_text:
            base = f"{base} - {msg_text}"
        return base

    while time.monotonic() < deadline:
        if signin_state and signin_state.get("captured"):
            balance = extract_balance(page)
            msg_text = (signin_state.get("message") or "").strip()
            if signin_state.get("success"):
                msg = _format_success(msg_text, balance)
                return "success", _append_balance_message(msg, balance), balance
            lower_text = msg_text.lower()
            if any(token in msg_text or token in lower_text for token in SIGNIN_ALREADY_TOKENS):
                msg = f"今日已签到{f' - {msg_text}' if msg_text else ''}"
                return "success", _append_balance_message(msg, balance), balance
            msg = f"签到失败{f': {msg_text}' if msg_text else ''}"
            return "failed", _append_balance_message(msg, balance), balance

        status = detect_checkin_status(page, url)
        last_status = status

        if status["state"] == "success":
            balance = extract_balance(page)
            return "success", _append_balance_message(status["message"], balance), balance

        if status["state"] == "failed":
            balance = extract_balance(page)
            return "failed", _append_balance_message(status["message"], balance), balance

        if status["state"] == "logged_in":
            username = status.get("username") or username

        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        page.wait_for_timeout(min(poll_interval_ms, remaining_ms))

    balance = extract_balance(page)
    username = username or extract_username(page, log_detect=False)

    try:
        monitor_dump = read_checkin_monitor(page)
        events = monitor_dump.get("events") or []
        network = monitor_dump.get("network") or []
        if events or network:
            log.info(f"  monitor events ({len(events)}): {json.dumps(events, ensure_ascii=False)[:1500]}")
            log.info(f"  monitor network ({len(network)}): {json.dumps(network, ensure_ascii=False)[:1500]}")
        else:
            log.info("  monitor 未捕获任何事件 (events/network 均为空)")
    except Exception as _e:
        log.info(f"  monitor dump 失败: {_e}")

    if balance and prev_balance:
        new_val = _parse_balance_value(balance)
        old_val = _parse_balance_value(prev_balance)
        if new_val is not None and old_val is not None and new_val > old_val:
            gained = new_val - old_val
            display_name = username or extract_username(page, log_detect=False) or "当前账号"
            message = f"签到成功，本次增加 ${gained:.2f} (账号: {display_name})"
            return "success", _append_balance_message(message, balance), balance

    # If session is valid and today already has a success entry in history,
    # anyrouter no longer fires sign_in on re-visit and shows no banner —
    # treat as "already checked in" instead of uncertain.
    if username and account_name and checked_today(account_name):
        message = f"今日已签到 (会话有效，无新签到信号)"
        return "success", _append_balance_message(message, balance), balance

    if username:
        message = _build_uncertain_checkin_message(wait_seconds, username=username, balance=balance)
        return "uncertain", _append_balance_message(message, balance), balance

    if balance:
        message = _build_uncertain_checkin_message(wait_seconds, balance=balance)
        return "uncertain", _append_balance_message(message, balance), balance

    return "failed", _append_balance_message(last_status["message"], balance), balance


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


# ── Claude Code health probe ───────────────────────────────
# Verify an account can actually relay a Claude Code request. anyrouter is an
# official Claude relay and only forwards requests that look like they come from
# Claude Code, so the probe must carry Claude Code's signature: the CLI
# User-Agent, the claude-code anthropic-beta header, and a system prompt whose
# first line identifies the caller as Claude Code. max_tokens is kept tiny so a
# probe costs very little even on Opus.

CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
HEALTH_MODEL = "claude-opus-4-8"
HEALTH_MAX_TOKENS = 16
HEALTH_TIMEOUT = 30
# Captured from a real claude-cli/2.1.187 request so the probe is byte-for-byte
# indistinguishable from Claude Code.
HEALTH_USER_AGENT = "claude-cli/2.1.187 (external, sdk-cli)"
# anyrouter requires every request to opt into the 1M context window via this
# beta flag — without it the relay rejects with HTTP 400 "请启用 1m 上下文",
# even for Sonnet. It is a site-wide policy, not an Opus-specific one.
HEALTH_ANTHROPIC_BETA = (
    "claude-code-20250219,context-1m-2025-08-07,interleaved-thinking-2025-05-14,"
    "thinking-token-count-2026-05-13,context-management-2025-06-27,"
    "prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04-07,"
    "advanced-tool-use-2025-11-20,effort-2025-11-24"
)
# Anthropic SDK (Stainless-generated) fingerprint headers real Claude Code sends.
# Static values from the captured request — anyrouter doesn't check they match
# the probe host's actual platform.
HEALTH_STAINLESS_HEADERS = {
    "x-stainless-arch": "arm64",
    "x-stainless-lang": "js",
    "x-stainless-os": "MacOS",
    "x-stainless-package-version": "0.94.0",
    "x-stainless-retry-count": "0",
    "x-stainless-runtime": "node",
    "x-stainless-runtime-version": "v24.3.0",
    "x-stainless-timeout": "300",
}
_QUOTA_TOKENS = ("insufficient", "quota", "credit", "余额", "额度", "欠费", "充值", "余量")
# Transient categories worth retrying, and the backoff (seconds) before each
# retry. anyrouter's Opus upstream returns 503 in bursts, so a short retry often
# turns a false "unavailable" into a real verdict. 3 attempts total.
HEALTH_RETRY_CATEGORIES = ("server", "rate_limit", "network")
HEALTH_RETRY_BACKOFF = (2, 4)


def _mask_token(token):
    """Show only the head and tail of an API token for safe display."""
    if not token or not isinstance(token, str):
        return ""
    token = token.strip()
    if len(token) <= 10:
        return "sk-****"
    return f"{token[:6]}****{token[-4:]}"


def _probe_suffix(msg):
    msg = (msg or "").strip()
    return f": {msg[:120]}" if msg else ""


def _classify_probe_response(status, body):
    """Map an HTTP status + body from /v1/messages into a health verdict."""
    data = None
    try:
        data = json.loads(body)
    except Exception:
        pass

    err_msg = ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            err_msg = err.get("message") or ""
        elif isinstance(err, str):
            err_msg = err
        if not err_msg and isinstance(data.get("message"), str):
            err_msg = data["message"]
    if not err_msg and body:
        err_msg = body.strip()[:200]
    low = err_msg.lower()

    # Success: a real assistant message came back.
    if status == 200 and isinstance(data, dict) and data.get("type") == "message":
        text = ""
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")
        reply = " ".join(text.split())[:60] or "(空回复)"
        return {"ok": True, "category": "ok", "message": f"可用 - 模型回复「{reply}」"}

    # Balance/quota first — its keywords are more specific than status alone.
    if status == 402 or any(tok in low for tok in _QUOTA_TOKENS):
        return {"ok": False, "category": "quota", "message": f"余额不足 / 额度用尽{_probe_suffix(err_msg)}"}

    if status in (401, 403):
        return {"ok": False, "category": "auth", "message": f"令牌无效或已禁用{_probe_suffix(err_msg)}"}

    if status == 429:
        return {"ok": False, "category": "rate_limit", "message": f"触发限流，稍后重试{_probe_suffix(err_msg)}"}

    if status in (400, 404) and ("model" in low or "模型" in low):
        return {"ok": False, "category": "model", "message": f"令牌可用，但测试模型不可用{_probe_suffix(err_msg)}"}

    if status >= 500:
        return {"ok": False, "category": "server", "message": f"中转服务端错误 ({status}){_probe_suffix(err_msg)}"}

    if status == 200:
        return {"ok": False, "category": "unknown", "message": f"返回 200 但响应格式异常{_probe_suffix(err_msg)}"}

    return {"ok": False, "category": "unknown", "message": f"HTTP {status}{_probe_suffix(err_msg)}"}


def _probe_once(endpoint, data, headers):
    """One request/response round-trip to /v1/messages, already classified."""
    req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return {"ok": False, "category": "network", "message": f"无法连接: {str(reason)[:120]}"}
    except Exception as e:
        return {"ok": False, "category": "network", "message": f"请求异常: {str(e)[:120]}"}
    return _classify_probe_response(status, body)


def _run_claude_probe(token, base_url, model=HEALTH_MODEL):
    """Send a Claude Code-shaped request to {base_url}/v1/messages.

    Retries transient failures (503 upstream, 429 rate-limit, network blips)
    with increasing backoff; deterministic verdicts (auth / quota / model / bad
    request) return immediately since retrying them is pointless.
    """
    endpoint = base_url.rstrip("/") + "/v1/messages?beta=true"
    payload = {
        "model": model,
        "max_tokens": HEALTH_MAX_TOKENS,
        "system": [
            {"type": "text", "text": CLAUDE_CODE_SYSTEM,
             "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "accept": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": HEALTH_ANTHROPIC_BETA,
        "anthropic-dangerous-direct-browser-access": "true",
        "authorization": f"Bearer {token}",
        "x-app": "cli",
        "user-agent": HEALTH_USER_AGENT,
        "x-claude-code-session-id": str(uuid.uuid4()),
        **HEALTH_STAINLESS_HEADERS,
    }

    result = None
    for attempt in range(len(HEALTH_RETRY_BACKOFF) + 1):
        result = _probe_once(endpoint, data, headers)
        if result.get("ok") or result.get("category") not in HEALTH_RETRY_CATEGORIES:
            return result
        if attempt < len(HEALTH_RETRY_BACKOFF):
            time.sleep(HEALTH_RETRY_BACKOFF[attempt])
    return result


# ── Core operations ────────────────────────────────────────

def _wait_for_browser_close(ctx, page=None):
    """Wait until the persistent browser window is closed by the user."""
    while True:
        try:
            pages = ctx.pages
            if not pages:
                break
            current_page = page if page in pages else pages[0]
            current_page.wait_for_timeout(1000)
        except Exception:
            break


def open_saved_session_browser(name, url):
    """Open a visible browser for manual inspection using the saved session."""
    if not has_session(name):
        raise ValueError("未保存会话")

    profile_dir = get_profile_dir(name)
    console_url = url.rstrip("/") + "/console"
    opened_url = console_url
    balance = None

    with sync_playwright() as p:
        ctx = _launch_ctx(p, profile_dir, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        try:
            page.goto(console_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            opened_url = url
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        try:
            page.wait_for_timeout(1500)
            balance = extract_balance(page)
        except Exception:
            pass

        _wait_for_browser_close(ctx, page)

        try:
            ctx.close()
        except Exception:
            pass

    return {"opened_url": opened_url, "balance": balance}


def do_checkin(name, url, headless=True, timeout=30000):
    busy_error = get_account_busy_error(name)
    if busy_error:
        log.info(f"[{name}] 跳过签到 - {busy_error}")
        return

    set_status(name, "checking")

    if not has_session(name):
        set_status(name, "no_session", "未保存会话")
        append_history({"account": name, "time": datetime.now().isoformat(), "status": "skipped", "message": "未保存会话"})
        return

    # Get previous balance for comparison
    prev_balance = None
    with _lock:
        prev = _account_status.get(name, {})
        prev_balance = prev.get("balance")
    if not prev_balance:
        last = _find_last_history(name)
        if last:
            prev_balance = last.get("balance")

    profile_dir = get_profile_dir(name)
    balance = None
    try:
        with sync_playwright() as p:
            ctx = _launch_ctx(p, profile_dir, headless=headless)
            prepare_checkin_monitor(ctx)
            signin_state = attach_signin_capture(ctx)
            diag_records = attach_response_recorder(ctx) if CHECKIN_DIAG_LOG else None
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_load_state("networkidle", timeout=timeout)

            result_state, message, balance = wait_for_checkin_result(
                page, url, prev_balance=prev_balance,
                signin_state=signin_state, account_name=name,
            )

            if diag_records is not None:
                dump_response_records(name, diag_records)

            ctx.close()

        if result_state == "success":
            set_status(name, "success", message, balance=balance)
            append_history({"account": name, "time": datetime.now().isoformat(), "status": "success", "message": message, "balance": balance})
            log.info(f"[{name}] 签到成功 - {message}")
        elif result_state == "uncertain":
            set_status(name, "uncertain", message, balance=balance)
            append_history({"account": name, "time": datetime.now().isoformat(), "status": "uncertain", "message": message, "balance": balance})
            log.warning(f"[{name}] 签到待确认 - {message}")
        else:
            set_status(name, "failed", message, balance=balance)
            append_history({"account": name, "time": datetime.now().isoformat(), "status": "failed", "message": message, "balance": balance})
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
        if current.get("state") in ACTIVE_ACCOUNT_STATES:
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


def do_check_health(name, url, token):
    """Probe whether an account can relay a real Claude Code request."""
    if not token:
        set_health(name, "failed", "未设置 API 令牌", category="no_token")
        return
    set_health(name, "checking", "检测中")
    try:
        result = _run_claude_probe(token, url or DEFAULT_URL)
    except Exception as e:
        result = {"ok": False, "category": "network", "message": f"请求异常: {str(e)[:120]}"}
    state = "ok" if result.get("ok") else "failed"
    set_health(name, state, result.get("message", ""), category=result.get("category"))
    if result.get("ok"):
        log.info(f"[{name}] 可用性检测通过 - {result.get('message', '')}")
    else:
        log.warning(f"[{name}] 可用性检测失败 - {result.get('message', '')}")


def do_open_browser(name, url):
    """Open a visible browser for manual inspection."""
    if not has_session(name):
        set_status(name, "no_session", "未保存会话")
        return

    set_status(name, "inspecting", "浏览器已打开，可手动检查 key 和签到状态")
    balance = None

    try:
        result = open_saved_session_browser(name, url)
        balance = result.get("balance")
        set_status(name, "idle", balance=balance)
        log.info(f"[{name}] 手动查看结束")
    except Exception as e:
        msg = str(e)[:200]
        set_status(name, "inspect_failed", msg, balance=balance)
        log.error(f"[{name}] 打开浏览器失败 - {msg}")


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
                name = extract_username(pg, log_detect=True)
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
                        detected_name = extract_username(pg, log_detect=True)
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


@app.route("/api/auth/status")
def auth_status():
    config = load_config()
    return jsonify({
        "authed": bool(session.get("authed")),
        "hasPassword": bool(config.get("password")),
    })


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    config = load_config()
    pw = (request.json or {}).get("password", "")
    stored = config.get("password", "")
    if not stored:
        return jsonify({"error": "No password set"}), 400
    if check_password_hash(stored, pw):
        session["authed"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "密码错误"}), 403


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/setup", methods=["POST"])
def auth_setup():
    config = load_config()
    if config.get("password"):
        return jsonify({"error": "Password already set"}), 400
    pw = (request.json or {}).get("password", "")
    if not pw or len(pw) < 4:
        return jsonify({"error": "密码至少4位"}), 400
    config["password"] = generate_password_hash(pw)
    save_config(config)
    session["authed"] = True
    return jsonify({"ok": True})


@app.route("/api/auth/change-password", methods=["POST"])
def auth_change_password():
    if not session.get("authed"):
        return jsonify({"error": "Unauthorized"}), 401
    config = load_config()
    data = request.json or {}
    old_pw = data.get("oldPassword", "")
    new_pw = data.get("newPassword", "")
    stored = config.get("password", "")
    if stored and not check_password_hash(stored, old_pw):
        return jsonify({"error": "原密码错误"}), 403
    if not new_pw or len(new_pw) < 4:
        return jsonify({"error": "新密码至少4位"}), 400
    config["password"] = generate_password_hash(new_pw)
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/accounts")
@require_auth
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
            "note": a.get("note", ""),
            "hasSession": has_session(n),
            "hasToken": bool(a.get("token")),
            "tokenMask": _mask_token(a.get("token")),
            "health": _account_health.get(n),
            "state": s.get("state", "idle"),
            "message": s.get("message", ""),
            "balance": s.get("balance") or (last.get("balance") if last else None),
            "checkedToday": checked_today(n),
            "lastCheckin": last["time"] if last else None,
            "lastStatus": last["status"] if last else None,
        })
    return jsonify(result)


@app.route("/api/accounts", methods=["POST"])
@require_auth
def add_account():
    data = request.json
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    if not _safe_name(name):
        return jsonify({"error": "账号名包含非法字符"}), 400
    config = load_config()
    if any(a["name"] == name for a in config["accounts"]):
        return jsonify({"error": "Already exists"}), 409
    config["accounts"].append({"name": name, "url": data.get("url", DEFAULT_URL)})
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/accounts/<name>", methods=["DELETE"])
@require_auth
def remove_account(name):
    if not _safe_name(name):
        return jsonify({"error": "账号名包含非法字符"}), 400
    config = load_config()
    config["accounts"] = [a for a in config["accounts"] if a["name"] != name]
    save_config(config)
    d = PROFILES_DIR / name
    if d.exists():
        shutil.rmtree(d)
    with _lock:
        _account_status.pop(name, None)
    return jsonify({"ok": True})


@app.route("/api/accounts/<name>", methods=["PATCH"])
@require_auth
def patch_account(name):
    """Rename an account and/or update its note.

    Body: {"name": "new_name"?, "note": "new note"?}. Both fields optional;
    pass only what you want to change. Renaming moves the profile directory
    and migrates the in-memory status key, so the browser session and
    balance state survive the rename. History entries keep their original
    account name — they are an immutable log of what happened.
    """
    if not _safe_name(name):
        return jsonify({"error": "账号名包含非法字符"}), 400
    busy = get_account_busy_error(name)
    if busy:
        return jsonify({"error": busy}), 409

    data = request.json or {}
    new_name_raw = data.get("name")
    new_name = new_name_raw.strip() if isinstance(new_name_raw, str) else None
    new_note = data.get("note")
    if new_note is not None:
        new_note = str(new_note).strip()[:200]
    new_token = data.get("token")

    config = load_config()
    idx = next((i for i, a in enumerate(config["accounts"]) if a["name"] == name), -1)
    if idx < 0:
        return jsonify({"error": "Not found"}), 404

    current = name
    if new_name and new_name != name:
        if not _safe_name(new_name):
            return jsonify({"error": "新账号名包含非法字符"}), 400
        if any(a["name"] == new_name for a in config["accounts"]):
            return jsonify({"error": "新账号名已存在"}), 409
        src = PROFILES_DIR / name
        dst = PROFILES_DIR / new_name
        if dst.exists():
            return jsonify({"error": "目标会话目录已存在，请先移除"}), 409
        if src.exists():
            src.rename(dst)
        config["accounts"][idx]["name"] = new_name
        with _lock:
            if name in _account_status:
                _account_status[new_name] = _account_status.pop(name)
        current = new_name

    if new_note is not None:
        entry = config["accounts"][idx]
        if new_note:
            entry["note"] = new_note
        else:
            entry.pop("note", None)

    if isinstance(new_token, str) and new_token.strip():
        config["accounts"][idx]["token"] = new_token.strip()

    save_config(config)
    return jsonify({"ok": True, "name": current})


@app.route("/api/login-new", methods=["POST"])
@require_auth
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
@require_auth
def login_account(name):
    """Re-login an existing account."""
    acc, err = _account_or_error(name, require_session=False)
    if err:
        return err
    threading.Thread(target=do_login, args=(name, acc["url"]), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/open-browser/<name>", methods=["POST"])
@require_auth
def open_browser_account(name):
    acc, err = _account_or_error(name)
    if err:
        return err
    url = acc.get("url", DEFAULT_URL)
    threading.Thread(target=do_open_browser, args=(name, url), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/checkin", methods=["POST"])
@require_auth
def checkin_all():
    global _busy
    with _lock:
        if _busy:
            return jsonify({"error": "Already running"}), 409
        _busy = True
    try:
        config = load_config()
        with _lock:
            busy_names = {
                name for name, status in _account_status.items()
                if status.get("state") in ACTIVE_ACCOUNT_STATES
            }
        acc_map = {
            a["name"]: a for a in config["accounts"]
            if has_session(a["name"]) and a["name"] not in busy_names
        }
        if not acc_map:
            with _lock:
                _busy = False
            return jsonify({"error": "没有可签到的账号（请先登录或关闭正在使用的浏览器）"}), 400
        headless = config.get("headless", True)
        timeout = config.get("timeout", DEFAULT_TIMEOUT) * 1000

        # Frontend can send preferred processing order
        data = request.get_json(silent=True) or {}
        order = data.get("order")
        if order and isinstance(order, list):
            accounts = [acc_map[n] for n in order if n in acc_map]
            seen = {a["name"] for a in accounts}
            accounts += [a for a in acc_map.values() if a["name"] not in seen]
        else:
            accounts = list(acc_map.values())
    except Exception:
        with _lock:
            _busy = False
        raise

    def run():
        global _busy
        try:
            for a in accounts:
                do_checkin(a["name"], a.get("url", DEFAULT_URL), headless, timeout)
        finally:
            with _lock:
                _busy = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "count": len(accounts)})


@app.route("/api/checkin/<name>", methods=["POST"])
@require_auth
def checkin_one(name):
    acc, err = _account_or_error(name)
    if err:
        return err
    config = load_config()
    url = acc.get("url", DEFAULT_URL)
    headless = config.get("headless", True)
    timeout = config.get("timeout", DEFAULT_TIMEOUT) * 1000
    threading.Thread(target=do_checkin, args=(name, url, headless, timeout), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/refresh-balance", methods=["POST"])
@require_auth
def refresh_balance_all():
    global _refreshing
    with _lock:
        if _refreshing:
            return jsonify({"error": "Already refreshing"}), 409
        _refreshing = True
    try:
        config = load_config()
        with _lock:
            busy_names = {
                name for name, status in _account_status.items()
                if status.get("state") in ACTIVE_ACCOUNT_STATES
            }
        acc_map = {
            a["name"]: a for a in config["accounts"]
            if has_session(a["name"]) and a["name"] not in busy_names
        }
        if not acc_map:
            with _lock:
                _refreshing = False
            return jsonify({"error": "没有可刷新的账号（请先登录或关闭正在使用的浏览器）"}), 400

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
    except Exception:
        with _lock:
            _refreshing = False
        raise

    def run():
        global _refreshing
        try:
            for a in ordered:
                do_refresh_balance(a["name"], a.get("url", DEFAULT_URL))
        finally:
            with _lock:
                _refreshing = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "count": len(ordered)})


@app.route("/api/refresh-balance/<name>", methods=["POST"])
@require_auth
def refresh_balance_one(name):
    acc, err = _account_or_error(name)
    if err:
        return err
    url = acc.get("url", DEFAULT_URL)
    threading.Thread(target=do_refresh_balance, args=(name, url), daemon=True).start()
    return jsonify({"ok": True})


def _health_account_or_error(name):
    """Pre-flight for health-probe endpoints: needs a token, not a session."""
    if not _safe_name(name):
        return None, (jsonify({"error": "账号名包含非法字符"}), 400)
    config = load_config()
    acc = next((a for a in config["accounts"] if a["name"] == name), None)
    if not acc:
        return None, (jsonify({"error": "Not found"}), 404)
    if not acc.get("token"):
        return None, (jsonify({"error": "请先为该账号填写 API 令牌"}), 400)
    h = _account_health.get(name)
    if h and h.get("state") == "checking":
        return None, (jsonify({"error": "该账号正在检测中"}), 409)
    return acc, None


@app.route("/api/check-health/<name>", methods=["POST"])
@require_auth
def check_health_one(name):
    acc, err = _health_account_or_error(name)
    if err:
        return err
    threading.Thread(
        target=do_check_health,
        args=(name, acc.get("url", DEFAULT_URL), acc.get("token")),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.route("/api/check-health", methods=["POST"])
@require_auth
def check_health_all():
    global _health_busy
    with _lock:
        if _health_busy:
            return jsonify({"error": "Already running"}), 409
        _health_busy = True
    try:
        config = load_config()
        acc_map = {a["name"]: a for a in config["accounts"] if a.get("token")}
        if not acc_map:
            with _lock:
                _health_busy = False
            return jsonify({"error": "没有已填写 API 令牌的账号"}), 400

        data = request.get_json(silent=True) or {}
        order = data.get("order")
        if order and isinstance(order, list):
            accounts = [acc_map[n] for n in order if n in acc_map]
            seen = {a["name"] for a in accounts}
            accounts += [a for a in acc_map.values() if a["name"] not in seen]
        else:
            accounts = list(acc_map.values())
    except Exception:
        with _lock:
            _health_busy = False
        raise

    def run():
        global _health_busy
        try:
            for a in accounts:
                do_check_health(a["name"], a.get("url", DEFAULT_URL), a.get("token"))
        finally:
            with _lock:
                _health_busy = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "count": len(accounts)})


@app.route("/api/history")
@require_auth
def get_history():
    return jsonify(load_history()[:100])


if __name__ == "__main__":
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
    print("\n  AnyRouter 签到助手")
    print("  http://localhost:8686\n")
    app.run(host="127.0.0.1", port=8686, debug=False)
