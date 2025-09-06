# bot.py
import os, re, time, json, html, logging, requests, tempfile, random, shutil
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==== AYARLAR ====
GOIP_URL  = "http://5.11.128.154:6060/default/en_US/tools.html?type=sms_inbox"
GOIP_USER = "user"
GOIP_PASS = "9090"

BOT_TOKEN = "8480045051:AAGDht_XMNXuF2ZNUKC49J_m_n2GTGkoyys"
CHAT_IDS  = [-1002951199599, -1003063387437]  # (GERİYE UYUMLULUK) routes.json boşsa buraya gönderilir

POLL_INTERVAL = 10

# SADECE BU KULLANICI KOMUT ÇALIŞTIRSIN
OWNER_ID = 6672759317
DENY_MSG = "⛔ Yetkiniz yoktur. İletişim @CengizzAtay"

# Dosya yolları
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE   = os.path.join(BASE_DIR, "seen.json")
ROUTES_FILE = os.path.join(BASE_DIR, "routes.json")  # { "<chat_id>": [1,5,7], ... }

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("goip-forwarder")

# ---- HTTP session with retry/backoff ----
def make_session() -> requests.Session:
    s = requests.Session()
    s.auth = HTTPBasicAuth(GOIP_USER, GOIP_PASS)
    s.headers.update({"User-Agent": "GoIP-SMS-Forwarder/1.0"})
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.6,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

SESSION = make_session()

# =============== STATE ===============
def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            return set(json.load(open(SEEN_FILE, "r", encoding="utf-8")))
        except Exception:
            return set()
    return set()

def _atomic_write(path:str, data_text:str):
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=d)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data_text)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    try:
        os.replace(tmp_path, path)
    except OSError:
        shutil.move(tmp_path, path)

def save_seen(seen:set):
    _atomic_write(SEEN_FILE, json.dumps(list(seen), ensure_ascii=False))

def load_routes() -> dict:
    if os.path.exists(ROUTES_FILE):
        try:
            data = json.load(open(ROUTES_FILE, "r", encoding="utf-8"))
            fixed = {}
            for k, v in data.items():
                try:
                    cid = str(int(k))
                except Exception:
                    cid = str(k)
                fixed[cid] = set(int(x) for x in v if str(x).isdigit())
            return fixed
        except Exception as e:
            log.warning("routes.json okunamadı: %s", e)
            return {}
    return {}

def save_routes(routes:dict):
    serializable = {cid: sorted(list(v)) for cid, v in routes.items()}
    _atomic_write(ROUTES_FILE, json.dumps(serializable, ensure_ascii=False, indent=2))

# =============== GOIP ===============
def fetch_html():
    r = SESSION.get(GOIP_URL, timeout=(3, 6))
    if r.status_code == 200:
        return r.text
    log.warning("GoIP HTTP durum kodu: %s", r.status_code)
    return ""

def parse_sms_blocks(html_text:str):
    results=[]
    for m in re.finditer(r'sms=\s*\[(.*?)\];\s*pos=(\d+);\s*sms_row_insert\(.*?(\d+)\)', html_text, flags=re.S):
        arr_str, pos, line = m.groups()
        line = int(line)
        msgs = re.findall(r'"([^"]*)"', arr_str)
        for raw in msgs:
            raw = raw.strip()
            if not raw:
                continue
            parts = raw.split(",", 2)  # date, num, content
            if len(parts) < 3:
                continue
            date, num, content = parts
            results.append({
                "line": line,
                "date": date.strip(),
                "num":  num.strip(),
                "content": content.strip()
            })
    return results

# =============== HELPERS ===============
def _norm(s: str) -> str:
    s = s.replace("\r", "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def make_key(row) -> str:
    return f"{row['line']}::{_norm(row.get('date',''))}::{_norm(row.get('num',''))}::{_norm(row.get('content',''))}"

def initial_warmup_seen(seen:set):
    html_txt = fetch_html()
    if not html_txt:
        log.info("Warm-up: HTML boş geldi, yine de devam.")
        return
    rows = parse_sms_blocks(html_txt)
    added = 0
    for row in rows:
        key = make_key(row)
        if key not in seen:
            seen.add(key)
            added += 1
    if added:
        save_seen(seen)
    log.info("Warm-up tamam: %d kayıt seen olarak işaretlendi.", added)

# =============== TELEGRAM CORE ===============
def tg_api(method, params=None, use_get=False, timeout=20):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        if use_get:
            r = SESSION.get(url, params=params or {}, timeout=(3, timeout))
        else:
            r = SESSION.post(url, data=params or {}, timeout=(3, timeout))
        return r
    except requests.RequestException as e:
        log.warning("TG %s network hata: %s", method, e)
        return None

def tg_delete_webhook(drop=False):
    r = tg_api("deleteWebhook", {"drop_pending_updates": "true" if drop else "false"})
    if r is None:
        return False
    if r.status_code == 200:
        ok = r.json().get("ok", False)
        log.info("deleteWebhook ok=%s", ok)
        return ok
    log.warning("deleteWebhook status=%s %s", r.status_code, r.text[:200])
    return False

def tg_send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True):
    r = tg_api("sendMessage", {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true" if disable_web_page_preview else "false",
    }, use_get=False, timeout=15)
    if not r:
        return False
    if r.status_code != 200:
        log.warning("Telegram hata: %s %s", r.status_code, r.text[:200])
        return False
    return True

def send_tg_formatted(chat_id, line, num, content, date):
    text = (
        f"📲 <b>Yeni SMS</b>\n"
        f"🟢 Line: <code>{line}</code>\n"
        f"👤 Gönderen: <code>{html.escape(num)}</code>\n"
        f"🕒 {html.escape(date)}\n"
        f"💬 <code>{html.escape(content)}</code>"
    )
    return tg_send_message(chat_id, text)

# ---- Long-polling updates ----
UPD_OFFSET = 0

def tg_fetch_updates(timeout=20):
    global UPD_OFFSET
    r = tg_api("getUpdates", {"timeout": timeout, "offset": UPD_OFFSET}, use_get=True, timeout=timeout+5)
    if not r:
        return []
    if r.status_code == 409:
        log.warning("getUpdates 409: webhook aktif. Silmeyi deniyorum…")
        tg_delete_webhook(drop=False)
        r = tg_api("getUpdates", {"timeout": timeout, "offset": UPD_OFFSET}, use_get=True, timeout=timeout+5)
        if not r or r.status_code != 200:
            if r:
                log.warning("getUpdates retry status=%s %s", r.status_code, r.text[:200])
            return []
    if r.status_code != 200:
        log.warning("getUpdates status: %s %s", r.status_code, r.text[:200])
        return []
    data = r.json()
    if not data.get("ok"):
        log.warning("getUpdates ok=false: %s", data)
        return []
    results = data.get("result", [])
    if results:
        UPD_OFFSET = results[-1]["update_id"] + 1
    return results

# (Kalan kısımlar aynı, sadece CHAT_IDS ile devam edecek)
