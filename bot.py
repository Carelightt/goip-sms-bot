# bot.py
import os, re, time, json, html, logging, requests
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==== AYARLAR ====
GOIP_URL  = "http://5.11.128.154:6060/default/en_US/tools.html?type=sms_inbox"
GOIP_USER = "user"
GOIP_PASS = "9090"

BOT_TOKEN = "8468644425:AAGJq2zEJiOSvrox8uqMv1VePrD9VsnrmDs"
CHAT_ID   = -1002615288080

POLL_INTERVAL = 10
SEEN_FILE     = "seen.json"

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
        allowed_methods=["GET"],
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

SESSION = make_session()

def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            return set(json.load(open(SEEN_FILE, "r", encoding="utf-8")))
        except:
            return set()
    return set()

def save_seen(seen:set):
    json.dump(list(seen), open(SEEN_FILE,"w",encoding="utf-8"), ensure_ascii=False)

def send_tg(line, num, content, date):
    text = (
        f"📩 <b>Yeni SMS</b>\n"
        f"🧵 Line: <code>{line}</code>\n"
        f"👤 Gönderen: <code>{html.escape(num)}</code>\n"
        f"🕒 {html.escape(date)}\n"
        f"💬 <code>{html.escape(content)}</code>"
    )
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": str(CHAT_ID), "text": text, "parse_mode": "HTML"}
    r = requests.post(url, data=data, timeout=15)
    if r.status_code != 200:
        log.warning("Telegram hata: %s %s", r.status_code, r.text[:200])

def fetch_html():
    # timeout=(connect, read)
    r = SESSION.get(GOIP_URL, timeout=(3, 6))
    if r.status_code == 200:
        return r.text
    log.warning("GoIP HTTP durum kodu: %s", r.status_code)
    return ""

def parse_sms_blocks(html_text:str):
    results=[]
    # sms=[ ... ]; pos=..; sms_row_insert(..., line)
    for m in re.finditer(r'sms=\s*\[(.*?)\];\s*pos=(\d+);\s*sms_row_insert\(.*?(\d+)\)', html_text, flags=re.S):
        arr_str, pos, line = m.groups()
        line = int(line)
        msgs = re.findall(r'"([^"]*)"', arr_str)
        for raw in msgs:
            raw = raw.strip()
            if not raw:
                continue
            parts = raw.split(",", 2)
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

def _norm(s: str) -> str:
    # küçük farkları normalize et (boşluk/CRLF vs.)
    s = s.replace("\r", "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def make_key(row) -> str:
    return f"{row['line']}::{_norm(row.get('date',''))}::{_norm(row.get('num',''))}::{_norm(row.get('content',''))}"

def initial_warmup_seen(seen:set):
    """
    BOT AÇILDIĞINDA: mevcut inbox'taki tüm kayıtları 'seen' yap
    böylece hiçbir eski SMS Telegram'a gönderilmez.
    """
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

def main():
    seen = load_seen()
    log.info("Başladı, görülen %d kayıt", len(seen))

    # --- KRİTİK: İlk açılışta warm-up yap, eskiyi asla göndermiyoruz ---
    initial_warmup_seen(seen)

    while True:
        try:
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

                # Sadece yeni gelenleri gönder
                send_tg(row['line'], row['num'], row['content'], row['date'])
                seen.add(key)
                newc += 1

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

if __name__=="__main__":
    main()
