# bot.py — GoIP SMS -> Telegram (komutlu abonelik: /numaraver, /abonelik)

import os, re, time, json, html, logging, requests
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============== AYARLAR ==============
GOIP_URL  = "http://5.11.128.154:6060/default/en_US/tools.html?type=sms_inbox"
GOIP_USER = "user"
GOIP_PASS = "9090"

BOT_TOKEN = "8468644425:AAGJq2zEJiOSvrox8uqMv1VePrD9VsnrmDs"
ADMIN_ID  = 6672759317            # sadece sen
MAX_LINE  = 16

# İstersen referans olsun diye buraya grubunu yaz (zorunlu değil)
GROUP_ID_HINT = -1002951199599

POLL_INTERVAL = 10               # GoIP sorgu aralığı (sn)
SEND_DELAY    = 0.3              # Telegram rate-limit'e karşı minik gecikme
SEEN_FILE     = "seen.json"      # görülen kayıt fingerprintleri
SUB_FILE      = "subscriptions.json"  # {chat_id:[lines]}
UPD_FILE      = "updates.offset" # Telegram getUpdates offset

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("goip-forwarder")

# ===== HTTP Session (retry/backoff) =====
def make_session() -> requests.Session:
    s = requests.Session()
    s.auth = HTTPBasicAuth(GOIP_USER, GOIP_PASS)
    s.headers.update({"User-Agent": "GoIP-SMS-Forwarder/1.1"})
    retry = Retry(
        total=3, connect=3, read=3,
        backoff_factor=0.6,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

SESSION = make_session()

# ===== Kalıcı dosyalar =====
def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        try: return set(json.load(open(SEEN_FILE, "r", encoding="utf-8")))
        except: return set()
    return set()

def save_seen(seen:set):
    json.dump(list(seen), open(SEEN_FILE, "w", encoding="utf-8"), ensure_ascii=False)

def load_subs() -> dict:
    if os.path.exists(SUB_FILE):
        try: return json.load(open(SUB_FILE, "r", encoding="utf-8"))
        except: return {}
    return {}

def save_subs(subs:dict):
    json.dump(subs, open(SUB_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def load_offset() -> int:
    if os.path.exists(UPD_FILE):
        try: return int(open(UPD_FILE, "r", encoding="utf-8").read().strip() or "0")
        except: return 0
    return 0

def save_offset(offset:int):
    open(UPD_FILE, "w", encoding="utf-8").write(str(offset))

# ===== GoIP fetch/parse =====
def fetch_html() -> str:
    r = SESSION.get(GOIP_URL, timeout=(3, 6))
    if r.status_code == 200:
        return r.text
    log.warning("GoIP HTTP durum kodu: %s", r.status_code)
    return ""

def parse_sms_blocks(html_text:str):
    """
    Sayfadaki JS blokları:
    sms = ["MM-DD HH:MM:SS,NUM,CONTENT", ...];
    pos=...;
    sms_row_insert(lX_sms_store, sms, pos, LINE)
    """
    results=[]
    for m in re.finditer(r'sms=\s*\[(.*?)\];\s*pos=(\d+);\s*sms_row_insert\(.*?(\d+)\)', html_text, flags=re.S):
        arr_str, _pos, line = m.groups()
        line = int(line)
        msgs = re.findall(r'"([^"]*)"', arr_str)
        for raw in msgs:
            raw = (raw or "").strip()
            if not raw: 
                continue
            parts = raw.split(",", 2)
            if len(parts) < 3:
                continue
            date, num, content = parts
            results.append({
                "line": line,
                "date": date.strip(),
                "num": (num or "").strip(),
                "content": (content or "").strip()
            })
    return results

# ===== Normalize & fingerprint =====
def _norm(s: str) -> str:
    s = (s or "").replace("\r", "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def make_key(row) -> str:
    return f"{row['line']}::{_norm(row.get('date'))}::{_norm(row.get('num'))}::{_norm(row.get('content'))}"

# ===== OTP çıkarma =====
def extract_code(msg: str):
    m = re.search(r'\b(\d{4,8})\b', msg or "")
    return m.group(1) if m else None

# ===== Telegram util =====
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def tg_send_message(chat_id, text, parse_mode="HTML"):
    try:
        r = requests.post(f"{API}/sendMessage",
                          data={"chat_id": str(chat_id),
                                "text": text,
                                "parse_mode": parse_mode,
                                "disable_web_page_preview": True},
                          timeout=15)
        if r.status_code != 200:
            log.warning("Telegram hata: %s %s", r.status_code, r.text[:200])
        return r
    except Exception as e:
        log.warning("Telegram gönderim hatası: %s", e)

def format_sms(line, num, content, date):
    txt = (
        f"📩 <b>Yeni SMS</b>\n"
        f"🧵 <b>Line:</b> <code>{line}</code>\n"
        f"👤 <b>Gönderen:</b> <code>{html.escape(num)}</code>\n"
        f"🕒 {html.escape(date)}\n"
        f"💬 <code>{html.escape(content)}</code>"
    )
    code = extract_code(content)
    if code:
        txt += f"\n\n🔢 <b>KOD:</b>\n<pre>{code}</pre>"
    return txt

# ===== Abonelik tabanlı gönderim =====
def send_to_subscribers(subs:dict, line:int, num:str, content:str, date:str):
    payload = format_sms(line, num, content, date)
    delivered = 0
    for chat_id, lines in subs.items():
        try:
            if line in lines:
                tg_send_message(chat_id, payload)
                delivered += 1
                time.sleep(SEND_DELAY)
        except Exception as e:
            log.warning("Gönderim hatası chat %s: %s", chat_id, e)
    return delivered

# ===== İlk açılış WARM-UP =====
def initial_warmup_seen(seen:set):
    html = fetch_html()
    if not html:
        log.info("Warm-up: HTML boş geldi, yine de devam.")
        return
    rows = parse_sms_blocks(html)
    added = 0
    for row in rows:
        key = make_key(row)
        if key not in seen:
            seen.add(key)
            added += 1
    if added:
        save_seen(seen)
    log.info("Warm-up tamam: %d kayıt seen olarak işaretlendi.", added)

# ===== /numaraver komutu (admin-only) =====
def parse_lines_arg(arg:str):
    """
    Girdi örnekleri:
      L1-L5
      L2,L4,L7
      L1-L3,L6,L10-L12
    """
    arg = (arg or "").upper().replace(" ", "")
    targets = set()
    for token in arg.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            a,b = token.split("-",1)
            a = int(a.replace("L",""))
            b = int(b.replace("L",""))
            for x in range(min(a,b), max(a,b)+1):
                if 1 <= x <= MAX_LINE:
                    targets.add(x)
        else:
            x = int(token.replace("L",""))
            if 1 <= x <= MAX_LINE:
                targets.add(x)
    return sorted(targets)

def poll_updates_and_handle(subs:dict, last_offset:int) -> int:
    """
    Telegram getUpdates ile komutları dinler.
    Sadece ADMIN_ID yetkili.
    Komutlar: /numaraver <aralık|liste>  ,  /abonelik
    """
    try:
        r = requests.get(f"{API}/getUpdates", params={"timeout": 20, "offset": last_offset+1}, timeout=30)
        data = r.json()
    except Exception as e:
        log.warning("getUpdates hatası: %s", e)
        return last_offset

    if not data.get("ok"):
        return last_offset

    for upd in data.get("result", []):
        last_offset = max(last_offset, upd.get("update_id", last_offset))

        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue

        chat_id = str(msg["chat"]["id"])
        from_id = msg["from"]["id"]
        text    = (msg.get("text") or "").strip()

        if not text.startswith("/"):
            continue

        # Sadece admin
        if from_id != ADMIN_ID:
            tg_send_message(chat_id, "⛔ Bu komutu kullanma yetkin yok.")
            continue

        if text.startswith("/numaraver"):
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                tg_send_message(chat_id, "Kullanım: <code>/numaraver L1-L5</code> veya <code>/numaraver L2,L4,L7</code>")
                continue
            wanted = parse_lines_arg(parts[1])
            if not wanted:
                tg_send_message(chat_id, "⚠️ Geçersiz line. Örn: <code>/numaraver L1-L5</code>")
                continue
            subs[chat_id] = wanted
            save_subs(subs)
            tg_send_message(chat_id, f"✅ Bu grup artık Line {wanted} aboneliğine sahip.")
            continue

        if text.startswith("/abonelik"):
            current = subs.get(chat_id, [])
            tg_send_message(chat_id, f"📌 Bu grubun abonelikleri: {current if current else 'Yok'}")
            continue

    return last_offset

# ===== Main loop =====
def main():
    seen = load_seen()
    subs = load_subs()
    offset = load_offset()

    log.info("Başladı, görülen %d kayıt", len(seen))
    initial_warmup_seen(seen)

    while True:
        try:
            # 1) Telegram komutlarını dinle
            offset = poll_updates_and_handle(subs, offset)
            save_offset(offset)

            # 2) GoIP'ten SMS çek
            html = fetch_html()
            if not html:
                time.sleep(3)
                continue

            rows = parse_sms_blocks(html)
            newc = 0
            for row in rows:
                key = make_key(row)
                if key in seen:
                    continue

                delivered = send_to_subscribers(subs, row['line'], row['num'], row['content'], row['date'])
                # Abone yoksa hiç göndermeyelim (global spam olmasın)
                if delivered > 0:
                    newc += 1

                seen.add(key)

            if newc:
                save_seen(seen)
                log.info("Yeni %d SMS gönderildi", newc)

        except requests.exceptions.ReadTimeout:
            log.warning("GoIP Read timeout — atlıyorum.")
        except requests.exceptions.RequestException as e:
            log.warning("Ağ hatası: %s", e)
        except Exception as e:
            log.warning("Hata: %s", e)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
