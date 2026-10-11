#!/usr/bin/env python3
"""
ebay_ofertas.py

Busca ofertas en:
  - Subastas de eBay en varios paises europeos (solo pujas en EUR, ubicadas en la UE
    y con envio a Alemania). Los precios "Sofort-Kaufen" se ignoran por completo.
  - Kleinanzeigen.de (anuncios nuevos por debajo del valor de referencia).

Las ofertas se ordenan por ahorro estimado frente a un valor de referencia y se envian
por correo.

Valor de referencia de cada busqueda:
  1. "valor_referencia" en config.json, si lo pones (lo mas fiable), o
  2. automatico: mediana de las pujas de subastas casi terminadas (poco tiempo restante
     y varias pujas), que se acercan mucho al precio real de venta.

Uso:
    python ebay_ofertas.py              # busca y envia el correo
    python ebay_ofertas.py --dry-run    # busca y guarda ofertas.html sin enviar
    python ebay_ofertas.py --demo       # datos de ejemplo, sin internet ni correo

Requisitos: Python 3.9+ y `pip install requests beautifulsoup4`
Credenciales: archivo credenciales.txt junto al script (ver LEEME.md)
"""

import argparse
import base64
import json
import os
import random
import re
import smtplib
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from urllib.parse import quote

try:
    import requests
except ImportError:
    requests = None
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

BASE = Path(__file__).resolve().parent
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
KA_BASE = "https://www.kleinanzeigen.de"

EU = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU",
    "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK",
}
SITE = {
    "EBAY_DE": "eBay.de", "EBAY_AT": "eBay.at", "EBAY_FR": "eBay.fr", "EBAY_IT": "eBay.it",
    "EBAY_ES": "eBay.es", "EBAY_NL": "eBay.nl", "EBAY_BE": "eBay.be", "EBAY_IE": "eBay.ie",
}


# ---------------------------------------------------------------- utilidades

def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def need_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Falta la variable {name} en credenciales.txt")
    return value


def eur(obj) -> float | None:
    """Devuelve el importe solo si esta en EUR; cualquier otra moneda se ignora."""
    if not obj or obj.get("value") in (None, ""):
        return None
    if obj.get("currency", "EUR") != "EUR":
        return None
    return float(obj["value"])


def parse_date(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def time_left(end: datetime, now: datetime) -> str:
    minutes = int((end - now).total_seconds() // 60)
    if minutes < 0:
        return "terminada"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


def _has(title: str, word: str, exact: bool) -> bool:
    pattern = r"(?<![\w])" + re.escape(word.lower()) + (r"(?![\w])" if exact else "")
    return re.search(pattern, title.lower()) is not None


def title_ok(title: str, s: dict, cfg: dict) -> bool:
    """Descarta por palabras (inicio de palabra), palabras exactas y palabras requeridas."""
    for word in cfg.get("exclude_global", []) + s.get("exclude", []):
        if _has(title, word, exact=False):
            return False
    for word in s.get("exclude_exact", []):
        if _has(title, word, exact=True):
            return False
    for word in s.get("require", []):
        if word.lower() not in title.lower():
            return False
    return True


# ---------------------------------------------------------------- eBay

class Ebay:
    def __init__(self, client_id: str, client_secret: str, cfg: dict):
        auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        r = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": SCOPE},
            timeout=30,
        )
        if r.status_code != 200:
            sys.exit(f"No se pudo obtener el token de eBay ({r.status_code}): {r.text[:300]}")
        self.token = r.json()["access_token"]
        # Para que eBay calcule el envio hasta tu codigo postal en Alemania
        ctx = "country=DE" + (f",zip={cfg['plz']}" if cfg.get("plz") else "")
        self.context = "contextualLocation=" + quote(ctx, safe="")

    def search(self, marketplace: str, params: dict) -> list[dict]:
        r = requests.get(
            SEARCH_URL,
            headers={
                "Authorization": f"Bearer {self.token}",
                "X-EBAY-C-MARKETPLACE-ID": marketplace,
                "X-EBAY-C-ENDUSERCTX": self.context,
            },
            params=params,
            timeout=30,
        )
        if r.status_code != 200:
            print(f"  Aviso: {SITE.get(marketplace, marketplace)} fallo ({r.status_code}): {r.text[:150]}")
            return []
        return r.json().get("itemSummaries", [])


def shipping_cost(item: dict) -> float | None:
    """Envio mas barato en EUR. None si no se puede calcular (se descarta)."""
    options = item.get("shippingOptions") or []
    if not options:
        return 0.0
    costs = [eur(o.get("shippingCost")) for o in options]
    costs = [c for c in costs if c is not None]
    return min(costs) if costs else None


def ebay_auctions(ebay: Ebay, s: dict, cfg: dict, now: datetime) -> list[dict]:
    """Subastas de todos los eBay configurados, sin duplicados."""
    end = (now + timedelta(hours=cfg["hours_ahead"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    filters = [
        "buyingOptions:{AUCTION}",
        f"itemEndDate:[..{end}]",
        "deliveryCountry:DE",
    ]
    if s.get("condition_ids"):
        filters.append("conditionIds:{" + "|".join(str(i) for i in s["condition_ids"]) + "}")

    found: dict[str, dict] = {}
    for market in cfg["ebay_marketplaces"]:
        items = ebay.search(market, {
            "q": s["query"], "filter": ",".join(filters),
            "sort": "endingSoonest", "limit": 100,
        })
        for item in items:
            key = item.get("legacyItemId") or item.get("itemId")
            if key in found:
                continue
            item["_market"] = market
            found[key] = item
    return list(found.values())


def normalize_auction(item: dict, s: dict, cfg: dict, now: datetime) -> dict | None:
    buying = item.get("buyingOptions") or []
    if "AUCTION" not in buying:
        return None
    if cfg.get("excluir_subastas_con_sofort_kaufen") and "FIXED_PRICE" in buying:
        return None
    if not title_ok(item.get("title", ""), s, cfg):
        return None
    country = (item.get("itemLocation") or {}).get("country")
    if country and country not in EU:
        return None  # fuera de la UE: aduanas e impuestos
    # Solo la puja actual. Nunca el campo "price" (puede ser el precio Sofort-Kaufen).
    bid = eur(item.get("currentBidPrice"))
    ship = shipping_cost(item)
    if bid is None or ship is None or not item.get("itemEndDate"):
        return None
    if bid < s.get("min_price", 0):
        return None  # accesorio o subasta que aun no refleja el precio real
    end = parse_date(item["itemEndDate"])
    return {
        "source": SITE.get(item["_market"], item["_market"]),
        "kind": "auction",
        "id": "ebay:" + str(item.get("legacyItemId") or item.get("itemId")),
        "search": s.get("name", s["query"]),
        "title": item.get("title", ""),
        "url": item.get("itemWebUrl", ""),
        "image": (item.get("image") or {}).get("imageUrl", ""),
        "condition": item.get("condition", ""),
        "location": country or "",
        "price": bid,
        "shipping": ship,
        "total": bid + ship,
        "bids": item.get("bidCount", 0),
        "end": end,
        "minutes_left": (end - now).total_seconds() / 60,
        "time_left": time_left(end, now),
    }


def auto_reference(auctions: list[dict], s: dict, cfg: dict) -> tuple[float | None, int]:
    """Mediana de pujas de subastas casi terminadas: aproximacion al precio real."""
    near = [
        a["price"] for a in auctions
        if a["minutes_left"] <= cfg["ref_minutes_left"]
        and a["bids"] >= cfg["ref_min_bids"]
        and a["price"] >= s.get("min_price", 0)
    ]
    if len(near) < cfg["ref_min_samples"]:
        return None, len(near)
    return statistics.median(near), len(near)


# ---------------------------------------------------------------- Kleinanzeigen

KA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
}


def ka_url(s: dict, max_price: int | None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", s.get("ka_query", s["query"]).lower()).strip("-")
    lo = int(s.get("min_price", 0))
    hi = str(int(max_price)) if max_price else ""
    return f"{KA_BASE}/s-preis:{lo}:{hi}/{slug}/k0"


def ka_parse_price(text: str) -> tuple[float | None, bool]:
    """'650 € VB' -> (650.0, True). 'Zu verschenken' o vacio -> (None, ...)."""
    vb = "VB" in text
    m = re.search(r"(\d[\d.]*)(?:,(\d{1,2}))?\s*€", text)
    if not m:
        return None, vb
    whole = m.group(1).replace(".", "")
    cents = m.group(2) or "0"
    return float(f"{whole}.{cents}"), vb


def ka_parse(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for art in soup.select("article.aditem"):
        ad_id = art.get("data-adid")
        link = art.select_one("a.ellipsis") or art.select_one("h2 a")
        price_el = art.select_one(".aditem-main--middle--price-shipping--price")
        if not ad_id or not link or not price_el:
            continue
        price, vb = ka_parse_price(price_el.get_text(" ", strip=True))
        if price is None:
            continue
        loc = art.select_one(".aditem-main--top--left")
        date = art.select_one(".aditem-main--top--right")
        img = art.select_one("img")
        href = link.get("href") or art.get("data-href") or ""
        ship_el = art.select_one(".aditem-main--middle--price-shipping--shipping")
        out.append({
            "id": ad_id,
            "title": link.get_text(" ", strip=True),
            "url": href if href.startswith("http") else KA_BASE + href,
            "price": price,
            "vb": vb,
            "location": loc.get_text(" ", strip=True) if loc else "",
            "date": date.get_text(" ", strip=True) if date else "",
            "image": (img.get("src") or img.get("data-imgsrc") or "") if img else "",
            "shipping_possible": bool(ship_el and "Versand" in ship_el.get_text()),
            "topad": bool(art.select_one(".badge-topad")),
        })
    return out


def kleinanzeigen(s: dict, cfg: dict, ref: float) -> list[dict]:
    ka = cfg["kleinanzeigen"]
    max_price = ref * (1 - cfg["min_discount"])
    url = ka_url(s, max_price)
    time.sleep(random.uniform(ka.get("delay_min_s", 4), ka.get("delay_max_s", 8)))
    try:
        r = requests.get(url, headers=KA_HEADERS, timeout=30)
    except requests.RequestException as e:
        print(f"  Aviso: Kleinanzeigen no respondio ({e})")
        return []
    if r.status_code != 200:
        print(f"  Aviso: Kleinanzeigen respondio {r.status_code} (posible bloqueo temporal)")
        return []
    deals = []
    for ad in ka_parse(r.text):
        if ad["topad"] or not title_ok(ad["title"], s, cfg):
            continue
        if ad["price"] < s.get("min_price", 0) or ad["price"] > max_price:
            continue
        if ka.get("solo_con_envio") and not ad["shipping_possible"]:
            continue
        deals.append({
            "source": "Kleinanzeigen",
            "kind": "classified",
            "id": "ka:" + ad["id"],
            "search": s.get("name", s["query"]),
            "title": ad["title"],
            "url": ad["url"],
            "image": ad["image"],
            "condition": ("VB" if ad["vb"] else "Festpreis")
                         + (" | Versand möglich" if ad["shipping_possible"] else ""),
            "location": ad["location"],
            "price": ad["price"],
            "shipping": 0.0,
            "total": ad["price"],
            "bids": None,
            "time_left": ad["date"],
        })
    return deals


# ---------------------------------------------------------------- busqueda

def add_savings(deal: dict, ref: float, ref_note: str) -> dict:
    deal["reference"] = ref
    deal["reference_note"] = ref_note
    deal["savings"] = ref - deal["total"]
    deal["savings_pct"] = deal["savings"] / ref if ref else 0
    return deal


def find_deals(cfg: dict, now: datetime) -> list[dict]:
    ebay = None
    if cfg["ebay_marketplaces"]:
        ebay = Ebay(need_env("EBAY_CLIENT_ID"), need_env("EBAY_CLIENT_SECRET"), cfg)
    use_ka = cfg.get("kleinanzeigen", {}).get("activo", False)
    if use_ka and BeautifulSoup is None:
        sys.exit("Para Kleinanzeigen instala: pip install beautifulsoup4")

    deals = []
    for s in cfg["searches"]:
        name = s.get("name", s["query"])
        print(f"Buscando: {name}")

        auctions = []
        if ebay:
            raw = ebay_auctions(ebay, s, cfg, now)
            auctions = [a for a in (normalize_auction(i, s, cfg, now) for i in raw) if a]

        ref, note = None, ""
        manual = s.get("valor_referencia")
        if cfg.get("preferir_referencia_automatica", True) or not manual:
            auto, n = auto_reference(auctions, s, cfg)
            if auto is not None:
                ref, note = auto, f"auto, {n} subastas"
        if ref is None and manual:
            ref, note = float(manual), "manual"
        if ref is None:
            print("  Sin valor de referencia. Pon 'valor_referencia' en config.json.")
            continue
        print(f"  Valor de referencia: {ref:.0f} EUR ({note})")

        if ref < s.get("min_value", cfg["min_value"]):
            print("  Valor por debajo del minimo, se omite")
            continue

        for a in auctions:
            add_savings(a, ref, note)
            if a["savings_pct"] >= cfg["min_discount"]:
                deals.append(a)

        if use_ka and s.get("kleinanzeigen", True):
            for d in kleinanzeigen(s, cfg, ref):
                deals.append(add_savings(d, ref, note))
    return deals


# ---------------------------------------------------------------- correo

def rows_html(deals: list[dict]) -> str:
    td = 'style="padding:8px;border-bottom:1px solid #eee;vertical-align:top"'
    tdr = 'style="padding:8px;border-bottom:1px solid #eee;vertical-align:top;text-align:right;white-space:nowrap"'
    rows = []
    for d in deals:
        img = f'<img src="{escape(d["image"])}" width="80" style="border-radius:6px">' if d["image"] else ""
        extra = f" | {d['bids']} pujas" if d["bids"] is not None else ""
        ship = f"+ envio {d['shipping']:.2f}" if d["kind"] == "auction" else ""
        rows.append(f"""
        <tr>
          <td {td}>{img}</td>
          <td {td}>
            <a href="{escape(d['url'])}" style="color:#0654ba;font-weight:600;text-decoration:none">{escape(d['title'])}</a><br>
            <span style="color:#777;font-size:12px">{escape(d['source'])} | {escape(d['search'])} | {escape(d['condition'])}{extra} | {escape(d['location'])}</span>
          </td>
          <td {tdr}>{d['price']:.2f} &euro;<br><span style="color:#777;font-size:12px">{ship}</span></td>
          <td {tdr}>~{d['reference']:.0f} &euro;<br><span style="color:#777;font-size:12px">{escape(d['reference_note'])}</span></td>
          <td {tdr} ><span style="color:#1a7f37;font-weight:700">{d['savings']:.0f} &euro;<br>-{d['savings_pct']*100:.0f} %</span></td>
          <td {tdr}>{escape(d['time_left'])}</td>
        </tr>""")
    return "".join(rows)


def section(title: str, deals: list[dict], last_col: str, note: str) -> str:
    body = rows_html(deals) or (
        '<tr><td colspan="6" style="padding:16px;color:#777">Nada que cumpla los criterios.</td></tr>'
    )
    th = 'style="padding:8px;text-align:right"'
    return f"""
  <h3 style="margin:28px 0 4px">{title} ({len(deals)})</h3>
  <p style="color:#666;margin:0 0 8px;font-size:12px">{note}</p>
  <table style="border-collapse:collapse;width:100%;font-size:14px">
    <tr style="background:#f5f5f5;text-align:left">
      <th style="padding:8px"></th><th style="padding:8px">Articulo</th>
      <th {th}>Precio</th><th {th}>Valor ref.</th><th {th}>Ahorro</th><th {th}>{last_col}</th>
    </tr>{body}
  </table>"""


def build_html(deals: list[dict], cfg: dict, now: datetime) -> str:
    auctions = [d for d in deals if d["kind"] == "auction"]
    classifieds = [d for d in deals if d["kind"] == "classified"]
    sites = ", ".join(SITE.get(m, m) for m in cfg["ebay_marketplaces"])
    return f"""<!doctype html>
<html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;max-width:960px;margin:auto">
  <h2 style="margin-bottom:4px">Ofertas por debajo del valor de mercado</h2>
  <p style="color:#555;margin-top:0;font-size:13px">{now.astimezone().strftime('%d.%m.%Y %H:%M')}
    | ordenadas por ahorro estimado | solo precios en EUR, sin precios Sofort-Kaufen</p>
  {section("Subastas de eBay que terminan pronto", auctions, "Termina en",
           f"{sites}. Subastas en la UE con envio a Alemania que terminan en las proximas "
           f"{cfg['hours_ahead']} h. La puja actual suele subir en los ultimos minutos.")}
  {section("Kleinanzeigen", classifieds, "Publicado",
           "Anuncios nuevos por debajo del valor de referencia. VB = precio negociable.") if cfg.get("kleinanzeigen", {}).get("activo") else ""}
</body></html>"""


def send_email(html: str, count: int) -> None:
    host = need_env("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = need_env("SMTP_USER")
    password = need_env("SMTP_PASSWORD")
    sender = os.environ.get("EMAIL_FROM", user)
    recipient = need_env("EMAIL_TO")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Ofertas: {count} por debajo del valor de mercado"
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText("Abre este correo en formato HTML para ver las ofertas.", "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)


# ---------------------------------------------------------------- demo

def demo_deals(now: datetime) -> list[dict]:
    out = []
    samples = [
        ("eBay.de", "auction", "Samsung Galaxy S24 Ultra 512GB Titan Violet", "S24 Ultra", 590, 6.99, 14, 780, 25, "DE"),
        ("eBay.it", "auction", "Samsung Galaxy S24 Ultra 512GB Titanium Black", "S24 Ultra", 545, 12.50, 9, 780, 48, "IT"),
        ("eBay.fr", "auction", "Fluke 87V multimètre numérique", "Fluke Multimeter", 165, 9.90, 7, 290, 70, "FR"),
        ("Kleinanzeigen", "classified", "Samsung Galaxy S24 Ultra 512 GB wie neu", "S24 Ultra", 560, 0, None, 780, None, "80331 München"),
        ("Kleinanzeigen", "classified", "Ubiquiti UniFi Dream Machine Pro", "UniFi", 230, 0, None, 340, None, "82152 Planegg"),
    ]
    for i, (src, kind, title, search, price, ship, bids, ref, mins, loc) in enumerate(samples):
        d = {
            "source": src, "kind": kind, "id": f"demo{i}", "search": search, "title": title,
            "url": "https://www.ebay.de/" if kind == "auction" else KA_BASE,
            "image": "", "condition": "Gebraucht" if kind == "auction" else "VB | Versand möglich",
            "location": loc, "price": price, "shipping": ship, "total": price + ship, "bids": bids,
            "time_left": time_left(now + timedelta(minutes=mins), now) if mins else "Heute, 14:12",
        }
        out.append(add_savings(d, ref, "manual"))
    return out


# ---------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(description="Ofertas de eBay Europa y Kleinanzeigen")
    parser.add_argument("--config", default=str(BASE / "config.json"))
    parser.add_argument("--dry-run", action="store_true", help="no envia correo, guarda ofertas.html")
    parser.add_argument("--demo", action="store_true", help="usa datos de ejemplo")
    args = parser.parse_args()

    load_env(BASE / "credenciales.txt")
    load_env(BASE / ".env")
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    seen_path = BASE / "enviados.json"
    seen = set(json.loads(seen_path.read_text())) if seen_path.exists() else set()

    # En la nube (GitHub Actions) Kleinanzeigen suele bloquear: se puede apagar con KLEINANZEIGEN=0
    if os.environ.get("KLEINANZEIGEN") == "0":
        cfg.setdefault("kleinanzeigen", {})["activo"] = False

    if args.demo:
        cfg.setdefault("kleinanzeigen", {})["activo"] = True
        deals = demo_deals(now)
    else:
        if requests is None:
            sys.exit("Instala las dependencias: pip install requests beautifulsoup4")
        deals = find_deals(cfg, now)
        if cfg.get("skip_already_sent", True):
            deals = [d for d in deals if d["id"] not in seen]

    unique = {}
    for d in deals:
        unique.setdefault(d["id"], d)
    deals = sorted(unique.values(), key=lambda d: d["savings"], reverse=True)[: cfg["max_results"]]

    html = build_html(deals, cfg, now)
    print(f"\n{len(deals)} ofertas encontradas")

    if args.dry_run or args.demo:
        out = BASE / "ofertas.html"
        out.write_text(html, encoding="utf-8")
        print(f"Guardado en {out}")
        return

    if not deals and not cfg.get("send_when_empty", False):
        print("Sin ofertas nuevas, no se envia correo")
        return

    send_email(html, len(deals))
    print("Correo enviado")
    seen.update(d["id"] for d in deals)
    seen_path.write_text(json.dumps(sorted(seen)[-10000:]))


if __name__ == "__main__":
    main()
