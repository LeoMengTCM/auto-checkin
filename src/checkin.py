#!/usr/bin/env python3
"""AnyRouter 自动签到工具

使用 Playwright 持久化浏览器上下文实现多账号自动签到。

用法:
    python src/checkin.py login <账号名>    # 手动登录并保存会话
    python src/checkin.py checkin [账号名]   # 自动签到（不指定则签到所有）
    python src/checkin.py open <账号名>     # 打开浏览器手动检查状态
    python src/checkin.py probe [账号名]    # 用 API 令牌检测账号可用性
    python src/checkin.py list              # 查看账号列表
    python src/checkin.py add <账号名>       # 添加新账号到配置
    python src/checkin.py remove <账号名>    # 从配置中移除账号
"""

import argparse
import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from patchright.sync_api import sync_playwright

# 路径配置
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config" / "accounts.json"
PROFILES_DIR = BASE_DIR / "data" / "profiles"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "checkin.log"

# 默认配置
DEFAULT_URL = "https://anyrouter.top"
DEFAULT_TIMEOUT = 30


def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


def load_config():
    if not CONFIG_FILE.exists():
        return {"accounts": [], "headless": True, "timeout": DEFAULT_TIMEOUT}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def get_profile_dir(account_name):
    profile_dir = PROFILES_DIR / account_name
    profile_dir.mkdir(parents=True, exist_ok=True)
    return str(profile_dir)


def has_session(account_name):
    """检查账号是否已有保存的浏览器会话"""
    profile_dir = PROFILES_DIR / account_name
    return profile_dir.exists() and any(profile_dir.iterdir())


def cmd_login(account_name):
    """打开浏览器让用户手动登录，保存会话"""
    from server import _launch_ctx

    config = load_config()

    # 如果账号不在配置中，自动添加
    names = [a["name"] for a in config["accounts"]]
    if account_name not in names:
        config["accounts"].append({"name": account_name, "url": DEFAULT_URL})
        save_config(config)
        logging.info(f"已自动添加账号 '{account_name}' 到配置")

    account = next(a for a in config["accounts"] if a["name"] == account_name)
    url = account.get("url", DEFAULT_URL)
    profile_dir = get_profile_dir(account_name)

    logging.info(f"正在为账号 '{account_name}' 打开浏览器...")
    logging.info("请在浏览器中手动登录，完成后关闭浏览器窗口即可保存会话。")

    with sync_playwright() as p:
        context = _launch_ctx(p, profile_dir, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded")

        # 等待用户手动操作，直到浏览器被关闭
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass

        try:
            context.close()
        except Exception:
            pass

    logging.info(f"账号 '{account_name}' 的会话已保存。")


def cmd_checkin(account_name=None):
    """自动签到"""
    from server import _launch_ctx, prepare_checkin_monitor, wait_for_checkin_result

    config = load_config()
    timeout = config.get("timeout", DEFAULT_TIMEOUT) * 1000  # 转为毫秒
    headless = config.get("headless", True)

    if account_name:
        accounts = [a for a in config["accounts"] if a["name"] == account_name]
        if not accounts:
            logging.error(f"账号 '{account_name}' 不在配置中。请先运行: python src/checkin.py login {account_name}")
            return
    else:
        accounts = config["accounts"]

    if not accounts:
        logging.warning("没有配置任何账号。请先运行: python src/checkin.py login <账号名>")
        return

    success_count = 0
    fail_count = 0

    logging.info(f"开始签到，共 {len(accounts)} 个账号...")

    with sync_playwright() as p:
        for account in accounts:
            name = account["name"]
            url = account.get("url", DEFAULT_URL)

            if not has_session(name):
                logging.warning(f"[{name}] 没有保存的会话，跳过。请先运行: python src/checkin.py login {name}")
                fail_count += 1
                continue

            profile_dir = get_profile_dir(name)
            logging.info(f"[{name}] 正在签到...")

            try:
                context = _launch_ctx(p, profile_dir, headless=headless)
                prepare_checkin_monitor(context)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout)

                # 等待页面完全加载（JS 挑战 + 自动签到）
                page.wait_for_load_state("networkidle", timeout=timeout)

                result_state, message, _balance = wait_for_checkin_result(page, url)

                context.close()
                if result_state == "success":
                    logging.info(f"[{name}] 签到成功 - {message}")
                    success_count += 1
                elif result_state == "uncertain":
                    logging.warning(f"[{name}] 签到待确认 - {message}")
                    fail_count += 1
                else:
                    logging.warning(f"[{name}] 签到失败 - {message}")
                    fail_count += 1
            except Exception as e:
                logging.error(f"[{name}] 签到失败: {e}")
                fail_count += 1
                try:
                    context.close()
                except Exception:
                    pass

    logging.info(f"签到完成: 成功 {success_count}, 失败 {fail_count}, 共 {len(accounts)}")


def cmd_open(account_name):
    """打开已保存会话的浏览器，手动检查 key 和签到状态。"""
    from server import open_saved_session_browser

    config = load_config()
    account = next((a for a in config["accounts"] if a["name"] == account_name), None)
    if not account:
        logging.error(f"账号 '{account_name}' 不在配置中。请先运行: python src/checkin.py login {account_name}")
        return

    if not has_session(account_name):
        logging.error(f"账号 '{account_name}' 没有保存的会话。请先运行: python src/checkin.py login {account_name}")
        return

    url = account.get("url", DEFAULT_URL)
    logging.info(f"正在为账号 '{account_name}' 打开浏览器...")
    logging.info("将直接打开控制台页面，请手动确认 key 状态和签到情况，完成后关闭浏览器。")

    try:
        result = open_saved_session_browser(account_name, url)
    except Exception as e:
        logging.error(f"打开浏览器失败: {e}")
        return

    balance = result.get("balance")
    opened_url = result.get("opened_url", url)
    if balance:
        logging.info(f"[{account_name}] 当前余额: {balance}")
    logging.info(f"[{account_name}] 浏览器已关闭（打开地址: {opened_url}）")


def cmd_probe(account_name=None):
    """用账号 API 令牌发一条 Claude Code 测试请求，检测可用性。"""
    from server import _run_claude_probe, _mask_token

    config = load_config()
    if account_name:
        accounts = [a for a in config["accounts"] if a["name"] == account_name]
        if not accounts:
            logging.error(f"账号 '{account_name}' 不在配置中。")
            return
    else:
        accounts = config["accounts"]

    targets = [a for a in accounts if a.get("token")]
    skipped = len(accounts) - len(targets)
    if not targets:
        logging.warning("没有已填写 API 令牌的账号。请在 Web UI 编辑账号并填入令牌。")
        return
    if skipped:
        logging.info(f"跳过 {skipped} 个未填写令牌的账号")

    ok_count = 0
    for a in targets:
        name = a["name"]
        logging.info(f"[{name}] 检测中 ({_mask_token(a['token'])}) ...")
        result = _run_claude_probe(a["token"], a.get("url", DEFAULT_URL))
        if result.get("ok"):
            logging.info(f"[{name}] ✅ {result.get('message', '可用')}")
            ok_count += 1
        else:
            logging.warning(f"[{name}] ❌ [{result.get('category')}] {result.get('message', '不可用')}")

    logging.info(f"检测完成: 可用 {ok_count}/{len(targets)}")


def cmd_list():
    """列出所有账号"""
    config = load_config()
    accounts = config.get("accounts", [])

    if not accounts:
        print("没有配置任何账号。")
        print("添加账号: python src/checkin.py login <账号名>")
        return

    print(f"共 {len(accounts)} 个账号:\n")
    print(f"{'序号':<6}{'账号名':<20}{'URL':<35}{'会话状态'}")
    print("-" * 75)
    for i, account in enumerate(accounts, 1):
        name = account["name"]
        url = account.get("url", DEFAULT_URL)
        status = "已保存" if has_session(name) else "未登录"
        print(f"{i:<6}{name:<20}{url:<35}{status}")


def cmd_add(account_name, url=None):
    """添加账号"""
    config = load_config()
    names = [a["name"] for a in config["accounts"]]
    if account_name in names:
        logging.info(f"账号 '{account_name}' 已存在。")
        return

    config["accounts"].append({"name": account_name, "url": url or DEFAULT_URL})
    save_config(config)
    logging.info(f"已添加账号 '{account_name}'。请运行 login 命令登录: python src/checkin.py login {account_name}")


def cmd_remove(account_name):
    """移除账号"""
    config = load_config()
    original_len = len(config["accounts"])
    config["accounts"] = [a for a in config["accounts"] if a["name"] != account_name]

    if len(config["accounts"]) == original_len:
        logging.warning(f"账号 '{account_name}' 不存在。")
        return

    save_config(config)

    # 删除浏览器配置文件
    profile_dir = PROFILES_DIR / account_name
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
        logging.info(f"已删除账号 '{account_name}' 的浏览器数据。")

    logging.info(f"已移除账号 '{account_name}'。")


def main():
    parser = argparse.ArgumentParser(description="AnyRouter 自动签到工具")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # login
    login_parser = subparsers.add_parser("login", help="手动登录并保存会话")
    login_parser.add_argument("account", help="账号名称")

    # checkin
    checkin_parser = subparsers.add_parser("checkin", help="自动签到")
    checkin_parser.add_argument("account", nargs="?", help="账号名称（不指定则签到所有）")

    # open
    open_parser = subparsers.add_parser("open", help="打开已保存会话的浏览器，手动检查状态")
    open_parser.add_argument("account", help="账号名称")

    # probe
    probe_parser = subparsers.add_parser("probe", help="用 API 令牌检测账号可用性")
    probe_parser.add_argument("account", nargs="?", help="账号名称（不指定则检测所有已填令牌的账号）")

    # list
    subparsers.add_parser("list", help="查看账号列表")

    # add
    add_parser = subparsers.add_parser("add", help="添加账号")
    add_parser.add_argument("account", help="账号名称")
    add_parser.add_argument("--url", default=None, help="签到 URL（默认: anyrouter.top）")

    # remove
    remove_parser = subparsers.add_parser("remove", help="移除账号")
    remove_parser.add_argument("account", help="账号名称")

    args = parser.parse_args()

    setup_logging()

    if args.command == "login":
        cmd_login(args.account)
    elif args.command == "checkin":
        cmd_checkin(args.account)
    elif args.command == "open":
        cmd_open(args.account)
    elif args.command == "probe":
        cmd_probe(args.account)
    elif args.command == "list":
        cmd_list()
    elif args.command == "add":
        cmd_add(args.account, args.url)
    elif args.command == "remove":
        cmd_remove(args.account)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
