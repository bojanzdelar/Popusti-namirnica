import os
from bs4 import BeautifulSoup
from discord_webhook import DiscordWebhook
from playwright.sync_api import sync_playwright


def fetch_html(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
            locale="en-US"
        )

        page = context.new_page()

        page.goto(url, wait_until="domcontentloaded")

        page.wait_for_timeout(2000)

        html = page.content()

        browser.close()
        return html


def fetch_product_prices(url):
    try:
        html = fetch_html(url)

        soup = BeautifulSoup(html, 'html.parser')

        product_prices = {}

        for div in soup.find('div', class_='prices_offline_col'):
            seller = div.find('img')['alt']
            price_str = div.find(
                'div', class_='product_price').get_text(strip=True)

            price = float(price_str.replace(',', '.'))

            if seller not in ["Dis", "Tempo"]:
                product_prices[seller] = price

        return product_prices

    except Exception as e:
        print(f"Error fetching data: {e}")
        return None


def send_message(report):
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    webhook = DiscordWebhook(url=webhook_url, content=report)
    return webhook.execute()


if __name__ == "__main__":
    url = os.environ["CENOTEKA_URL"]

    product_prices = fetch_product_prices(url)

    if product_prices is None:
        send_message("Error fetching product prices.")
        exit()

    threshold_price = float(os.environ["THRESHOLD_PRICE"])

    low_prices = {
        seller: price
        for seller, price in product_prices.items()
        if price <= threshold_price
    }

    if not low_prices:
        exit()

    report = f"Low prices found (<= {threshold_price} RSD):\n"
    for seller, price in low_prices.items():
        report += f"{seller} - {price} RSD\n"

    send_message(report)
