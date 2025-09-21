import requests
from requests.auth import HTTPBasicAuth

url = "http://5.11.128.154:5050/default/en_US/sms.html?type=sms_inbox"
resp = requests.get(url, auth=HTTPBasicAuth("sms", "9091"), timeout=5)
print("status:", resp.status_code)
open("inbox.html", "w", encoding="utf-8").write(resp.text)
print("HTML dosyası kaydedildi: inbox.html")
