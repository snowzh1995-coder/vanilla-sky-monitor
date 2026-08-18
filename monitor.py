#!/usr/bin/env python3
"""Monitor Vanilla Sky tickets and send a push notification when bookable."""

from __future__ import annotations

import argparse
import getpass
import html as html_lib
import http.cookiejar
import json
import os
import random
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


BASE_URL = "https://ticket.vanillasky.ge"
TICKETS_URL = f"{BASE_URL}/en/tickets"
DEFAULT_CONFIG = {
    "target_date": "2026-10-01",
    "departure_id": 6,
    "departure_name": "Mestia",
    "arrival_id": 7,
    "arrival_name": "Natakhtari",
    "poll_seconds": 60,
    "notification": {
        "provider": "telegram",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "ntfy_topic": "",
    },
}
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "monitor_state.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 VanillaSkyTicketMonitor/1.0"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def request(
    url: str,
    *,
    data: dict[str, str] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
    timeout: int = 25,
    headers: dict[str, str] | None = None,
) -> bytes:
    body = urllib.parse.urlencode(data).encode("utf-8") if data else None
    final_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if body is not None:
        final_headers["Content-Type"] = "application/x-www-form-urlencoded"
    if headers:
        final_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=final_headers)
    client = opener or urllib.request.build_opener()
    with client.open(req, timeout=timeout) as response:
        return response.read()


def fetch_json(url: str) -> object:
    raw = request(url)
    return json.loads(raw.decode("utf-8"))


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return json.loads(json.dumps(fallback))
    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    return loaded if isinstance(loaded, dict) else json.loads(json.dumps(fallback))


def save_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temp.replace(path)


def deep_merge(base: dict, overlay: dict) -> dict:
    result = json.loads(json.dumps(base))
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict:
    cfg = deep_merge(DEFAULT_CONFIG, load_json(CONFIG_PATH, {}))
    notice = cfg["notification"]
    notice["provider"] = os.getenv("VS_NOTIFY_PROVIDER", notice.get("provider", "telegram"))
    notice["telegram_bot_token"] = os.getenv(
        "VS_TELEGRAM_BOT_TOKEN", notice.get("telegram_bot_token", "")
    )
    notice["telegram_chat_id"] = os.getenv(
        "VS_TELEGRAM_CHAT_ID", str(notice.get("telegram_chat_id", ""))
    )
    notice["ntfy_topic"] = os.getenv("VS_NTFY_TOPIC", notice.get("ntfy_topic", ""))
    cfg["target_date"] = os.getenv("VS_TARGET_DATE", cfg["target_date"])
    cfg["poll_seconds"] = int(os.getenv("VS_POLL_SECONDS", cfg["poll_seconds"]))
    return cfg


def telegram_api(token: str, method: str, payload: dict[str, str] | None = None) -> object:
    url = f"https://api.telegram.org/bot{token}/{method}"
    raw = request(url, data=payload)
    response = json.loads(raw.decode("utf-8"))
    if not response.get("ok"):
        raise RuntimeError(f"Telegram 返回错误：{response.get('description', response)}")
    return response.get("result")


def notify(config: dict, message: str, *, urgent: bool = False) -> None:
    notice = config["notification"]
    provider = str(notice.get("provider", "")).lower()
    if provider == "telegram":
        token = str(notice.get("telegram_bot_token", "")).strip()
        chat_id = str(notice.get("telegram_chat_id", "")).strip()
        if not token or not chat_id:
            raise RuntimeError("Telegram 尚未设置，请先运行：python monitor.py --setup")
        telegram_api(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": "false",
            },
        )
        return

    if provider == "ntfy":
        topic = str(notice.get("ntfy_topic", "")).strip()
        if not topic:
            raise RuntimeError("ntfy 尚未设置，请先运行：python monitor.py --setup")
        request(
            f"https://ntfy.sh/{urllib.parse.quote(topic, safe='')}",
            data=None,
            headers={
                "Title": "Vanilla Sky ticket alert",
                "Priority": "5" if urgent else "3",
                "Tags": "airplane,ticket",
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=20,
            opener=RawBodyOpener(message.encode("utf-8")),
        )
        return

    raise RuntimeError(f"不支持的通知方式：{provider!r}")


class RawBodyHandler(urllib.request.BaseHandler):
    def __init__(self, body: bytes):
        self.body = body

    def http_request(self, req: urllib.request.Request) -> urllib.request.Request:
        req.data = self.body
        return req

    https_request = http_request


def RawBodyOpener(body: bytes) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(RawBodyHandler(body))


def calendar_dates(config: dict) -> set[str]:
    endpoint = (
        f"{BASE_URL}/custom/check-flight/"
        f"{config['departure_id']}/{config['arrival_id']}"
    )
    payload = fetch_json(endpoint)
    if not isinstance(payload, dict) or not isinstance(payload.get("from"), list):
        raise RuntimeError(f"网站返回了无法识别的日期数据：{payload!r}")
    return {str(item) for item in payload["from"]}


def strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def confirm_bookable(config: dict) -> tuple[bool, str]:
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    page = request(TICKETS_URL, opener=opener).decode("utf-8", errors="replace")
    build_id_match = re.search(
        r'name="form_build_id"\s+value="([^"]+)"', page, flags=re.IGNORECASE
    )
    if not build_id_match:
        raise RuntimeError("没有找到网站查询表单，网站结构可能已变化")

    query_date = datetime.strptime(config["target_date"], "%Y-%m-%d").strftime("%m/%d/%Y")
    form = {
        "types": "0",
        "departure": str(config["departure_id"]),
        "date_picker": query_date,
        "arrive": str(config["arrival_id"]),
        "person_count": "1",
        "person_types[adult]": "1",
        "person_types[child]": "0",
        "person_types[infant]": "0",
        "form_build_id": build_id_match.group(1),
        "form_id": "form_select_date",
        "op": "",
    }
    result = request(TICKETS_URL, data=form, opener=opener, timeout=35).decode(
        "utf-8", errors="replace"
    )
    no_ticket = "There are no available tickets" in result
    bookable = (
        not no_ticket
        and 'class="flight-item' in result
        and 'value="Continue"' in result
        and config["departure_name"].lower() in result.lower()
        and config["arrival_name"].lower() in result.lower()
    )

    if not bookable:
        return False, "日期已出现，但查询页当前显示没有可售票"

    time_match = re.search(
        r'class="flight-dates"[^>]*>.*?(\d{1,2}:\d{2})', result, flags=re.I | re.S
    )
    price_match = re.search(
        r'class="gel style-price-box"[^>]*>(.*?)</span>', result, flags=re.I | re.S
    )
    details = []
    if time_match:
        details.append(f"起飞时间 {time_match.group(1)}")
    if price_match:
        details.append(f"价格 {strip_tags(price_match.group(1))}")
    return True, "，".join(details) if details else "查询页已出现可选择航班和 Continue 按钮"


def check_once(config: dict) -> tuple[str, str]:
    target = config["target_date"]
    dates = calendar_dates(config)
    if target not in dates:
        latest = max(dates) if dates else "暂无日期"
        return "not_released", f"尚未放票（目前最晚可选日期：{latest}）"
    available, detail = confirm_bookable(config)
    return ("available", detail) if available else ("released_no_inventory", detail)


def message_for(config: dict, status: str, detail: str) -> str:
    route = f"{config['departure_name']} → {config['arrival_name']}"
    date = config["target_date"]
    if status == "available":
        return (
            "🚨 Vanilla Sky 有票了！\n"
            f"日期：{date}\n航线：{route}\n{detail}\n"
            f"马上购票：{TICKETS_URL}"
        )
    return (
        "✈️ Vanilla Sky 已更新放票日历\n"
        f"{date} {route} 已出现在可选日期中，但当前没有查到可售座位。"
        "程序会继续监控退票或新增座位。\n"
        f"手动查看：{TICKETS_URL}"
    )


def process_status(config: dict, status: str, detail: str, state: dict) -> bool:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {detail}", flush=True)
    state_key = "available_notified" if status == "available" else "release_notified"
    if status == "not_released" or state.get(state_key):
        return False
    notify(config, message_for(config, status, detail), urgent=(status == "available"))
    state[state_key] = utc_now()
    if status == "available":
        state.setdefault("release_notified", state[state_key])
    save_json(STATE_PATH, state)
    print("通知已发送。", flush=True)
    return status == "available"


def setup() -> int:
    config = deep_merge(DEFAULT_CONFIG, load_json(CONFIG_PATH, {}))
    print("\n请选择手机通知方式：")
    print("1. Telegram（推荐，只需创建一个机器人）")
    print("2. ntfy（需要安装 ntfy 手机 App）")
    choice = input("输入 1 或 2 [1]：").strip() or "1"

    if choice == "1":
        token = getpass.getpass("粘贴 BotFather 给你的 Bot Token（输入时不会显示）：").strip()
        if not token:
            print("未输入 Token，设置已取消。")
            return 1
        print("现在请在 Telegram 中打开你的机器人，给它发送任意一条消息。")
        input("发送完成后按 Enter：")
        updates = telegram_api(token, "getUpdates")
        chat_id = ""
        if isinstance(updates, list):
            for item in reversed(updates):
                container = item.get("message") or item.get("channel_post") or {}
                chat = container.get("chat") or {}
                if chat.get("id") is not None:
                    chat_id = str(chat["id"])
                    break
        if not chat_id:
            print("没有读到消息。请确认已给机器人发消息，然后重新运行设置。")
            return 1
        config["notification"].update(
            {
                "provider": "telegram",
                "telegram_bot_token": token,
                "telegram_chat_id": chat_id,
            }
        )
    elif choice == "2":
        suggestion = "vanillasky-" + secrets.token_urlsafe(12).replace("_", "-")
        print("请在 ntfy App 中订阅同名 Topic。Topic 名越随机越安全。")
        topic = input(f"Topic [{suggestion}]：").strip() or suggestion
        config["notification"].update({"provider": "ntfy", "ntfy_topic": topic})
        print(f"请在 ntfy App 中订阅：https://ntfy.sh/{topic}")
    else:
        print("选择无效。")
        return 1

    save_json(CONFIG_PATH, config)
    notify(config, "✅ Vanilla Sky 机票监控通知测试成功。")
    print("设置完成，测试通知已发送到你的手机。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="监控 Vanilla Sky 指定日期的机票")
    parser.add_argument("--setup", action="store_true", help="设置手机推送")
    parser.add_argument("--check-once", action="store_true", help="只检查一次后退出")
    parser.add_argument("--test-notification", action="store_true", help="发送测试通知")
    args = parser.parse_args()

    if args.setup:
        return setup()

    config = load_config()
    if args.test_notification:
        notify(config, "✅ Vanilla Sky 机票监控通知测试成功。")
        print("测试通知已发送。")
        return 0

    state = load_json(STATE_PATH, {})
    consecutive_errors = 0
    print(
        f"开始监控：{config['target_date']} "
        f"{config['departure_name']} → {config['arrival_name']}\n"
        f"查询间隔：{max(30, int(config['poll_seconds']))} 秒。按 Ctrl+C 可停止。",
        flush=True,
    )

    while True:
        try:
            status, detail = check_once(config)
            consecutive_errors = 0
            available_sent = process_status(config, status, detail, state)
            if available_sent or args.check_once:
                return 0
        except KeyboardInterrupt:
            print("\n监控已停止。")
            return 0
        except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            consecutive_errors += 1
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 检查失败：{exc}", flush=True)
            if args.check_once:
                return 2
            if consecutive_errors >= 5:
                print("网站或网络连续异常；程序会继续自动重试。", flush=True)

        interval = max(30, int(config["poll_seconds"]))
        time.sleep(interval + random.uniform(0, min(5, interval * 0.1)))


if __name__ == "__main__":
    sys.exit(main())
