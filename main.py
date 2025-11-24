import os
from urllib.request import Request, urlopen
from bs4 import BeautifulSoup
from discord_webhook import DiscordWebhook

# Function to fetch data from the URL and extract product prices


def fetch_product_prices(url):
    try:
        # Send a GET request to the URL
        request = Request(url, headers={
                      "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"})
        response = urlopen(request).read().decode('utf-8')

        # Parse the HTML content of the response
        soup = BeautifulSoup(response, 'html.parser')

        # Extract the seller names and product prices
        product_prices = {}
        for div in soup.find('div', class_='prices_offline_col'):
            # Get the 'alt' attribute of the 'img' tag
            seller = div.find('img')['alt']
            price_str = div.find('div', class_='product_price').get_text(
                strip=True)  # Get the text content of the div
            # Extract numeric values from the price string
            price = float(price_str.replace(',', '.'))

            if seller not in ["Dis", "Tempo"]:
                product_prices[seller] = price

        return product_prices

    except Exception as e:
        print(f"Error fetching data: {e}")
        return None

# Function send message to Discord channel with the report


def send_message(report):
    # Replace the URL with your Discord webhook URL
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    webhook = DiscordWebhook(url=webhook_url, content=report)
    response = webhook.execute()
    return response


if __name__ == "__main__":
    url = os.environ["CENOTEKA_URL"]
    product_prices = fetch_product_prices(url)

    if product_prices is None:
        send_message("Error fetching product prices.")
        exit()

    threshold_price = float(os.environ["THRESHOLD_PRICE"])

    low_prices = {seller: price for seller,
                  price in product_prices.items() if price <= threshold_price}

    if not low_prices:
        exit()

    # Prepare report with low prices
    report = f"Low prices found (<= {threshold_price} RSD):\n"
    for seller, price in low_prices.items():
        report += f"{seller} - {price} RSD\n"

    # Send messsage with the report
    send_message(report)
