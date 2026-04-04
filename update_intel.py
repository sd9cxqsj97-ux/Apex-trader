import requests

# URL to download the HTML file
url = 'https://gist.githubusercontent.com/sd9cxqsj97-ux/adb497c8cb1122ee2f52af339a61eb41/raw/index.html'

# Download the content from the URL
response = requests.get(url)

# Save the content to a file
with open('apex-intel-v3.html', 'w', encoding='utf-8') as file:
    # Replace occurrences of the specified URLs
    content = response.text.replace('https://api-fxpractice.oanda.com/v3', 'http://localhost:5000/oanda')
    content = content.replace('https://api.binance.com/api/v3', 'http://localhost:5000/binance')
    file.write(content)

print("apex-intel-v3.html has been updated and saved.")
