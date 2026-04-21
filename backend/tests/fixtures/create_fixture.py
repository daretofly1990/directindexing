"""
Run this script once in a network-enabled environment to download the real Wikipedia fixture.
Usage: python create_fixture.py
"""
import urllib.request

url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
headers = {"User-Agent": "Mozilla/5.0 (compatible; DirectIndexBot/1.0)"}
req = urllib.request.Request(url, headers=headers)
with urllib.request.urlopen(req, timeout=30) as resp:
    html = resp.read().decode("utf-8")
with open("sp500_wiki.html", "w", encoding="utf-8") as f:
    f.write(html)
print(f"Saved {len(html)} chars to sp500_wiki.html")
