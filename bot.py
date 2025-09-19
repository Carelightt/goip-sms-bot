# bot.py
import os, re, time, json, html, logging, requests, tempfile, random, shutil
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, urljoin

# ==== AYARLAR ====
GOIP_URL  = "http://5.11.128.154:5050/default/en_US/tools.html?type=sms_inbox"
GOIP_USER = "user"
GOIP_PASS = "1010"

BOT_TOKEN = "8299573802:AAFrTWxpx2JuJgv2vsVZ4r2NMTT4B16KMZg"
# CHAT_ID   = -1003081296225  # ❌ kaldırıldı

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
def fetch_html(url=None):
    url = url or GOIP_URL
    r = SESSION.get(url, timeout=(3, 6))
    if r.status_code == 200:
        return r.text
    log.warning("GoIP HTTP durum kodu: %s (%s)", r.status_code, url)
    return ""

def detect_max_lines(html_text:str) -> int:
    nums = [int(x) for x in re.findall(r'Line\s+(\d+)', html_text)]
    return max(nums) if nums else 16

def _with_query(url:str, **kw):
    pr = urlparse(url)
    q = parse_qs(pr.query)
    for k, v in kw.items():
        q[str(k)] = [str(v)]
    nq = urlencode({k: v[0] for k, v in q.items()}, doseq=False)
    return urlunparse((pr.scheme, pr.netloc, pr.path, pr.params, nq, pr.fragment))

def fetch_line_page(line:int) -> str:
    """
    GoIP32 arayüzünde radio butonlar XHR ile içerik getiriyor.
    Burada bilinen endpoint varyantlarını sırayla deneriz; SMS izi bulunursa döneriz.
    """
    base = GOIP_URL
    tools_url = urljoin(base, "tools.html")
    candidates = [
        _with_query(base, type="sms_inbox", line=line),
        _with_query(base, line=line),
        _with_query(base, type="sms_inbox", ajax=1, line=line),
        f"{tools_url}?type=sms_inbox&line={line}",
        f"{tools_url}?type=sms_inbox&ajax=1&line={line}",
        urljoin(base, f"ajax_sms_inbox.html?line={line}"),
        urljoin(base, f"ajax_sms_store.html?line={line}"),
        urljoin(base, f"sms_inbox.html?line={line}"),
    ]
    seen = set()
    for u in candidates:
        if u in seen:
            continue
        seen.add(u)
        html_txt = fetch_html(u)
        if not html_txt:
            continue
        if re.search(r'sms=\s*\[', html_txt) or re.search(r'Time:\s*\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s*From:', html_txt):
            return html_txt
    return ""

def iter_inbox_pages():
    """
    GoIP16: tek sayfada gömülü JS blokları bulunur.
    GoIP32: her line için XHR ile içerik gelir -> fetch_line_page ile her line ayrı çekilir.
    DÖNÜŞ: [(assumed_line:int|None, html:str), ...]
    """
    pages = []
    base_html = fetch_html(GOIP_URL)
    if not base_html:
        return pages

    # Ana sayfayı da (GoIP16 uyum) ekle
    pages.append((None, base_html))

    # Sayfadaki butonlardan max line tespit edip tek tek çek
    max_lines = detect_max_lines(base_html)
    for ln in range(1, max_lines + 1):
        html_txt = fetch_line_page(ln)
        if html_txt:
            pages.append((ln, html_txt))
    return pages

def parse_sms_blocks(html_text:str):
    """
    Eski arayüz (GoIP16) için: JS dizisi ve sms_row_insert(...) şeklindeki gömülü veri.
    """
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

def parse_sms_blocks_fallback_timefrom(html_text:str, assumed_line:int|None=None):
    """
    Yeni arayüz (GoIP32) için: "Time:.. From:.. <mesaj>" şeklindeki düz metin/tabela.
    """
    txt = re.sub(r'<[^>]+>', '\n', html_text)
    txt = re.sub(r'\r', '', txt)
    txt = re.sub(r'[ \t]+', ' ', txt)
    txt = re.sub(r'\n\s*\n+', '\n', txt).strip()

    results = []
    patt = re.compile(
        r'Time:\s*(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*From:\s*([^\s]+)\s*(.+?)(?=Time:|\Z)',
        re.S
    )
    for m in patt.finditer(txt):
        date, num, content = m.groups()
        results.append({
            "line": (assumed_line if assumed_line is not None else -1),
            "date": date.strip(),
            "num":  num.strip(),
            "content": content.strip(),
        })
    return results

def parse_any_sms_blocks(html_text:str, assumed_line:int|None=None):
    rows = parse_sms_blocks(html_text)  # GoIP16 yolu
    if rows:
        return rows
    return parse_sms_blocks_fallback_timefrom(html_text, assumed_line=assumed_line)

# =============== HELPERS ===============
def _norm(s: str) -> str:
    s = s.replace("\r", "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def make_key(row) -> str:
    return f"{row['line']}::{_norm(row.get('date',''))}::{_norm(row.get('num',''))}::{_norm(row.get('content',''))}"

def initial_warmup_seen(seen:set):
    pages = iter_inbox_pages()
    if not pages:
        log.info("Warm-up: HTML boş geldi, yine de devam.")
        return
    added = 0
    for assumed_line, html_txt in pages:
        rows = parse_any_sms_blocks(html_txt, assumed_line=assumed_line)
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

# =============== KOMUTLAR ===============
LINE_RE = re.compile(r'[lL]?(\d+)')
ALLOWED_CHAT_TYPES = {"group", "supergroup"}  # sadece gruplarda komut çalışsın

def parse_line_spec(spec:str):
    nums = set(int(n) for n in LINE_RE.findall(spec or ""))
    return sorted(nums)

def handle_command(text:str, chat_id:str, routes:dict, chat_type:str, user_id:int|None):
    # SADECE OWNER KOMUT ÇALIŞTIRABİLİR
    if user_id != OWNER_ID:
        tg_send_message(chat_id, DENY_MSG)
        if str(chat_id) in routes:
            routes.pop(str(chat_id), None)
            save_routes(routes)
        return routes

    if chat_type not in ALLOWED_CHAT_TYPES:
        tg_send_message(chat_id, DENY_MSG)
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

    # 👇 YENİ: /grupaç (alias /grupac) — grubu aç, routes.json oluştur
    if cmd in {"grupaç", "grupac"}:
        routes.setdefault(str(chat_id), set())
        save_routes(routes)
        tg_send_message(chat_id, "✅ Bu grup açıldı. Şimdi /numaraver 1 2 3 ... ile hatları tanımlayabilirsin.")
        return routes

    if cmd == "numaraver":
        if not arg:
            tg_send_message(chat_id, "Kullanım: /numaraver 1 5 ...  (L1 L5 de olur)")
            return routes
        lines = parse_line_spec(arg)
        if not lines:
            tg_send_message(chat_id, "Hatalı format. Örn: /numaraver 2 3  veya  /numaraver L2 L3")
            return routes
        routes.setdefault(str(chat_id), set())
        for ln in lines:
            routes[str(chat_id)].add(ln)
        save_routes(routes)
        tg_send_message(chat_id, f"✅ {', '.join('L'+str(x) for x in sorted(routes[str(chat_id)]))}  BU GRUBA EKLENDİ.")
        return routes

    if cmd in {"kaldır", "kaldir", "iptal", "sil", "remove"}:
        if not arg:
            tg_send_message(chat_id, "Kullanım: /kaldır 5 ...  veya  /kaldır hepsi")
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
            tg_send_message(chat_id, "Hatalı format. Örn: /kaldır 2 3  veya  /kaldır L2 L3")
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
            tg_send_message(chat_id, "Bu gruba şu an hiç numara verilmemiş. Önce /grupaç, sonra /numaraver ...")
        else:
            tg_send_message(chat_id, f"Aktif Linelar: <code>{', '.join('L'+str(x) for x in sorted(lines))}</code>")
        return routes

    tg_send_message(chat_id,
        "Komutlar:\n"
        "• /grupaç → grubu açar (routes.json oluşturur)\n"
        "• /whereami → chat_id gösterir\n"
        "• /numaraver 1 5 ... → hatları ekle (L1 L5 de olur)\n"
        "• /kaldır 1 5 ... → hatları çıkar  |  /kaldır hepsi\n"
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
        from_user = msg.get("from") or {}
        try:
            user_id = int(from_user.get("id")) if from_user.get("id") is not None else None
        except Exception:
            user_id = None
        routes = handle_command(text, str(chat_id), routes, chat_type, user_id)
    return routes

# =============== ROUTING ===============
def deliver_sms_to_routes(row, routes:dict):
    line = int(row['line'])
    sent_total = 0

    for chat_id, lines in routes.items():
        try:
            if int(chat_id) > 0:
                continue
        except Exception:
            continue

        want = line in lines
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

            pages = iter_inbox_pages()
            if not pages:
                time.sleep(3)
                continue

            newc = 0
            routed = 0
            for assumed_line, html_txt in pages:
                rows = parse_any_sms_blocks(html_txt, assumed_line=assumed_line)
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
        except requests.RequestException as e:
            log.warning("Ağ hatası: %s", e)
        except Exception as e:
            log.warning("Hata: %s", e)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
