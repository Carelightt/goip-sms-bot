# ==============================================================
# bot.py — DİNAMİK "From:" TEMELLİ MARKA FİLTRESİ + RAPOR + TELEGRAM API BLOĞU TAM
# ==============================================================

import os, re, time, json, html, logging, requests, tempfile, random, shutil
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# ==== AYARLAR ====
GOIP_URL  = "http://5.11.128.154:5050/default/en_US/sms.html?type=sms_inbox"
GOIP_USER = "sms"
GOIP_PASS = "9091"

BOT_TOKEN = "8299573802:AAFrTWxpx2JuJgv2vsVZ4r2NMTT4B16KMZg"
CHAT_ID   = -1003098321304  # routes.json boşsa fallback

POLL_INTERVAL = 10
ALLOWED_USER_IDS = {8450766241, 6672759317}

BRAND_ALIASES = {
    "google": "600653000000",
    "trendyol": "8555",
    "hepsiburada": "7575",
    "vakifbank": "4888",
    "ziraat": "4747",
    "facebook": ["FACEBOOK", "+320335320002"],
}

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE     = os.path.join(BASE_DIR, "seen.json")
ROUTES_FILE   = os.path.join(BASE_DIR, "routes.json")
FILTERS_FILE  = os.path.join(BASE_DIR, "filters.json")
REPORTS_FILE  = os.path.join(BASE_DIR, "reports.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("goip-forwarder")

# ==============================================================
# === YENİ EKLENEN FONKSİYONLAR ===
# ==============================================================

def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen), f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("save_seen hata: %s", e)

def load_routes():
    try:
        with open(ROUTES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_routes(routes):
    try:
        with open(ROUTES_FILE, "w", encoding="utf-8") as f:
            json.dump(routes, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("save_routes hata: %s", e)

def load_filters():
    try:
        with open(FILTERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_filters(filters):
    try:
        with open(FILTERS_FILE, "w", encoding="utf-8") as f:
            json.dump(filters, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("save_filters hata: %s", e)

def load_reports():
    try:
        with open(REPORTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_reports(reports):
    try:
        with open(REPORTS_FILE, "w", encoding="utf-8") as f:
            json.dump(reports, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("save_reports hata: %s", e)

# ==============================================================
# === TELEGRAM API BLOĞU ===
# ==============================================================

def tg_delete_webhook(drop=True):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates={str(drop).lower()}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            print("✅ Webhook silindi.")
        else:
            print(f"⚠️ Webhook silinemedi: {r.text}")
    except Exception as e:
        print(f"⚠️ Webhook silme hatası: {e}")

def tg_api(method, params=None, use_get=False, timeout=15):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
        if use_get:
            r = requests.get(url, params=params or {}, timeout=timeout)
        else:
            r = requests.post(url, json=params or {}, timeout=timeout)
        return r
    except Exception as e:
        log.warning("tg_api hata: %s", e)
        return None

def tg_send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    try:
        data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
        if reply_markup:
            data["reply_markup"] = reply_markup
        r = tg_api("sendMessage", data)
        return r
    except Exception as e:
        log.error("tg_send_message hata: %s", e)
        return None

def tg_edit_message(chat_id, msg_id, text, reply_markup=None):
    try:
        data = {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            data["reply_markup"] = reply_markup
        tg_api("editMessageText", data)
    except Exception as e:
        log.error("tg_edit_message hata: %s", e)

def tg_reply_markup(buttons):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for (t, d) in row] for row in buttons]}

# ==============================================================
# === GOIP SESSION ===
# ==============================================================

def make_session() -> requests.Session:
    s = requests.Session()
    s.auth = HTTPBasicAuth(GOIP_USER, GOIP_PASS)
    s.headers.update({"User-Agent": "GoIP-SMS-Forwarder/1.0"})
    retry = Retry(total=3, connect=3, read=3, backoff_factor=0.6,
                  status_forcelist=[502, 503, 504], allowed_methods=["GET", "POST"])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

SESSION = make_session()

def fetch_html():
    r = SESSION.get(GOIP_URL, timeout=(3, 6))
    if r.status_code == 200:
        html_txt = r.text
        try:
            with open(os.path.join(BASE_DIR, "last_inbox.html"), "w", encoding="utf-8") as f:
                f.write(html_txt)
        except Exception: pass
        return html_txt
    log.warning("GoIP HTTP durum kodu: %s", r.status_code); return ""

def parse_sms_blocks(html_text: str):
    results = []
    if not html_text: return results
    sms_blocks = list(re.finditer(r'sms\s*=\s*\[(.*?)\]', html_text, flags=re.S | re.I))
    if not sms_blocks:
        log.warning("Parser: sms=[...] bloğu bulunamadı."); return results
    for m in sms_blocks:
        arr_str = m.group(1)
        tail = html_text[m.end(): m.end() + 800]
        line_match = re.search(r'sms_row_insert\([^)]*?(\d+)\s*\)', tail, flags=re.S)
        if line_match:
            try: line = int(line_match.group(1))
            except Exception: line = 0
        else:
            lm2 = re.search(r'(?:curr_)?line\s*=\s*(\d+)', tail, flags=re.I)
            if lm2:
                try: line = int(lm2.group(1))
                except Exception: line = 0
            else: line = 0
        msgs = re.findall(r'"([^"]*)"', arr_str) or re.findall(r"'([^']*)'", arr_str)
        for raw in msgs:
            raw = (raw or "").strip()
            if not raw: continue
            parts = raw.split(",", 2)
            if len(parts) < 3:
                parts = raw.split(";", 2)
                if len(parts) < 3: continue
            date, num, content = parts[0], parts[1], parts[2]
            results.append({
                "line": int(line),
                "date": date.strip(),
                "num":  num.strip(),
                "content": content.strip(),
            })
    log.info("Parser: %d SMS satırı çıkarıldı.", len(results))
    return results

def _norm(s: str) -> str:
    s = s.replace("\r", "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def make_key(row) -> str:
    return f"{row['line']}::{_norm(row.get('date',''))}::{_norm(row.get('num',''))}::{_norm(row.get('content',''))}"

def initial_warmup_seen(seen:set):
    html_txt = fetch_html()
    if not html_txt:
        log.info("Warm-up: HTML boş geldi, yine de devam."); return
    rows = parse_sms_blocks(html_txt)
    added = 0
    for row in rows:
        key = make_key(row)
        if key not in seen:
            seen.add(key); added += 1
    if added: save_seen(seen)
    log.info("Warm-up tamam: %d kayıt seen olarak işaretlendi.", added)

def normalize_brand_key(s: str) -> str:
    if s is None: return ""
    s = s.strip()
    s = s.replace("ı","i").replace("İ","i")
    s = s.lower()
    s = re.sub(r"[^a-z0-9_]+", "", s)
    return s

def extract_from_field(row) -> str | None:
    content = row.get("content","")
    num = row.get("num","")
    m = re.search(r'(?i)\bfrom\s*[:=]\s*([^\s\]\)\(,;|]+)', content)
    if m: return normalize_brand_key(m.group(1))
    if re.search(r'[A-Za-z]', num):
        token = re.findall(r'[A-Za-z0-9_]+', num)
        if token: return normalize_brand_key(token[0])
    return None

# ==============================================================
# === RAPOR & ROUTING HELPERLARI ===
# ==============================================================

def incr_report(reports, chat_id, brand_key):
    reports.setdefault(str(chat_id), {})
    reports[str(chat_id)].setdefault(brand_key or "_other", 0)
    reports[str(chat_id)][brand_key or "_other"] += 1
    save_reports(reports)

def reset_report(reports, chat_id):
    reports[str(chat_id)] = {}
    save_reports(reports)

def format_report(reports, chat_id):
    r = reports.get(str(chat_id)) or {}
    if not r: return "Rapor yok."
    lines = []
    for k, v in r.items():
        lines.append(f"• {k}: {v}")
    return "\n".join(lines)

def send_tg_formatted(chat_id, line, num, content, date):
    msg = f"📩 <b>L{line}</b> | <i>{date}</i>\n<b>{num}</b>\n{html.escape(content)}"
    try:
        tg_send_message(chat_id, msg)
        return True
    except: return False

def _is_allowed_for_chat_line(chat_id:str, line:int, brand_key:str|None, filters:dict) -> bool:
    cflt = filters.get(str(chat_id)) or {}
    active_marks_for_line = [b for b, arr in cflt.items() if line in (arr or [])]
    if not active_marks_for_line: return True
    if not brand_key: return False
    brand_key = normalize_brand_key(brand_key)
    return brand_key in active_marks_for_line

def detect_brand_key(row) -> str | None:
    return extract_from_field(row)

def deliver_sms_to_routes(row, routes:dict, filters:dict, reports:dict):
    line = int(row['line'])
    brand_key = detect_brand_key(row)
    sent_total = 0

    if not routes:
        if _is_allowed_for_chat_line(str(CHAT_ID), line, brand_key, filters):
            if send_tg_formatted(CHAT_ID, row['line'], row['num'], row['content'], row['date']):
                incr_report(reports, str(CHAT_ID), brand_key)
                sent_total += 1
        return sent_total

    for chat_id, lines in routes.items():
        if line not in (lines or []): continue
        if not _is_allowed_for_chat_line(chat_id, line, brand_key, filters): continue
        ok = send_tg_formatted(chat_id, row['line'], row['num'], row['content'], row['date'])
        if ok:
            incr_report(reports, chat_id, brand_key)
            sent_total += 1
        else:
            time.sleep(0.3 + random.random()*0.5)
            if send_tg_formatted(chat_id, row['line'], row['num'], row['content'], row['date']):
                incr_report(reports, chat_id, brand_key)
                sent_total += 1
    return sent_total

# =============== MAIN LOOP ===============
def main():
    tg_delete_webhook(drop=False)
    seen    = load_seen()
    routes  = load_routes()
    filters = load_filters()
    reports = load_reports()
    log.info("Başladı, görülen %d | aktif grup: %d | filtreli grup: %d",
             len(seen), len(routes), len(filters))
    initial_warmup_seen(seen)
    while True:
        try:
            routes, filters, reports = poll_and_handle_updates(routes, filters, reports)
            html_txt = fetch_html()
            if not html_txt:
                time.sleep(3); continue
            rows = parse_sms_blocks(html_txt)
            newc = 0; routed = 0
            for row in rows:
                key = make_key(row)
                if key in seen: continue
                sent = deliver_sms_to_routes(row, routes, filters, reports)
                if sent > 0: routed += sent
                seen.add(key); newc += 1
            if newc:
                save_seen(seen)
                log.info("Yeni SMS: %d | Gönderilen: %d", newc, routed)
            time.sleep(POLL_INTERVAL)
        except Exception as e:
            log.exception("Ana döngü hatası: %s", e)
            time.sleep(5)

if __name__ == "__main__":
    main()
