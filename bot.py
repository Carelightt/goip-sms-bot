# bot.py — DİNAMİK "From:" TEMELLİ MARKA FİLTRESİ + RAPOR
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

# >>>>> YETKİLİ KULLANICILAR (EKLENDİ) <<<<<
ALLOWED_USER_IDS = {8450766241, 6672759317}

# Dosya yolları
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE     = os.path.join(BASE_DIR, "seen.json")
ROUTES_FILE   = os.path.join(BASE_DIR, "routes.json")    # { "<chat_id>": [1,5,7], ... }
FILTERS_FILE  = os.path.join(BASE_DIR, "filters.json")   # { "<chat_id>": { "<brand>":[lines] } }
REPORTS_FILE  = os.path.join(BASE_DIR, "reports.json")   # { "<chat_id>": {"total":int, "brands":{bkey:int}} }

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("goip-forwarder")

# ---- HTTP session with retry/backoff ----
def make_session() -> requests.Session:
    s = requests.Session()
    s.auth = HTTPBasicAuth(GOIP_USER, GOIP_PASS)
    s.headers.update({"User-Agent": "GoIP-SMS-Forwarder/1.0"})
    retry = Retry(
        total=3, connect=3, read=3,
        backoff_factor=0.6,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False, respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    s.mount("http://", adapter); s.mount("https://", adapter)
    return s

SESSION = make_session()

# =============== STATE ===============
def load_seen():
    if os.path.exists(SEEN_FILE):
        try: return set(json.load(open(SEEN_FILE, "r", encoding="utf-8")))
        except Exception: return set()
    return set()

def _atomic_write(path:str, data_text:str):
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=d)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data_text); f.flush()
        try: os.fsync(f.fileno())
        except Exception: pass
    try: os.replace(tmp_path, path)
    except OSError: shutil.move(tmp_path, path)

def save_seen(seen:set):
    _atomic_write(SEEN_FILE, json.dumps(list(seen), ensure_ascii=False))

def load_routes() -> dict:
    if os.path.exists(ROUTES_FILE):
        try:
            data = json.load(open(ROUTES_FILE, "r", encoding="utf-8"))
            fixed = {}
            for k, v in (data or {}).items():
                cid = str(k)
                try: cid = str(int(k))
                except Exception: pass
                lines = sorted({int(x) for x in (v or []) if isinstance(x, (int,str)) and str(x).isdigit()})
                fixed[cid] = lines
            return fixed
        except Exception as e:
            log.warning("routes.json okunamadı: %s", e)
            return {}
    return {}

def save_routes(routes:dict):
    _atomic_write(ROUTES_FILE, json.dumps(routes, ensure_ascii=False, indent=2))

def load_filters() -> dict:
    if os.path.exists(FILTERS_FILE):
        try:
            data = json.load(open(FILTERS_FILE, "r", encoding="utf-8")) or {}
            fixed = {}
            for cid, brands in data.items():
                cid = str(cid)
                fixed[cid] = {}
                for b, arr in (brands or {}).items():
                    bkey = normalize_brand_key(str(b))
                    fixed[cid][bkey] = sorted({int(x) for x in (arr or []) if str(x).isdigit()})
            return fixed
        except Exception as e:
            log.warning("filters.json okunamadı: %s", e)
            return {}
    return {}

def save_filters(flt:dict):
    _atomic_write(FILTERS_FILE, json.dumps(flt, ensure_ascii=False, indent=2))

# ---- RAPOR STATE ----
def load_reports() -> dict:
    if os.path.exists(REPORTS_FILE):
        try:
            data = json.load(open(REPORTS_FILE, "r", encoding="utf-8")) or {}
            # normalize keys to str
            fixed = {}
            for cid, obj in data.items():
                scid = str(cid)
                total = int((obj or {}).get("total", 0))
                brands = {}
                for b, n in (obj or {}).get("brands", {}).items():
                    brands[str(b)] = int(n)
                fixed[scid] = {"total": total, "brands": brands}
            return fixed
        except Exception as e:
            log.warning("reports.json okunamadı: %s", e)
            return {}
    return {}

def save_reports(rep:dict):
    _atomic_write(REPORTS_FILE, json.dumps(rep, ensure_ascii=False, indent=2))

def incr_report(rep:dict, chat_id:str, brand_key:str|None):
    chat_id = str(chat_id)
    rep.setdefault(chat_id, {"total": 0, "brands": {}})
    rep[chat_id]["total"] = rep[chat_id].get("total", 0) + 1
    bkey = normalize_brand_key(brand_key) if brand_key else "_genel"
    rep[chat_id]["brands"][bkey] = rep[chat_id]["brands"].get(bkey, 0) + 1
    save_reports(rep)

def reset_report(rep:dict, chat_id:str):
    chat_id = str(chat_id)
    rep[chat_id] = {"total": 0, "brands": {}}
    save_reports(rep)

def format_report(rep:dict, chat_id:str) -> str:
    obj = rep.get(str(chat_id)) or {"total": 0, "brands": {}}
    total = obj.get("total", 0)
    brands = obj.get("brands", {})
    # Markaları çoktan aza sırala
    items = sorted(brands.items(), key=lambda kv: kv[1], reverse=True)
    lines = [f"📊 <b>Alınan Toplam SMS :</b> <code>{total}</code>"]
    for k, n in items:
        label = "GENEL" if k == "_genel" else k.upper()
        lines.append(f"{label} : <code>{n}</code>")
    if len(lines) == 1:
        lines.append("Henüz kayıt yok.")
    return "\n".join(lines)

# =============== TELEGRAM CORE ===============
def tg_api(method, params=None, use_get=False, timeout=20):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        r = SESSION.get(url, params=params or {}, timeout=(3, timeout)) if use_get \
            else SESSION.post(url, data=params or {}, timeout=(3, timeout))
        return r
    except requests.RequestException as e:
        log.warning("TG %s network hata: %s", method, e); return None

def tg_delete_webhook(drop=False):
    r = tg_api("deleteWebhook", {"drop_pending_updates": "true" if drop else "false"})
    if not r: return False
    if r.status_code == 200:
        ok = r.json().get("ok", False); log.info("deleteWebhook ok=%s", ok); return ok
    log.warning("deleteWebhook status=%s %s", r.status_code, r.text[:200]); return False

def tg_send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True):
    r = tg_api("sendMessage", {
        "chat_id": str(chat_id), "text": text, "parse_mode": parse_mode,
        "disable_web_page_preview": "true" if disable_web_page_preview else "false",
    }, use_get=False, timeout=15)
    if not r: return False
    if r.status_code != 200:
        log.warning("Telegram hata: %s %s", r.status_code, r.text[:200]); return False
    return True

def send_tg_formatted(chat_id, line, num, content, date):
    # 3 ile 7 hane arasındaki sayıları yakala
    def repl(m):
        return f"<code>{m.group(0)}</code>"

    highlighted = re.sub(r"\b\d{3,7}\b", repl, html.escape(content))

    text = (
        f"🔔 <b>Yeni SMS</b>\n"
        f"📲 Line: <code>{line}</code>\n"
        f"👤 Gönderen: <code>{html.escape(num)}</code>\n"
        f"🕒 {html.escape(date)}\n"
        f"💬 {highlighted}"
    )
    return tg_send_message(chat_id, text)

# ---- Long-polling updates ----
UPD_OFFSET = 0
def tg_fetch_updates(timeout=20):
    global UPD_OFFSET
    r = tg_api("getUpdates", {"timeout": timeout, "offset": UPD_OFFSET}, use_get=True, timeout=timeout+5)
    if not r: return []
    if r.status_code == 409:
        log.warning("getUpdates 409: webhook aktif. Silmeyi deniyorum…")
        tg_delete_webhook(drop=False)
        r = tg_api("getUpdates", {"timeout": timeout, "offset": UPD_OFFSET}, use_get=True, timeout=timeout+5)
        if not r or r.status_code != 200:
            if r: log.warning("getUpdates retry status=%s %s", r.status_code, r.text[:200])
            return []
    if r.status_code != 200:
        log.warning("getUpdates status: %s %s", r.status_code, r.text[:200]); return []
    data = r.json()
    if not data.get("ok"): log.warning("getUpdates ok=false: %s", data); return []
    results = data.get("result", [])
    if results: UPD_OFFSET = results[-1]["update_id"] + 1
    return results

# =============== GOIP ===============
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
    """
    "sms=[ ... ]" bloklarından 'DATE,NUM,CONTENT' çıkarır.
    """
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
        log.info("Warm-up: HTML boş geldi, yine de devam."); return
    rows = parse_sms_blocks(html_txt)
    added = 0
    for row in rows:
        key = make_key(row)
        if key not in seen:
            seen.add(key); added += 1
    if added: save_seen(seen)
    log.info("Warm-up tamam: %d kayıt seen olarak işaretlendi.", added)

# --------- Marka anahtar normalizasyonu ve tespit ----------
def normalize_brand_key(s: str) -> str:
    if s is None: return ""
    # Türkçe büyük/küçük normalize + sadece harf/rakam/_ bırak
    s = s.strip()
    s = s.replace("ı","i").replace("İ","i")
    s = s.lower()
    s = re.sub(r"[^a-z0-9_]+", "", s)  # boşluk, tire vb. at
    return s

def extract_from_field(row) -> str | None:
    """
    İçerikten 'From: XXX' alanını yakala. Yoksa num alanından bir marka çıkar.
    """
    content = row.get("content","")
    num = row.get("num","")
    # 1) CONTENT içinde From: X
    m = re.search(r'(?i)\bfrom\s*[:=]\s*([^\s\]\)\(,;|]+)', content)
    if m:
        return normalize_brand_key(m.group(1))
    # 2) NUM alfanümerik ise onu marka say (örn GETIR, VAKIFBANK)
    if re.search(r'[A-Za-z]', num):
        token = re.findall(r'[A-Za-z0-9_]+', num)
        if token:
            return normalize_brand_key(token[0])
    return None

# =============== KOMUTLAR ===============
CMD_RE  = re.compile(r'^/([^\s@]+)(?:@\w+)?(?:\s+(.*))?$')
LINE_RE = re.compile(r'[lL]?(\d+)')

def parse_line_spec(spec:str):
    return sorted({int(n) for n in LINE_RE.findall(spec or "")})

def handle_command(text:str, chat_id:str, routes:dict, filters:dict, reports:dict):
    m = CMD_RE.match(text.strip())
    if not m: return routes, filters, reports
    cmd, arg = m.groups()
    cmd = cmd.lower()

    if cmd == "start":
        return routes, filters, reports

    if cmd == "whereami":
        tg_send_message(chat_id, f"🧭 <b>whereami</b>\n<code>{chat_id}</code>")
        return routes, filters, reports

    # ---- RAPOR ----
    if cmd == "rapor":
        msg = format_report(reports, chat_id)
        tg_send_message(chat_id, msg)
        return routes, filters, reports

    if cmd in {"raporsıfırla", "raporsifirla"}:
        reset_report(reports, chat_id)
        tg_send_message(chat_id, "✅ Bu grubun raporu sıfırlandı.")
        return routes, filters, reports

    # ---- Genel hat ekleme (filtre yok) ----
    if cmd == "numaraver":
        if not arg:
            tg_send_message(chat_id, "Kullanım: /numaraver L1 L5 ..."); return routes, filters, reports
        lines = parse_line_spec(arg)
        if not lines:
            tg_send_message(chat_id, "Hatalı format. Örn: /numaraver L2 L3"); return routes, filters, reports
        routes.setdefault(str(chat_id), [])
        for ln in lines:
            if ln not in routes[str(chat_id)]: routes[str(chat_id)].append(ln)
        routes[str(chat_id)] = sorted(routes[str(chat_id)]); save_routes(routes)
        tg_send_message(chat_id, f"✅ {', '.join('L'+str(x) for x in routes[str(chat_id)])} verildi ")
        return routes, filters, reports

    # ---- Dinamik marka ekleme: /<brand>ver L1 L2 ...
    def add_brand_filter(brand_raw:str, arg_str:str):
        nonlocal routes, filters
        bkey = normalize_brand_key(brand_raw)
        if not bkey:
            tg_send_message(chat_id, "Numara adı uygunsuz görünüyor."); return
        if not arg_str:
            tg_send_message(chat_id, f"Kullanım: /{bkey}ver L1 L2 ..."); return
        lines = parse_line_spec(arg_str)
        if not lines:
            tg_send_message(chat_id, "Hatalı format. Örn: L15 veya 15"); return

        # Hatları genel routes’a da ekleyelim (hat tanımlı olsun)
        routes.setdefault(str(chat_id), [])
        for ln in lines:
            if ln not in routes[str(chat_id)]: routes[str(chat_id)].append(ln)
        routes[str(chat_id)] = sorted(routes[str(chat_id)]); save_routes(routes)

        # Filtre yaz
        filters.setdefault(str(chat_id), {})
        filters[str(chat_id)].setdefault(bkey, [])
        cur = set(filters[str(chat_id)][bkey])
        for ln in lines: cur.add(ln)
        filters[str(chat_id)][bkey] = sorted(cur); save_filters(filters)

        tg_send_message(chat_id,
            f"✅ <b>{brand_raw}</b> numarası eklendi: "
            f"<code>{', '.join('L'+str(x) for x in lines)}</code>\n"
            f"Bu gruba <b>sadece {brand_raw}</b> (From:) SMS’leri düşecek."
        )

    # Genel komut: /filtrever <marka> L1 L2 ...
    if cmd == "filtrever":
        parts = (arg or "").strip().split(None, 1)
        if not parts or not parts[0]:
            tg_send_message(chat_id, "Kullanım: /filtrever <marka> L1 L2 ..."); return routes, filters, reports
        brand = parts[0]; rest = parts[1] if len(parts) > 1 else ""
        add_brand_filter(brand, rest); return routes, filters, reports

    # Dinamik: /xxxver
    if cmd.endswith("ver") and len(cmd) > 3:
        brand = cmd[:-3]  # 'getirver' -> 'getir'
        add_brand_filter(brand, arg or ""); return routes, filters, reports

    # ---- Kaldır (hat) ----
    if cmd in {"kaldır","kaldir","iptal","sil","remove"}:
        if not arg or not arg.strip():
            tg_send_message(chat_id, "Kullanım: /kaldır L2 L3 veya /kaldır hepsi")
            return routes, filters, reports

        arg_norm = arg.strip().lower()
        # hepsi / hepsini / tüm / tum -> hepsini kaldır
        if arg_norm in {"hepsi", "hepsini", "tüm", "tum"}:
            # Tüm line’ları kaldır
            routes[str(chat_id)] = []
            save_routes(routes)
            # Bu gruba ait TÜM filtreleri kaldır
            if str(chat_id) in filters:
                del filters[str(chat_id)]
            save_filters(filters)

            tg_send_message(chat_id, "Bütün numaralar bu gruptan kaldırıldı ✅")
            return routes, filters, reports

        # --- tek tek line kaldırma (eski davranış aynen devam) ---
        lines = parse_line_spec(arg)
        if not lines:
            tg_send_message(chat_id, "Hatalı format. Örn: /kaldır L2 L3 veya /kaldır hepsi")
            return routes, filters, reports

        current = set(routes.get(str(chat_id), []))
        removed_any = False
        for ln in lines:
            if ln in current:
                current.remove(ln)
                removed_any = True
        routes[str(chat_id)] = sorted(current)
        save_routes(routes)

        # Filtrelerden de bu hatları temizle
        if str(chat_id) in filters:
            for b in list(filters[str(chat_id)].keys()):
                arr = set(filters[str(chat_id)].get(b, []))
                filters[str(chat_id)][b] = sorted(x for x in arr if x not in lines)
            save_filters(filters)

        if removed_any:
            msg = (
                f"❌ Kaldırıldı. Kalan Line'lar: <code>{', '.join('L'+str(x) for x in current)}</code>"
                if current else "❌ Tüm hatlar kaldırıldı. Bu gruba artık SMS düşmeyecek."
            )
            tg_send_message(chat_id, msg)
        else:
            tg_send_message(chat_id, " Belirttiğin hatlar zaten bu grupta yok.")
        return routes, filters, reports

    # ---- AKTİF ----
    if cmd == "aktif":
        lines = routes.get(str(chat_id)) or []
        flt = filters.get(str(chat_id)) or {}
        parts = [f"Aktif Line'lar: <code>{'Yok' if not lines else ', '.join('L'+str(x) for x in lines)}</code>"]
        if flt:
            pr = []
            for b, arr in flt.items():
                if arr: pr.append(f"• {b}: <code>{', '.join('L'+str(x) for x in arr)}</code>")
            parts.append(" Filtreler:\n" + ("\n".join(pr) if pr else "Yok"))
        else:
            parts.append(" Filtreler: Yok")
        tg_send_message(chat_id, "\n".join(parts)); return routes, filters, reports

    # Yardım
    tg_send_message(chat_id,
        "Komutlar:\n"
        "• /whereami → chat_id gösterir\n"
        "• /numaraver L1 L5 ... → hatları ekle (tüm SMS’ler)\n"
        "• /filtrever <marka> L1 L2 ... → From: <marka> için filtre koy\n"
        "• /<marka>ver L1 L2 ... → kısa yol (örn: /getirver 1, /vakifbankver 15)\n"
        "• /kaldır L1 L5 ... → hatları çıkar (alias: /kaldir, /iptal, /sil, /remove)\n"
        "• /aktif → aktif hatlar ve filtreleri listele\n"
        "• /rapor → bu grubun SMS raporu\n"
        "• /raporsıfırla → bu grubun raporunu sıfırla"
    )
    return routes, filters, reports

def poll_and_handle_updates(routes:dict, filters:dict, reports:dict):
    updates = tg_fetch_updates(timeout=10)
    if not updates: 
        return routes, filters, reports

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

        # ❗ Komut değilse (başında / yoksa) direkt atla
        if not text.strip().startswith("/"):
            continue

        # ✅ Komut ise yetki kontrolü
        sender = msg.get("from") or {}
        user_id = sender.get("id")
        if user_id not in ALLOWED_USER_IDS:
            tg_send_message(chat_id, "Hakkınız yoktur. Destek için : @tonymonntnal @CengizzAtay")
            continue

        routes, filters, reports = handle_command(text, str(chat_id), routes, filters, reports)

    return routes, filters, reports

# =============== ROUTING ===============
def _is_allowed_for_chat_line(chat_id:str, line:int, brand_key:str|None, filters:dict) -> bool:
    """
    Bu chat/line için bir veya daha fazla marka filtresi tanımlıysa,
    sadece o markalarla eşleşenler geçer. Filtre yoksa serbest.
    """
    cflt = filters.get(str(chat_id)) or {}
    # Bu hat için atanmış marka var mı?
    active_marks_for_line = [b for b, arr in cflt.items() if line in (arr or [])]
    if not active_marks_for_line:
        return True  # filtre yok -> serbest
    if not brand_key:
        return False
    # normalize
    brand_key = normalize_brand_key(brand_key)
    return brand_key in active_marks_for_line

def detect_brand_key(row) -> str | None:
    """
    From: alanından veya num’dan normalize edilmiş marka anahtarı üretir.
    """
    b = extract_from_field(row)
    return b

def deliver_sms_to_routes(row, routes:dict, filters:dict, reports:dict):
    line = int(row['line'])
    brand_key = detect_brand_key(row)  # ör: "getir", "yemeksepeti", "vakifbank" ...
    sent_total = 0

    if not routes:
        if _is_allowed_for_chat_line(str(CHAT_ID), line, brand_key, filters):
            if send_tg_formatted(CHAT_ID, row['line'], row['num'], row['content'], row['date']):
                # Rapor: fallback CHAT_ID'ye de yazalım
                incr_report(reports, str(CHAT_ID), brand_key)
                sent_total += 1
        return sent_total

    for chat_id, lines in routes.items():
        want = line in (lines or [])
        if not want: continue
        if not _is_allowed_for_chat_line(chat_id, line, brand_key, filters):
            continue
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
                log.info("Yeni %d SMS işlendi | gönderim: %d", newc, routed)
        except requests.exceptions.ReadTimeout:
            log.warning("GoIP Read timeout — atlıyorum.")
        except requests.exceptions.RequestException as e:
            log.warning("Ağ hatası: %s", e)
        except Exception as e:
            log.warning("Hata: %s", e)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
