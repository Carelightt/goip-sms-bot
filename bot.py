# bot.py
import os, re, time, json, html, logging, requests, tempfile, random, shutil, datetime
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==== AYARLAR ====
# --- GOIP (HTML) kaynağı (geri uyumluluk) ---
GOIP_URL   = os.getenv("GOIP_URL",  "http://5.11.128.154:5050/default/en_US/tools.html?type=sms_inbox")
GOIP_USER  = os.getenv("GOIP_USER", "user")
GOIP_PASS  = os.getenv("GOIP_PASS", "1010")

# --- PERSEPHONE (JSON) kaynağı ---
# Örn: https://persephone.domain/api/sms?limit=200   (tam endpointini buraya yaz)
PERSEPHONE_URL   = os.getenv("PERSEPHONE_URL", "").strip()      # set edilirse bu kullanılır
PERSEPHONE_TOKEN = os.getenv("PERSEPHONE_TOKEN", "").strip()    # "Bearer xxx" gerekiyorsa

# Telegram
BOT_TOKEN = "8299573802:AAFrTWxpx2JuJgv2vsVZ4r2NMTT4B16KMZg"
CHAT_ID   = -1003098321304  # (GERİYE UYUMLULUK) routes.json boşsa buraya gönderilir

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
    # GOIP HTML Basic Auth gerekebiliyor; Persephone genelde Bearer ile gider (aşağıda header ekleyeceğiz)
    s.auth = HTTPBasicAuth(GOIP_USER, GOIP_PASS)
    s.headers.update({"User-Agent": "SMS-Forwarder/2.0"})
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.7,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=6, pool_maxsize=12)
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

# =============== KAYNAK SEÇİMİ ===============
def _active_source():
    """
    PERSEPHONE_URL set ise PERSEPHONE_JSON, yoksa GOIP_HTML
    """
    return "PERSEPHONE_JSON" if PERSEPHONE_URL else "GOIP_HTML"

# =============== PERSEPHONE JSON ===============
def _persephone_headers():
    h = {}
    if PERSEPHONE_TOKEN:
        # Eğer "Bearer " ekli değilse otomatik ekleyelim
        if PERSEPHONE_TOKEN.lower().startswith("bearer "):
            h["Authorization"] = PERSEPHONE_TOKEN
        else:
            h["Authorization"] = f"Bearer {PERSEPHONE_TOKEN}"
    return h

def fetch_persephone():
    """
    Beklenen dönüş: list[dict] veya {"data":[...]} şeklinde olabilir.
    Her kayıt şu alanlardan biriyle eşleşir:
      line:  line|sim|slot|channel|port
      num:   num|msisdn|from|sender
      text:  content|text|message
      date:  date|time|timestamp|recv_time
    """
    try:
        r = SESSION.get(PERSEPHONE_URL, headers=_persephone_headers(), timeout=(3, 10))
    except requests.RequestException as e:
        log.warning("Persephone erişim hatası: %s", e)
        return []

    if r.status_code != 200:
        log.warning("Persephone HTTP %s: %s", r.status_code, r.text[:200])
        return []

    try:
        data = r.json()
    except Exception as e:
        log.warning("Persephone JSON parse hata: %s", e)
        return []

    # data array olabilir veya {'data': [...]}
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
        items = data["data"]
    elif isinstance(data, list):
        items = data
    else:
        # başka bir sarmal yapıdaysa kaba bir çıkarım daha deneyelim
        for k, v in (data.items() if isinstance(data, dict) else []):
            if isinstance(v, list):
                items = v
                break
        else:
            items = []

    results = []
    for it in items:
        if not isinstance(it, dict):
            continue
        # line
        line = it.get("line", it.get("sim", it.get("slot", it.get("channel", it.get("port", None)))))
        # num
        num  = it.get("num", it.get("msisdn", it.get("from", it.get("sender", ""))))
        # content
        content = it.get("content", it.get("text", it.get("message", "")))
        # date/time
        date = it.get("date", it.get("time", it.get("timestamp", it.get("recv_time", ""))))

        # line integer'a zorla
        try:
            if isinstance(line, str) and line.lower().startswith("l"):
                line = int(re.sub(r"[^0-9]", "", line))
            else:
                line = int(line)
        except Exception:
            # bazı sistemler 1-index değil 0-index olabilir: yine de int değilse atla
            continue

        # tarih normalize: epoch ise stringe çevir
        if isinstance(date, (int, float)):
            try:
                date = datetime.datetime.fromtimestamp(float(date)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                date = str(date)
        else:
            date = str(date or "")

        num = str(num or "")
        content = str(content or "")

        results.append({
            "line": line,
            "date": date.strip(),
            "num":  num.strip(),
            "content": content.strip(),
        })
    return results

# =============== GOIP HTML ===============
def fetch_goip_html():
    try:
        r = SESSION.get(GOIP_URL, timeout=(3, 8))
    except requests.RequestException as e:
        log.warning("GoIP erişim hatası: %s", e)
        return ""
    if r.status_code == 200:
        return r.text
    log.warning("GoIP HTTP durum kodu: %s", r.status_code)
    return ""

def parse_goip_sms(html_text:str):
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

# =============== ORTAK KATMAN ===============
def fetch_messages():
    """
    Aktif kaynaktan normalize edilmiş list[ {line,date,num,content} ] döner.
    """
    if _active_source() == "PERSEPHONE_JSON":
        return fetch_persephone()
    else:
        html_txt = fetch_goip_html()
        if not html_txt:
            return []
        return parse_goip_sms(html_txt)

# =============== HELPERS ===============
def _norm(s: str) -> str:
    s = (s or "").replace("\r", "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def make_key(row) -> str:
    return f"{row['line']}::{_norm(row.get('date',''))}::{_norm(row.get('num',''))}::{_norm(row.get('content',''))}"

def initial_warmup_seen(seen:set):
    rows = fetch_messages()
    if not rows:
        log.info("Warm-up: veri yok (kaynak: %s).", _active_source())
        return
    added = 0
    for row in rows:
        key = make_key(row)
        if key not in seen:
            seen.add(key)
            added += 1
    if added:
        save_seen(seen)
    log.info("Warm-up tamam: %d kayıt seen olarak işaretlendi. (kaynak: %s)", added, _active_source())

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

# =============== KOMUTLAR ===============
LINE_RE = re.compile(r'[lL]?(\d+)')
ALLOWED_CHAT_TYPES = {"group", "supergroup"}  # güvenlik: sadece gruplarda komut

def parse_line_spec(spec:str):
    nums = set(int(n) for n in LINE_RE.findall(spec or ""))
    return sorted(nums)

def _probe_source():
    src = _active_source()
    if src == "PERSEPHONE_JSON":
        t0 = time.time()
        try:
            r = SESSION.get(PERSEPHONE_URL, headers=_persephone_headers(), timeout=(3, 10))
            dt = time.time() - t0
            if r.status_code == 200:
                try:
                    _ = r.json()
                    return f"✅ PERSEPHONE_JSON OK ({dt:.2f}s)\nURL: {PERSEPHONE_URL}"
                except Exception as e:
                    return f"⚠️ PERSEPHONE_JSON JSON parse: {e}\nURL: {PERSEPHONE_URL}"
            else:
                return f"❌ PERSEPHONE_JSON HTTP {r.status_code}\nURL: {PERSEPHONE_URL}\nBody: {r.text[:200]}"
        except Exception as e:
            return f"❌ PERSEPHONE_JSON erişim hatası: {e}\nURL: {PERSEPHONE_URL}"
    else:
        t0 = time.time()
        try:
            r = SESSION.get(GOIP_URL, timeout=(3, 8))
            dt = time.time() - t0
            if r.status_code == 200 and r.text:
                # basit bir imza arayalım
                ok = "sms_row_insert" in r.text or "sms=" in r.text
                return f"{'✅' if ok else '⚠️'} GOIP_HTML ({dt:.2f}s) URL: {GOIP_URL}  (imza={'var' if ok else 'yok'})"
            else:
                return f"❌ GOIP_HTML HTTP {r.status_code}\nURL: {GOIP_URL}\nBody: {r.text[:200]}"
        except Exception as e:
            return f"❌ GOIP_HTML erişim hatası: {e}\nURL: {GOIP_URL}"

def handle_command(text:str, chat_id:str, routes:dict, chat_type:str):
    if chat_type not in ALLOWED_CHAT_TYPES:
        tg_send_message(chat_id, "⛔ Hakkınız yok. Destek: @CengizzAtay")
        if str(chat_id) in routes:
            routes.pop(str(chat_id), None)
            save_routes(routes)
        return routes

    m = re.match(r'^/([^\s@]+)(?:@\w+)?(?:\s+(.*))?$', text.strip())
    if not m:
        return routes
    cmd, arg = m.groups()
    cmd = cmd.lower()

    if cmd == "start":
        return routes

    if cmd == "whereami":
        tg_send_message(chat_id, f"🧭 <b>whereami</b>\n<code>{chat_id}</code>")
        return routes

    if cmd == "source":
        tg_send_message(chat_id, f"🔌 <b>Aktif kaynak:</b> <code>{_active_source()}</code>")
        return routes

    if cmd == "probe":
        report = _probe_source()
        tg_send_message(chat_id, f"<b>PROBE</b>\n{html.escape(report)}")
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
        routes.setdefault(str(chat_id), set())
        for ln in lines:
            routes[str(chat_id)].add(ln)
        save_routes(routes)
        tg_send_message(chat_id, f"✅ {', '.join('L'+str(x) for x in sorted(routes[str(chat_id)]))}  BU GRUBA EKLENDİ.")
        return routes

    if cmd in {"kaldır", "kaldir", "iptal", "sil", "remove"}:
        if not arg:
            tg_send_message(chat_id,
                "Kullanım:\n"
                "• /kaldır L5 ... veya /kaldır 5 ...\n"
                "• /kaldır hepsi → tüm hatları siler"
            )
            return routes

        arg = arg.strip()
        if arg.lower() in {"hepsi", "all"}:
            if str(chat_id) in routes and routes[str(chat_id)]:
                routes.pop(str(chat_id), None)
                save_routes(routes)
                tg_send_message(chat_id, "❌ Tüm Numaralar kaldırıldı. Bu gruba artık SMS düşmeyecek.")
            else:
                tg_send_message(chat_id, "ℹ️ Zaten hiç hat opsiyonlu değil.")
            return routes

        lines = parse_line_spec(arg)
        if not lines:
            tg_send_message(chat_id, "Hatalı format. Örn: /kaldır L2 L3 veya /kaldır hepsi")
            return routes
        current = set(routes.get(str(chat_id), []))
        removed_any = False
        for ln in lines:
            if ln in current:
                current.remove(ln)
                removed_any = True
        if current:
            routes[str(chat_id)] = current
        else:
            routes.pop(str(chat_id), None)
        save_routes(routes)
        if removed_any:
            if current:
                tg_send_message(chat_id, f"❌ Kaldırıldı. Kalan Line'lar : <code>{', '.join('L'+str(x) for x in sorted(current))}</code>")
            else:
                tg_send_message(chat_id, "❌ Tüm Numaralar kaldırıldı. Bu gruba artık SMS düşmeyecek.")
        else:
            tg_send_message(chat_id, "Belirttiğin hat(lar) bu grupta yok.")
        return routes

    if cmd == "aktif":
        lines = routes.get(str(chat_id))
        if not lines:
            tg_send_message(chat_id, "Bu gruba şu an hiç numara verilmemiş.")
        else:
            tg_send_message(chat_id, f"Aktif Linelar: <code>{', '.join('L'+str(x) for x in sorted(lines))}</code>")
        return routes

    tg_send_message(chat_id,
        "Komutlar:\n"
        "• /whereami → chat_id gösterir\n"
        "• /source → aktif veri kaynağını gösterir\n"
        "• /probe → kaynak bağlantı test raporu\n"
        "• /numaraver L1 L5 ... → hatları ekle\n"
        "• /kaldır L1 L5 ... → hatları çıkar (alias: /kaldir, /iptal, /sil, /remove)\n"
        "• /kaldır hepsi → tüm hatları sıfırlar\n"
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
        chat_type = chat.get("type", "")
        if not chat_id:
            continue
        text = msg.get("text") or ""
        if not text:
            continue
        routes = handle_command(text, str(chat_id), routes, chat_type)
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
        # özel sohbet güvenliği: pozitif id (private) ise asla gönderme
        try:
            if int(chat_id) > 0:
                continue
        except Exception:
            continue

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
    log.info("Başladı, görülen %d kayıt | aktif grup sayısı: %d | kaynak: %s", len(seen), len(routes), _active_source())

    initial_warmup_seen(seen)

    while True:
        try:
            routes = poll_and_handle_updates(routes)

            rows = fetch_messages()
            if not rows:
                time.sleep(3)
                continue

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
            log.warning("Read timeout — atlıyorum.")
        except requests.RequestException as e:
            log.warning("Ağ hatası: %s", e)
        except Exception as e:
            log.warning("Hata: %s", e)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
