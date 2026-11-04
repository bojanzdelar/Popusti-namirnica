"""Daily chicken fillet price check based on the official retailer price lists.

Retailers are required to publish machine-readable price lists (Pravilnik o
uslovima, sadrzaju i nacinu objavljivanja cenovnika); they are listed on
https://data.gov.rs. Every file uses the same 15-column, ';'-separated format.
"""

import csv
import io
import os
import re
import tempfile
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from discord_webhook import DiscordWebhook


DATA_GOV_API = "https://data.gov.rs/api/1/datasets/{slug}/"
USER_AGENT = "popusti-namirnica/1.0 (+https://github.com/bojanzdelar/Popusti-namirnica)"

# Either a fixed "url" (the retailer overwrites the same file every day) or a
# data.gov.rs dataset "slug" (a new file with a new URL is uploaded every day).
SOURCES = [
    {"chain": "Maxi", "url": "https://tsmdelhaizeserbia.delhaize.rs/PublicDoc/cene_proizvoda_Delhaize.csv"},
    {"chain": "Idea", "url": "https://ideacenovnici.blob.core.windows.net/cenovnici/drzavni/cene_proizvoda_ideamarketi.csv"},
    {"chain": "Univerexport", "url": "https://ucloud.univerexport.rs/opendata/univer.csv"},
    {"chain": "Lidl", "url": "https://www.lidl.rs/explore/assets/webPriceData/rs/cene_proizvoda_Lidl.csv"},
    {"chain": "Gomex", "url": "https://aswcene.gomex.rs/upload/cene_proizvoda_gomex.csv"},
    {"chain": "Aman", "url": "https://lpfr.blob.core.windows.net/cene/cene_proizvoda_AMAN%20DOO.csv?sv=2025-07-05&spr=https&st=2026-10-14T11%3A04%3A33Z&se=2040-12-31T12%3A04%3A00Z&sr=b&sp=r&sig=YS0tjYdECfbCZNJpDrcjhG74bGKqbS%2BrKXxsVlNCqeg%3D"},
    {"chain": "Super Vero", "url": "https://lpfr.blob.core.windows.net/cene/cene_proizvoda_Veropoulos%20d.o.o..csv?sv=2025-07-05&spr=https&st=2026-10-19T09%3A30%3A45Z&se=2040-12-31T10%3A30%3A00Z&sr=b&sp=r&sig=smS9O9ae7%2FJOI7dFKORgN4mSm34ZnMKhl9yeQ%2FaMji8%3D&rscc=no-cache"},
    {"chain": "METRO", "slug": "cenovnici-proizvoda-prema-pravilniku-o-uslovima-sadrzaju-i-nacinu-objavljivanja-cenovnika-metro"},
]

# Column indexes of the official format.
COL_CATEGORY = 0
COL_NAME = 2
COL_UNIT = 5
COL_STORE = 6
COL_PRICE = 8
COL_UNIT_PRICE = 9
COL_SALE_PRICE = 10
COL_SALE_FROM = 11
COL_SALE_TO = 12
COL_KIND = 14

MEAT_CATEGORY = "8"  # "Sveže i prerađeno meso" (excludes frozen and pet food)
CURRENT_PRICE_LIST = "VAZECI_CENOVNIK"  # the other rows are monthly snapshots

# Product matching works on lowercase text without diacritics ("pileći" -> "pileci").
INCLUDE_PATTERNS = [r"\bpil", r"\bfil(e|ea|et|\.)"]
EXCLUDE_PATTERNS = [
    r"\bdim",                   # dimljeni, dim.
    r"\bslajs",
    r"\bdelikates",
    r"\bcrev",                  # u crevu
    r"\bomot",                  # u omotu
    r"\bpizza",
    r"\bkuvan", r"\bpecen", r"\bgril",
    r"\bpan(ir|\.)", r"\bpohov",
    r"\bmarin",
    r"\brolovan", r"\bsous\b",
    r"\bpolupri", r"\bsocan",   # seasoned / semi-prepared
    r"\bmedalj", r"\bodresc", r"\bkomadi", r"\bnachos",
    r"\bsunk", r"\bsir\b",
    r"\bbat", r"\bkarab",       # thigh/drumstick fillet is a different cut
    r"\bzam", r"\bsmrz",        # frozen
    r"\bneopla", r"\byuhor",    # processed-meat brands (delikates, u crevu)
]

WEIGHT_RE = re.compile(r"(cca|ca\.?)?\s*(\d+(?:[.,]\d+)?)\s*(kgr|kg|gr|g)\b")


def normalize(text):
    text = text.lower().replace("đ", "dj")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip()


def is_plain_fillet(name):
    n = normalize(name)
    return all(re.search(p, n) for p in INCLUDE_PATTERNS) and not any(
        re.search(p, n) for p in EXCLUDE_PATTERNS
    )


def parse_number(value):
    value = (value or "").strip().replace(" ", "")
    if not value:
        return None
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number > 0 else None


def parse_date(value):
    try:
        return datetime.strptime(value.strip(), "%d-%m-%Y").date()
    except ValueError:
        return None


def package_weight_kg(name):
    """Weight from the product name ('480 gr', '1,5kg') and whether it is
    approximate ('cca 3kg'), else (None, False)."""
    matches = WEIGHT_RE.findall(normalize(name))
    if not matches:
        return None, False
    approx, amount, unit = matches[-1]
    amount = float(amount.replace(",", "."))
    return (amount if unit.startswith("kg") else amount / 1000), bool(approx)


def current_price(row, today):
    """Sale price if a sale is active today, otherwise the regular price."""
    regular = parse_number(row[COL_PRICE])
    sale = parse_number(row[COL_SALE_PRICE])
    if sale and (not regular or sale < regular):
        start, end = parse_date(row[COL_SALE_FROM]), parse_date(row[COL_SALE_TO])
        if (start is None or start <= today) and (end is None or today <= end):
            return sale, True
    return regular, False


def price_per_kg(row, price):
    """The published unit price is unreliable, so derive it from the name."""
    sold_by_kg = row[COL_UNIT].strip().lower() in ("kg", "kgr")
    weight, approx = package_weight_kg(row[COL_NAME])
    # 'cca 3kg' items sold by kg are weighed at the till: the price is per kg.
    if weight and not (approx and sold_by_kg):
        return price / weight
    if sold_by_kg:
        return price
    regular = parse_number(row[COL_PRICE])
    unit_price = parse_number(row[COL_UNIT_PRICE])
    if regular and unit_price:
        return price * unit_price / regular
    return None


def resolve_url(source):
    if "url" in source:
        return source["url"]
    response = requests.get(
        DATA_GOV_API.format(slug=source["slug"]),
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    resources = [
        r for r in response.json()["resources"] if (r.get("format") or "").lower() == "csv"
    ]
    if not resources:
        raise RuntimeError(f"No CSV resource in dataset {source['slug']}")
    return max(resources, key=lambda r: r.get("last_modified") or "")["url"]


def read_rows(url):
    """Download the CSV to a temporary file and yield its data rows."""
    with tempfile.TemporaryFile() as tmp:
        with requests.get(
            url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=(15, 120)
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=1 << 20):
                tmp.write(chunk)
        tmp.seek(0)
        text = io.TextIOWrapper(tmp, encoding="utf-8-sig", errors="replace", newline="")
        reader = csv.reader(text, delimiter=";")
        next(reader, None)  # header
        yield from reader


def find_fillets(chain, rows, today):
    """Return the cheapest offer per product name for one chain."""
    offers = {}
    for row in rows:
        if len(row) <= COL_KIND or row[COL_KIND].strip() != CURRENT_PRICE_LIST:
            continue
        if row[COL_CATEGORY].strip() != MEAT_CATEGORY or not is_plain_fillet(row[COL_NAME]):
            continue

        price, on_sale = current_price(row, today)
        if not price:
            continue
        per_kg = price_per_kg(row, price)
        if not per_kg:
            continue

        name = row[COL_NAME].strip()
        store = row[COL_STORE].strip()
        offer = offers.get(name)
        if offer is None or per_kg < offer["per_kg"] - 0.005:
            offers[name] = {
                "chain": chain, "name": name, "price": price, "per_kg": per_kg,
                "on_sale": on_sale, "stores": {store},
            }
        elif abs(per_kg - offer["per_kg"]) <= 0.005:
            offer["stores"].add(store)
    return list(offers.values())


def collect_offers(today):
    offers, errors = [], []
    for source in SOURCES:
        chain = source["chain"]
        try:
            found = find_fillets(chain, read_rows(resolve_url(source)), today)
            print(f"{chain}: {len(found)} plain chicken fillet products")
            offers.extend(found)
        except Exception as exc:
            print(f"{chain}: ERROR {exc}")
            errors.append(f"{chain}: {exc}")
    return offers, errors


def format_offer(offer):
    stores = ", ".join(sorted(offer["stores"]))
    label = f"{offer['chain']} ({stores})" if stores.casefold() != offer["chain"].casefold() else offer["chain"]
    sale = " AKCIJA" if offer["on_sale"] else ""
    return (
        f"**{offer['per_kg']:.0f} RSD/kg**{sale} - {offer['name']} "
        f"({offer['price']:.2f} RSD) - {label}"
    )


def send_message(report):
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    # Discord rejects messages longer than 2000 characters.
    chunks, current = [], ""
    for line in report.splitlines():
        if len(current) + len(line) + 1 > 1900:
            chunks.append(current)
            current = ""
        current += line + "\n"
    chunks.append(current)
    for chunk in chunks:
        DiscordWebhook(url=webhook_url, content=chunk).execute()


if __name__ == "__main__":
    threshold = float(os.environ["THRESHOLD_PRICE"])  # RSD per kg
    today = datetime.now(ZoneInfo("Europe/Belgrade")).date()

    offers, errors = collect_offers(today)
    offers.sort(key=lambda o: o["per_kg"])

    if not offers:
        send_message("Nijedan cenovnik nije mogao da se obradi:\n" + "\n".join(errors))
        raise SystemExit(1)

    print("\nCheapest offers:")
    for offer in offers[:10]:
        print("  " + format_offer(offer))

    low = [o for o in offers if o["per_kg"] <= threshold]
    lines = []
    if low:
        lines.append(f"Pileći file do {threshold:.0f} RSD/kg ({today:%d.%m.%Y}):")
        lines.extend(format_offer(o) for o in low)
    if errors:
        lines.append("\nNeuspešno preuzeti cenovnici:")
        lines.extend(errors)

    if lines:
        send_message("\n".join(lines))
    else:
        print(f"\nNo offers at or below {threshold:.0f} RSD/kg.")
