import asyncio
import logging
import os
import hashlib
import json
import httpx
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIGURAZIONE FILTRI ---
MIN_DISCOUNT_GAMES = 40
MIN_DISCOUNT_OTHER = 10
MIN_PRICE_EUR = 20.0

# Query pulite puntate direttamente alle API interne di Instant Gaming (Infallibili e veloci)
API_QUERIES = [
    {"label": "PC", "emoji": "🖥️", "url": f"https://instant-gaming.com{MIN_DISCOUNT_GAMES}&platform[]=1"},
    {"label": "PlayStation", "emoji": "🟦", "url": f"https://instant-gaming.com{MIN_DISCOUNT_GAMES}&platform[]=8&platform[]=9"},
    {"label": "Xbox", "emoji": "🟩", "url": f"https://instant-gaming.com{MIN_DISCOUNT_GAMES}&platform[]=11&platform[]=12"},
    {"label": "Nintendo", "emoji": "🟥", "url": f"https://instant-gaming.com{MIN_DISCOUNT_GAMES}&platform[]=4&platform[]=5"},
    {"label": "Subscription", "emoji": "🟪", "url": f"https://instant-gaming.com{MIN_DISCOUNT_OTHER}&type[]=subscription"},
    {"label": "GiftCard", "emoji": "🟪", "url": f"https://instant-gaming.com{MIN_DISCOUNT_OTHER}&type[]=giftcard"},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://instant-gaming.com"
}

def build_game_url(prod_id: int, seo_name: str) -> str:
    return f"https://instant-gaming.com{prod_id}-comprare-{seo_name}/"

def scrape_deals() -> list[dict]:
    all_deals: list[dict] = []
    seen_ids: set[int] = set()
    
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        for query in API_QUERIES:
            try:
                response = client.get(query["url"])
                if response.status_code != 200:
                    continue
                
                # Le API restituiscono direttamente la lista degli elementi puliti
                hits = response.json()
                if not isinstance(hits, list):
                    if isinstance(hits, dict) and "hits" in hits:
                        hits = hits["hits"]
                    else:
                        continue

                for hit in hits:
                    try:
                        prod_id = int(hit.get("prod_id", 0))
                        if not prod_id or prod_id in seen_ids:
                            continue
                        
                        discount = int(hit.get("discount", 0))
                        if discount < MIN_DISCOUNT_GAMES and query["label"] not in ["Subscription", "GiftCard"]:
                            continue
                            
                        price_eur = hit.get("price_eur") or hit.get("price")
                        price_val = float(price_eur) if price_eur is not None else None
                        if price_val is None or price_val < MIN_PRICE_EUR:
                            continue
                        
                        name = hit.get("fullname") or hit.get("name", "")
                        if not name:
                            continue
                        
                        seo_name = hit.get("seo_name", "")
                        game_url = build_game_url(prod_id, seo_name)
                        
                        seen_ids.add(prod_id)
                        all_deals.append({
                            "title": name,
                            "discount": discount,
                            "price": f"€{price_val:.2f}",
                            "url": game_url,
                            "platform_label": query["label"],
                            "emoji": query["emoji"],
                            "image_url": hit.get("image") or hit.get("cover") or ""
                        })
                    except Exception:
                        continue
            except Exception as e:
                logger.error(f"Errore caricamento API per {query['label']}: {e}")
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
        try: return set(json.loads(SEEN_FILE.read_text()))
        except Exception: pass
    return set()

def save_seen(seen: set[str]) -> None:
    try: SEEN_FILE.write_text(json.dumps(list(seen)))
    except Exception: pass

def deal_id(deal: dict) -> str:
    key = f"{deal['title']}:{deal['discount']}"
    return hashlib.md5(key.encode()).hexdigest()

def build_affiliate_url(url: str) -> str:
    if not url or not AFFILIATE_ID: return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}igr={AFFILIATE_ID}"

def build_caption(deal: dict) -> str:
    url = build_affiliate_url(deal["url"])
    return (
        f"{deal['emoji']} <b>{deal['title']}</b>\n\n"
        f"🔥 <b>Sconto:</b> <b>-{deal['discount']}%</b>\n"
        f"💰 <b>Prezzo:</b> <b>{deal['price']}</b>\n\n"
        f"⚡️ <i>Offerta disponibile su Instant Gaming</i>\n\n"
        f'👉 🔗 <b><a href="{url}">CLICCA QUI PER ACQUISTARE</a></b> 🔗 👈'
    )

async def send_deal(client: httpx.AsyncClient, deal: dict) -> bool:
    caption = build_caption(deal)
    sent = False
    
    # Se l'API ha già l'immagine pronta, la inviamo direttamente senza fare uno scraping aggiuntivo
    if deal.get("image_url"):
        try:
            payload = {"chat_id": CHANNEL_ID, "photo": deal["image_url"], "caption": caption, "parse_mode": "HTML"}
            resp = await client.post(f"{TELEGRAM_API}/sendPhoto", json=payload, timeout=20)
            if resp.json().get("ok"): sent = True
        except Exception: pass
        
    if not sent:
        try:
            payload = {"chat_id": CHANNEL_ID, "text": caption, "parse_mode": "HTML"}
            resp = await client.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=20)
            if resp.json().get("ok"): sent = True
        except Exception: pass
        
    return sent

async def check_and_publish() -> None:
    logger.info("Avvio scansione offerte tramite API...")
    deals = scrape_deals()
    if not deals:
        logger.info("Nessuna offerta rilevata dai filtri attuali.")
        return
        
    seen = load_seen()
    new_deals = [d for d in deals if deal_id(d) not in seen]
    logger.info(f"Scansione completata. Trovate {len(deals)} offerte totali. Nuove da pubblicare: {len(new_deals)}")
    
    if not new_deals: return
    
    async with httpx.AsyncClient() as client:
        for deal in new_deals:
            if await send_deal(client, deal):
                seen.add(deal_id(deal))
            await asyncio.sleep(3.0)
            
    save_seen(seen)

async def loop_bot() -> None:
    await check_and_publish()
    while True:
        logger.info("⏰ Controllo completato. Prossimo avvio tra 8 ore...")
        await asyncio.sleep(28800)
        await check_and_publish()

# --- WEB SERVER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()

async def main() -> None:
    logger.info("🤖 Bot Comparatore Gaming Attivo")
    threading.Thread(target=run_health_server, daemon=True).start()
    await loop_bot()

if __name__ == "__main__":
    asyncio.run(main())
