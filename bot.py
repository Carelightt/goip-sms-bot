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
CHAT_ID   = -1002951199599  # (GERİYE UYUMLULUK) routes.json boşsa buraya gönderilir

POLL_INTERVAL = 10

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
                lines = sorted({int(x) for x in v if isinstance(x, (int, str)) and str(x).isdigit()})
                fixed[cid] = lines
            return fixed
        except Exception as e:
            log.warning("routes.json okunamadı: %s", e)
            return {}
    return {}

def save_routes(routes:dict):
    _atomic_write(ROUTES_FILE, json.dumps(routes, ensure_ascii=False, indent=2))

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
        f"🧵 Line: <code>{line}</code>\n"
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

# =============== KOMUTLAR ===============
# Komut adı için Unicode destekli regex: /komut[@Bot]? [arg...]
CMD_RE  = re.compile(r'^/([^\s@]+)(?:@\w+)?(?:\s+(.*))?$')
LINE_RE = re.compile(r'[lL]?(\d+)')

def parse_line_spec(spec:str):
    nums = set(int(n) for n in LINE_RE.findall(spec or ""))
    return sorted(nums)

def handle_command(text:str, chat_id:str, routes:dict):
    m = CMD_RE.match(text.strip())
    if not m:
        return routes
    cmd, arg = m.groups()
    cmd = cmd.lower()

    # /start hiçbir şey yazmasın
    if cmd == "start":
        return routes

    if cmd == "whereami":
        tg_send_message(chat_id, f"🧭 <b>whereami</b>\n<code>{chat_id}</code>")
        return routes

    if cmd == "numaraver":
        if not arg:
            tg_send_message(chat_id,
                "Kullanım: /numaraver L1 L5 ...\nÖrn: /numaraver L1 L5 L7"
            )
            return routes
        lines = parse_line_spec(arg)
        if not lines:
            tg_send_message(chat_id, "Hatalı format. Örn: /numaraver L2 L3")
            return routes
        routes.setdefault(str(chat_id), [])
        for ln in lines:
            if ln not in routes[str(chat_id)]:
                routes[str(chat_id)].append(ln)
        routes[str(chat_id)] = sorted(routes[str(chat_id)])
        save_routes(routes)
        tg_send_message(chat_id, f"✅ {', '.join('L'+str(x) for x in routes[str(chat_id)])}  BU GRUBA EKLENDİ.")
        return routes

    # /kaldır alias'ları: kaldır, kaldir, iptal, sil, remove
    if cmd in {"kaldır", "kaldir", "iptal", "sil", "remove"}:
        if not arg:
            tg_send_message(chat_id,
                "Kullanım: /kaldır L5 ... veya /kaldır 5 ...\nÖrn: /kaldır L2 L3"
            )
            return routes
        lines = parse_line_spec(arg)
        if not lines:
            tg_send_message(chat_id, "Hatalı format. Örn: /kaldır L2 L3")
            return routes
        current = set(routes.get(str(chat_id), []))
        removed_any = False
        for ln in lines:
            if ln in current:
                current.remove(ln)
                removed_any = True
        routes[str(chat_id)] = sorted(current)
        save_routes(routes)
        if removed_any:
            if current:
                tg_send_message(chat_id, f"❌ Kaldırıldı. Kalan Line'lar : <code>{', '.join('L'+str(x) for x in current)}</code>")
            else:
                tg_send_message(chat_id, "❌ Tüm Numaralar kaldırıldı. Bu gruba artık SMS düşmeyecek.")
        else:
            tg_send_message(chat_id, "Belirttiğin hat(lar)  bu grupta yok.")
        return routes

    if cmd == "aktif":
        lines = routes.get(str(chat_id))
        if not lines:
            tg_send_message(chat_id, "Bu gruba şu an hiç numara verilmemiş.")
        else:
            tg_send_message(chat_id, f"Aktif Linelar: <code>{', '.join('L'+str(x) for x in lines)}</code>")
        return routes

    # bilinmeyen /komutlar için yardım
    tg_send_message(chat_id,
        "Komutlar:\n"
        "• /whereami → chat_id gösterir\n"
        "• /numaraver L1 L5 ... → hatları ekle\n"
        "• /kaldır L1 L5 ... → hatları çıkar (alias: /kaldir, /iptal, /sil, /remove)\n"
        "• /aktif → aktif hatları listele"
    )
    return routes

def poll_and_handle_updates(routes:dict) -> dict:
    updates = tg_fetch_updates(timeout=10)
    if not updates:
        return routes
    for u in updates:
        msg = u.get("message") or u.get("channel_post")
        if not msg:
            continue
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id:
            continue
        text = msg.get("text") or ""
        if not text:
            continue
        routes = handle_command(text, str(chat_id), routes)
    return routes

# =============== ROUTING ===============
def deliver_sms_to_routes(row, routes:dict):
    line = int(row['line'])
    sent_total = 0

    if not routes:
        if send_tg_formatted(CHAT_ID, row['line'], row['num'], row['content'], row['date']):
            sent_total += 1
        return sent_total

    for chat_id, lines in routes.items():
        try:
            want = line in lines
        except Exception:
            want = False
        if want:
            ok = send_tg_formatted(chat_id, row['line'], row['num'], row['content'], row['date'])
            if ok:
                sent_total += 1
            else:
                time.sleep(0.3 + random.random()*0.5)
                if send_tg_formatted(chat_id, row['line'], row['num'], row['content'], row['date']):
                    sent_total += 1
    return sent_total

# =============== MAIN LOOP ===============
def main():
    tg_delete_webhook(drop=False)

    seen = load_seen()
    routes = load_routes()
    log.info("Başladı, görülen %d kayıt | aktif grup sayısı: %d", len(seen), len(routes))

    initial_warmup_seen(seen)

    while True:
        try:
            routes = poll_and_handle_updates(routes)

            html_txt = fetch_html()
            if not html_txt:
                time.sleep(3)
                continue

            rows = parse_sms_blocks(html_txt)
            newc = 0
            routed = 0
            for row in rows:
                key = make_key(row)
                if key in seen:
                    continue
                sent = deliver_sms_to_routes(row, routes)
                if sent > 0:
                    routed += sent
                seen.add(key)
                newc += 1

            if newc:
                save_seen(seen)
                log.info("Yeni %d SMS kaydı işlendi | gönderim: %d", newc, routed)

        except requests.exceptions.ReadTimeout:
            log.warning("GoIP Read timeout — atlıyorum.")
        except requests.exceptions.RequestException as e:
            log.warning("Ağ hatası: %s", e)
        except Exception as e:
            log.warning("Hata: %s", e)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()

