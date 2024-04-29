import os
import requests
from bs4 import BeautifulSoup
from discord_webhook import DiscordWebhook

# Function to fetch data from the URL and extract product prices


def fetch_product_prices(url):
    try:
        # Send a GET request to the URL
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()  # Raise an exception for any bad response status

        # Parse the HTML content of the response
        soup = BeautifulSoup(response.content, 'html.parser')

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

    except requests.exceptions.RequestException as e:
        print(f"Error fetching data: {e}")
        return []

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
    threshold_price = os.environ["THRESHOLD_PRICE"]

    if product_prices:
        low_prices = {seller: price for seller,
                      price in product_prices.items() if price <= threshold_price}

        if low_prices:
            # Prepare report with low prices
            report = f"Low prices found (<= {threshold_price} RSD):\n"
            for seller, price in low_prices.items():
                report += f"{seller} - {price} RSD\n"

            # Send messsage with the report
            send_message(report)
            print("Report sent successfully!")
        else:
            print("No prices found below threshold.")
    else:
        print("No product prices found on the page.")
