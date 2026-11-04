import os
import re
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup
from discord_webhook import DiscordWebhook
from playwright.sync_api import sync_playwright


EXCLUDED_SELLERS = {"dis", "tempo"}


CLOUDFLARE_TITLES = ("just a moment", "attention required", "access denied")


def _is_cloudflare_page(page):
    try:
        title = page.title().casefold()
    except Exception:
        return True
    return any(marker in title for marker in CLOUDFLARE_TITLES)


def _wait_for_cloudflare(page, timeout_ms=30000, step_ms=1000):
    """Return True once the Cloudflare challenge page has been replaced."""
    waited = 0
    while waited < timeout_ms:
        if not _is_cloudflare_page(page):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            return True
        page.wait_for_timeout(step_ms)
        waited += step_ms
    return not _is_cloudflare_page(page)


def fetch_html(url):
    """Load the product page in Chromium and return its rendered HTML."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
                ),
                locale="sr-RS",
            )
            page = context.new_page()
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=45000,
            )

            if response is not None and response.status >= 400:
                # Cloudflare serves its "Just a moment..." JS challenge with
                # HTTP 403/503. A real browser solves it and reloads the page,
                # so wait for that instead of failing immediately.
                if not _wait_for_cloudflare(page):
                    raise RuntimeError(
                        f"Cenoteka returned HTTP {response.status} for {url} "
                        f"(page title: {page.title()!r})"
                    )

            # The page may render price rows after the initial HTML response.
            try:
                page.wait_for_selector(
                    ".prices_offline_col, [class*='price'], [class*='Price']",
                    timeout=10000,
                    state="attached",
                )
            except Exception:
                # The parser below will report a useful diagnostic if no prices
                # were rendered, instead of hiding the original failure.
                pass

            return page.content()
        finally:
            browser.close()


def parse_price(value):
    """Parse Serbian prices such as '1.234,56', '699,99' or '1 234'."""
    text = value.replace("\xa0", " ").strip()
    match = re.search(r"\d[\d\s.]*[,\.]?\d*", text)
    if not match:
        raise ValueError(f"No numeric price found in {value!r}")

    number = re.sub(r"\s+", "", match.group(0))
    if "," in number:
        number = number.replace(".", "").replace(",", ".")
    elif number.count(".") > 1:
        number = number.replace(".", "")
    elif "." in number:
        left, right = number.rsplit(".", 1)
        # A single dot followed by three digits is a thousands separator.
        if len(right) == 3:
            number = left + right

    try:
        return float(Decimal(number))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid price value {value!r}") from exc


def _seller_name(row):
    image = row.find("img", alt=True)
    if image:
        return image.get("alt", "").strip()

    for attribute in ("data-seller", "data-store", "aria-label", "title"):
        value = row.get(attribute)
        if value:
            return value.strip()

    return ""


def _price_text(row):
    # Keep the old selector first, then try common alternatives used by
    # redesigned product pages.
    selectors = (
        ".product_price",
        "[class*='product_price']",
        "[class*='price-value']",
        "[class*='price_value']",
        "[class*='price']",
        "[class*='Price']",
    )
    for selector in selectors:
        element = row.select_one(selector)
        if element:
            text = element.get_text(" ", strip=True)
            if re.search(r"\d", text):
                return text

    text = row.get_text(" ", strip=True)
    return text if re.search(r"\d", text) else ""


def fetch_product_prices(url):
    try:
        html = fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")
        product_prices = {}

        # Original Cenoteka markup.
        legacy_rows = soup.select("div.prices_offline_col")

        # Fallback for changed markup: inspect price-related containers that
        # also identify a seller, avoiding unrelated page prices.
        candidate_rows = list(legacy_rows)
        if not candidate_rows:
            for element in soup.find_all(
                lambda tag: tag.name in {"div", "li", "article", "tr"}
                and any(
                    "price" in str(cls).lower()
                    for cls in (tag.get("class") or [])
                )
            ):
                if _seller_name(element):
                    candidate_rows.append(element)

        seen = set()
        for row in candidate_rows:
            # Nested price containers can describe the same seller more than
            # once. Process each seller only once.
            seller = _seller_name(row)
            if not seller:
                continue

            normalized_seller = seller.casefold()
            if normalized_seller in EXCLUDED_SELLERS or seller in seen:
                continue

            price_text = _price_text(row)
            if not price_text:
                continue

            try:
                price = parse_price(price_text)
            except ValueError:
                continue

            product_prices[seller] = price
            seen.add(seller)

        if not product_prices:
            title = soup.title.get_text(" ", strip=True) if soup.title else "unknown"
            price_classes = sorted({
                str(cls)
                for tag in soup.find_all(True)
                for cls in (tag.get("class") or [])
                if "price" in str(cls).lower()
            })
            raise RuntimeError(
                "No seller prices could be parsed. "
                f"Page title: {title!r}; price-related classes: "
                f"{price_classes[:30]}; URL: {url}"
            )

        return product_prices

    except Exception as exc:
        print(f"Error fetching product prices from {url}: {exc}")
        return None


def send_message(report):
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    webhook = DiscordWebhook(url=webhook_url, content=report)
    return webhook.execute()


if __name__ == "__main__":
    url = os.environ["CENOTEKA_URL"]
    product_prices = fetch_product_prices(url)

    if product_prices is None:
        send_message(
            "Error fetching product prices. Check the GitHub Actions logs "
            "for the exact HTTP/parsing error."
        )
        raise SystemExit(1)

    threshold_price = float(os.environ["THRESHOLD_PRICE"])
    low_prices = {
        seller: price
        for seller, price in product_prices.items()
        if price <= threshold_price
    }

    if not low_prices:
        print(
            f"No prices at or below {threshold_price} RSD. "
            f"Successfully parsed {len(product_prices)} sellers."
        )
        raise SystemExit(0)

    report = f"Low prices found (<= {threshold_price} RSD):\n"
    for seller, price in low_prices.items():
        report += f"{seller} - {price:.2f} RSD\n"

    send_message(report)
