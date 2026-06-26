import asyncio
import logging
import os
import hashlib
import json
import re
from pathlib import Path
from datetime import datetime
import httpx

logger = logging.getLogger(__name__)

# --- CONFIGURAZIONE INTERFACCIA E FILTRI ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MIN_DISCOUNT_GAMES = 40
MIN_DISCOUNT_OTHER = 10
MIN_PRICE_EUR = 20.0

SEARCH_QUERIES = [
    {"label": "PC", "emoji": "🖥️", "url": f"https://instant-gaming.com{MIN_DISCOUNT_GAMES}&platform[]=1"},
    {"label": "PlayStation", "emoji": "🟦", "url": f"https://instant-gaming.com{MIN_DISCOUNT_GAMES}&platform[]=8&platform[]=9"},
    {"label": "Xbox", "emoji": "🟩", "url": f"https://instant-gaming.com{MIN_DISCOUNT_GAMES}&platform[]=11&platform[]=12"},
    {"label": "Nintendo", "emoji": "🟥", "url": f"https://instant-gaming.com{MIN_DISCOUNT_GAMES}&platform[]=4&platform[]=5"},
    {"label": "Subscription", "emoji": "🟪", "url": f"https://instant-gaming.com{MIN_DISCOUNT_OTHER}"},
    {"label": "GiftCard", "emoji": "🟪", "url": f"https://instant-gaming.com{MIN_DISCOUNT_OTHER}"},
]

def build_game_url(prod_id: int, seo_name: str) -> str:
    return f"https://instant-gaming.com{prod_id}-buy-{seo_name}/"

def get_og_image(prod_id: int, seo_name: str) -> str:
    try:
        url = build_game_url(prod_id, seo_name)
        with httpx.Client(headers=HEADERS, timeout=10, follow_redirects=True) as client:
            r = client.get(url)
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', r.text)
        if m:
            return m.group(1)
    except Exception as e:
        logger.debug(f"Impossibile recuperare og:image per {seo_name}: {e}")
    return ""

def _fetch_hits(url: str) -> list[dict]:
    try:
        with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
    except Exception as e:
        logger.error(f"Errore nel caricamento di {url}: {e}")
        return []
    m = re.search(r"window\.searchResults\s*=\s*(\{.*?\});", response.text, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(1)).get("hits", [])
    except Exception:
        return []

def scrape_deals() -> list[dict]:
    all_deals: list[dict] = []
    seen_ids: set[int] = set()
    for query in SEARCH_QUERIES:
        hits = _fetch_hits(query["url"])
        for hit in hits:
            try:
                prod_id = int(hit.get("prod_id", 0))
                if not prod_id or prod_id in seen_ids:
                    continue
                discount = int(hit.get("discount", 0))
                if discount <= 0:
                    continue
                price_eur = hit.get("price_eur")
                price_val = float(price_eur) if price_eur is not None else None
                if price_val is None or price_val < MIN_PRICE_EUR:
                    continue
                name = hit.get("fullname") or hit.get("name", "")
                if not name:
                    continue
                seo_name = hit.get("seo_name", "")
                game_url = build_game_url(prod_id, seo_name) if seo_name else ""
                seen_ids.add(prod_id)
                all_deals.append({
                    "title": name,
                    "discount": discount,
                    "price": f"€{price_val:.2f}",
                    "seo_name": seo_name,
                    "prod_id": prod_id,
                    "url": game_url,
                    "platform_label": query["label"],
                    "emoji": query["emoji"],
                })
            except Exception:
                continue
    return all_deals

# --- PARTE TELEGRAM BOT ---
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
AFFILIATE_ID = os.environ.get("INSTANT_GAMING_AFFILIATE_ID", "gamer-0c292bc").strip()

SEEN_FILE = Path("seen_deals.json")
TELEGRAM_API = f"https://telegram.org{BOT_TOKEN}"

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            pass
    return set()

def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(list(seen)))

def deal_id(deal: dict) -> str:
    key = f"{deal['title']}:{deal['discount']}"
    return hashlib.md5(key.encode()).hexdigest()

def build_affiliate_url(url: str) -> str:
    if not url or not AFFILIATE_ID:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}igr={AFFILIATE_ID}"

def build_caption(deal: dict) -> str:
    url = build_affiliate_url(deal["url"])
    lines = [
        f"{deal['emoji']} <b>{deal['title']}</b>",
        "",
        f"🔥 <b>Sconto:</b> <b>-{deal['discount']}%</b>",
        f"💰 <b>Prezzo:</b> <b>{deal['price']}</b>",
        "",
        "⚡️ <i>Offerta disponibile su Instant Gaming</i>",
        "",
        f'👉 🔗 <b><a href="{url}">CLICCA QUI PER ACQUISTARE</a></b> 🔗 👈',
    ]
    return "\n".join(lines)

async def post_with_retry(client: httpx.AsyncClient, endpoint: str, **kwargs) -> dict:
    for attempt in range(3):
        resp = await client.post(endpoint, timeout=30, **kwargs)
        result = resp.json()
        if result.get("ok"):
            return result
        if resp.status_code == 429:
            await asyncio.sleep(36)
    return result

async def fetch_image_bytes(prod_id: int, seo_name: str) -> bytes | None:
    image_url = get_og_image(prod_id, seo_name)
    if not image_url:
        return None
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
            resp = await client.get(image_url)
            if resp.status_code == 200:
                return resp.content
    except Exception:
        pass
    return None

async def send_deal(client: httpx.AsyncClient, deal: dict) -> bool:
    caption = build_caption(deal)
    img_bytes = await fetch_image_bytes(deal.get("prod_id", 0), deal.get("seo_name", ""))
    sent = False
    if img_bytes:
        try:
            files = {"photo": ("cover.jpg", img_bytes, "image/jpeg")}
            result = await post_with_retry(client, f"{TELEGRAM_API}/sendPhoto", data={"chat_id": CHANNEL_ID, "caption": caption, "parse_mode": "HTML"}, files=files)
            if result.get("ok"): sent = True
        except Exception:
            pass
    if not sent:
        result = await post_with_retry(client, f"{TELEGRAM_API}/sendMessage", json={"chat_id": CHANNEL_ID, "text": caption, "parse_mode": "HTML"})
        if result.get("ok"): sent = True
    return sent

async def check_and_publish() -> None:
    deals = scrape_deals()
    if not deals: return
    seen = load_seen()
    new_deals = [d for d in deals if deal_id(d) not in seen]
    if not new_deals: return
    async with httpx.AsyncClient() as client:
        for deal in new_deals:
            if await send_deal(client, deal):
                seen.add(deal_id(deal))
            await asyncio.sleep(3.0)
    save_seen(seen)

async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("🤖 Bot Comparatore Gaming Attivo")
    await check_and_publish()
    while True:
        logger.info("⏰ Controllo completato. Prossimo avvio tra 8 ore...")
        await asyncio.sleep(28800)  # Esegue il ciclo ogni 8 ore esatte
        await check_and_publish()

if __name__ == "__main__":
    asyncio.run(main())
