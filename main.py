# main.py
import os, re, time, json, hashlib, datetime, threading
import urllib.parse
from typing import Tuple

import requests
from flask import Flask, jsonify

# =========================
# ====== Config ===========
# =========================
OUTLIER_URL = "https://app.outlier.ai/projects"

OUTLIER_COOKIE = os.getenv("OUTLIER_COOKIE", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC",
                                   "300"))  # 5 phút mặc định

STATE_FILE = "state.json"
HAS_STREAK_MIN = 2  # cần >=2 lần liên tiếp thấy has_tasks mới notify
NOTIFY_ON_FIRST_RUN = False  # lần chạy đầu không notify

# =========================
# ====== HTTP session =====
# =========================
session = requests.Session()
session.headers.update({
    "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Cookie": OUTLIER_COOKIE,
})


# =========================
# ====== State I/O ========
# =========================
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    return {
        "last_hash": s.get("last_hash", ""),
        "last_status": s.get("last_status", "unknown"),
        "last_checked": s.get("last_checked"),
        "has_streak": s.get("has_streak", 0),
    }


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


state = load_state()


# =========================
# ====== Telegram =========
# =========================
def tg_send(msg: str):
    """Gửi tin nhắn Telegram bằng GET request"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram chưa được cấu hình đầy đủ.")
        return
    if not msg or not msg.strip():
        print("⚠️ Tin nhắn trống, bỏ qua.")
        return

    # Encode nội dung để an toàn (ví dụ: dấu cách, tiếng Việt, ký tự đặc biệt)
    encoded_msg = urllib.parse.quote_plus(msg.strip())
    url = (f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
           f"?chat_id={TELEGRAM_CHAT_ID}&text={encoded_msg}&parse_mode=HTML")

    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        print("✅ Đã gửi Telegram:", msg)
    except Exception as e:
        print("🚫 Lỗi gửi Telegram:", e)


# =========================
# === Headless Playwright ==
# =========================
# YÊU CẦU: pip install playwright  +  python -m playwright install chromium
def _parse_cookie_string(cookie_str: str, domain: str):
    pairs = [c.strip() for c in cookie_str.split(";") if "=" in c]
    cookies = []
    for p in pairs:
        name, value = p.split("=", 1)
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": domain,
            "path": "/",
            "httpOnly": False,
            "secure": True
        })
    return cookies


def render_and_read() -> Tuple[str, str]:
    """
    Mở https://app.outlier.ai/projects, đợi JS gọi API xong, rồi đọc text trong
    <div class="radix-themes">…</div>.
    Trả: (status, content_hash_text)
      - status: 'no_tasks' | 'has_tasks' | 'login_required' | 'unknown'
      - content_hash_text: chuỗi text để hash (chống spam notify)
    """
    from playwright.sync_api import sync_playwright

    cookie_str = OUTLIER_COOKIE
    if not cookie_str:
        return "login_required", ""

    with sync_playwright() as p:
        # Replit cần --no-sandbox
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        ctx = browser.new_context()
        ctx.add_cookies(_parse_cookie_string(cookie_str, ".outlier.ai"))

        page = ctx.new_page()

        # Inject đếm pending fetch/xhr để biết khi nào im lặng
        page.add_init_script("""
            (function () {
              const origFetch = window.fetch;
              window.__pending = 0;
              window.fetch = async function() {
                window.__pending++;
                try { return await origFetch.apply(this, arguments); }
                finally { window.__pending--; }
              };
              const open = XMLHttpRequest.prototype.open;
              const send = XMLHttpRequest.prototype.send;
              XMLHttpRequest.prototype.open = function() { this.addEventListener('loadend', () => { window.__pending = Math.max(0, window.__pending-1); }); open.apply(this, arguments); };
              XMLHttpRequest.prototype.send = function() { window.__pending++; try { send.apply(this, arguments); } catch(e){ window.__pending = Math.max(0, window.__pending-1); throw e; } };
            })();
        """)

        # Vào trang và chờ DOM sẵn
        page.goto(OUTLIER_URL, wait_until="domcontentloaded", timeout=30000)
        # Thử chờ network idle (có thể không đạt do socket keep-alive)
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except:
            pass

        # Chờ selector .radix-themes xuất hiện (tối đa 20s)
        try:
            page.wait_for_selector("div.radix-themes", timeout=20000)
        except:
            # Không có container ⇒ khả năng chưa login
            txt = page.inner_text("body")
            ctx.close()
            browser.close()
            low = txt.lower()
            if any(k in low for k in
                   ["sign in", "log in", "continue with google", "next-auth"]):
                return "login_required", txt
            return "unknown", txt

        # Đợi pending request về 0 ổn định ~2s (tổng tối đa 25s)
        t0 = time.time()
        while time.time() - t0 < 25:
            pending = page.evaluate("window.__pending ?? 0")
            if pending == 0:
                time.sleep(2)
                if page.evaluate("window.__pending ?? 0") == 0:
                    break
            time.sleep(0.3)

        # Lấy text trong vùng radix-themes
        try:
            content_text = page.inner_text("div.radix-themes")
        except:
            content_text = page.inner_text("body")

        ctx.close()
        browser.close()

    low = content_text.lower()

    # Login?
    if any(k in low for k in
           ["sign in", "log in", "continue with google", "next-auth"]):
        return "login_required", content_text

    # Quy tắc phân loại:
    # - Nếu rõ ràng có "No tasks available" => no_tasks
    # - Ngược lại: nếu xuất hiện các nút/hành động đặc trưng khi có task
    #   (ví dụ nút "Project details" kèm badge nhiệm vụ…), coi như has_tasks.
    # - Nếu không tìm thấy gì chắc chắn => unknown
    no_markers = [
        "no tasks available", "there are no tasks", "you have no tasks"
    ]
    if any(m in low for m in no_markers):
        return "no_tasks", content_text

    positive_strong = [
        "start task",
        "continue task",
        "available tasks",
        "accept task",
        "project details",  # xuất hiện khi có project hiển thị
        "assigned to you",
    ]
    if any(m in low for m in positive_strong):
        return "has_tasks", content_text

    # fallback: nếu container có “Current project” + KHÔNG có "no tasks available"
    if "current project" in low and not any(m in low for m in no_markers):
        # Cẩn thận: chữ 'Current project' luôn có, nhưng nếu không có 'no tasks available'
        # ta chỉ tạm đánh dấu unknown, tránh báo ảo
        return "unknown", content_text

    return "unknown", content_text


# =========================
# ====== One check ========
# =========================
def check_once():
    now = datetime.datetime.utcnow().isoformat() + "Z"
    try:
        status_text, evidence_text = render_and_read()
        if status_text == "login_required":
            state.update({"last_status": status_text, "last_checked": now})
            save_state(state)
            tg_send(
                "⚠️ Cookie có thể hết hạn hoặc yêu cầu đăng nhập. Hãy cập nhật OUTLIER_COOKIE."
            )
            print(f"[{now}] login_required")
            return {
                "ok": False,
                "status": status_text,
                "changed": False,
                "streak": state.get("has_streak", 0),
                "time": now
            }

        # Hash nội dung container để phát hiện thay đổi thực sự
        content_hash = hashlib.md5(evidence_text.encode("utf-8",
                                                        "ignore")).hexdigest()

        prev_hash = state.get("last_hash", "")
        prev_streak = int(state.get("has_streak", 0))
        first_run = (state.get("last_checked") is None)

        changed = (content_hash
                   != prev_hash) if status_text != "unknown" else False
        has_streak = (prev_streak + 1) if status_text == "has_tasks" else 0

        should_notify = (status_text == "has_tasks"
                         and has_streak >= HAS_STREAK_MIN and changed
                         and (NOTIFY_ON_FIRST_RUN or not first_run))
        if should_notify:
            tg_send(
                "🔔 <b>Outlier</b>: Có dấu hiệu <b>task mới</b>. Vào kiểm tra: https://app.outlier.ai/projects"
            )

        if status_text != "unknown":
            state["last_hash"] = content_hash
        state["last_status"] = status_text
        state["last_checked"] = now
        state["has_streak"] = has_streak
        save_state(state)

        print(
            f"[{now}] checked -> status={status_text}, changed={changed}, streak={has_streak}, first_run={first_run}"
        )
        return {
            "ok": True,
            "status": status_text,
            "changed": changed,
            "streak": has_streak,
            "time": now
        }

    except Exception as e:
        print(f"[{now}] ERROR:", e)
        tg_send(f"⚠️ Outlier checker lỗi: {e}")
        return {"ok": False, "error": str(e), "time": now}


# =========================
# ====== Background =======
# =========================
def loop_worker():
    time.sleep(3)
    if not OUTLIER_COOKIE:
        print("Missing env: OUTLIER_COOKIE")
        print(
            "Service vẫn chạy, nhưng /check sẽ fail cho đến khi cung cấp cookie."
        )
    while True:
        try:
            check_once()
        except Exception as ex:
            print("Loop error:", ex)
        time.sleep(CHECK_INTERVAL_SEC)


# =========================
# ====== Flask app ========
# =========================
app = Flask(__name__)


@app.get("/")
def root():
    return jsonify({
        "service": "outlier-watcher",
        "last": state.get("last_checked"),
        "status": state.get("last_status"),
        "streak": state.get("has_streak", 0),
    })


@app.get("/health")
def health():
    return "ok"


@app.get("/check")
def manual_check():
    return jsonify(check_once())


@app.get("/env")
def env_info():
    return jsonify({
        "has_cookie": bool(OUTLIER_COOKIE),
        "has_bot_token": bool(TELEGRAM_BOT_TOKEN),
        "has_chat_id": bool(TELEGRAM_CHAT_ID),
        "interval": CHECK_INTERVAL_SEC,
        "state_status": state.get("last_status"),
    })


@app.get("/reset")
def reset_state():
    global state
    state = {
        "last_hash": "",
        "last_status": "unknown",
        "last_checked": None,
        "has_streak": 0
    }
    save_state(state)
    return jsonify(state)


# =========================
# ====== Entrypoint =======
# =========================
if __name__ == "__main__":
    threading.Thread(target=loop_worker, daemon=True).start()
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
