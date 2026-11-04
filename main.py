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


MAX_MESSAGE_LENGTH = 2000  # Discord message limit
KEEP_UPPER = {"RF", "RFZ", "MAP", "XXL", "UMK", "FP", "PP", "MC", "MM", "M", "OHVP", "KG"}
NAME_NOISE = re.compile(r"-STAND\.-VAR\.|^SVE(Ž|Z)E MESO\s+|^SVEZ\.?\s+", re.IGNORECASE)


def rsd(value, decimals=2):
    """Serbian number format: 1.234,56"""
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def clean_name(name):
    """'SVEŽE MESO PILEĆI FILE RF' -> 'Pileći file RF'"""
    name = NAME_NOISE.sub("", name).strip(" -")
    if name.upper() != name:
        return name  # already in mixed case
    words = []
    for i, word in enumerate(name.split()):
        if word in KEEP_UPPER or any(c.isdigit() for c in word):
            words.append(word)
        else:
            words.append(word.capitalize() if i == 0 else word.lower())
    return " ".join(words)


def store_label(offer):
    """Store formats worth showing, e.g. 'Maxi, Mega Maxi' or 'S, M, L'."""
    stores = []
    for store in sorted(offer["stores"]):
        short = re.sub(rf"^{re.escape(offer['chain'])}\s*-\s*", "", store, flags=re.IGNORECASE)
        stores.append(short.capitalize() if short.isupper() and len(short) > 2 else short)
    if len(stores) == 1 and normalize(stores[0]).startswith(normalize(offer["chain"]).split()[0]):
        return ""  # just the retailer's legal name
    return ", ".join(stores)


def format_offer(offer, show_stores=True):
    line = f"**{rsd(offer['per_kg'], 0)} RSD/kg** · {clean_name(offer['name'])}"
    stores = store_label(offer) if show_stores else ""
    return f"{line} _({stores})_" if stores else line


def build_message(offers, threshold, today, errors):
    lines = []
    if offers:
        by_chain = {}
        for offer in offers:  # already sorted by price per kg
            by_chain.setdefault(offer["chain"], []).append(offer)

        lines.append(f"## 🐔 Pileći file do {rsd(threshold, 0)} RSD/kg")
        lines.append(f"-# {today:%d.%m.%Y.} · cenovnici trgovaca sa data.gov.rs")
        for chain, chain_offers in by_chain.items():
            # Same stores for every offer: show them once, next to the chain.
            labels = {store_label(o) for o in chain_offers}
            shared = labels.pop() if len(labels) == 1 else None
            lines.append("")
            lines.append(f"### {chain}" + (f" _({shared})_" if shared else ""))
            lines.extend(format_offer(o, show_stores=shared is None) for o in chain_offers)

    if errors:
        lines.append("")
        lines.append("**⚠️ Neki cenovnici nisu preuzeti:**")
        lines.extend(f"- {e}"[:300] for e in errors)
    return "\n".join(lines)


def send_message(text):
    """Send the text, split on line boundaries to fit Discord's limit."""
    chunks, current = [], ""
    for line in text.splitlines():
        if current and len(current) + len(line) + 1 > MAX_MESSAGE_LENGTH:
            chunks.append(current)
            current = ""
        current += line + "\n"
    chunks.append(current)

    for chunk in chunks:
        response = DiscordWebhook(url=os.environ["DISCORD_WEBHOOK_URL"], content=chunk).execute()
        if response.status_code >= 400:
            raise RuntimeError(f"Discord returned HTTP {response.status_code}: {response.text}")


if __name__ == "__main__":
    threshold = float(os.environ["THRESHOLD_PRICE"])  # RSD per kg
    today = datetime.now(ZoneInfo("Europe/Belgrade")).date()

    offers, errors = collect_offers(today)
    offers.sort(key=lambda o: o["per_kg"])

    if not offers and errors:
        send_message(build_message([], threshold, today, errors))
        raise SystemExit(1)

    print("\nCheapest offers:")
    for offer in offers[:10]:
        print(f"  {offer['per_kg']:.0f} RSD/kg  {offer['chain']}: {offer['name']}")

    low = [o for o in offers if o["per_kg"] <= threshold]
    if low or errors:
        send_message(build_message(low, threshold, today, errors))
    else:
        print(f"\nNo offers at or below {threshold:.0f} RSD/kg.")
