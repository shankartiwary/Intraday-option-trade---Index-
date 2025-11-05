import streamlit as st
import pandas as pd
import requests
import time
from bs4 import BeautifulSoup

st.set_page_config(layout="wide")

def fetch_option_chain_data():
    """
    Fetches Nifty 50 option chain data from the NSE website.
    """
    url = 'https://www1.nseindia.com/live_market/dynaContent/live_watch/option_chain/optionKeys.jsp?symbol=NIFTY&date=-'
    headers = {
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.149 Safari/537.36',
        'accept-language': 'en,gu;q=0.9,hi;q=0.8',
        'accept-encoding': 'gzip, deflate, br'
    }
    try:
        session = requests.Session()
        request = session.get(url, headers=headers, timeout=5)
        cookies = dict(request.cookies)
        response = session.get(url, headers=headers, timeout=5, cookies=cookies)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'lxml')
        table = soup.find('table', {'id': 'octable'})
        rows = table.find_all('tr')
        data = []
        for row in rows[2:]:
            cells = row.find_all('td')
            if len(cells) > 21:
                data.append({
                    'CE_OI': cells[1].text.strip(),
                    'CE_CHNG_IN_OI': cells[2].text.strip(),
                    'CE_VOLUME': cells[3].text.strip(),
                    'CE_LTP': cells[5].text.strip(),
                    'CE_CHNG': cells[6].text.strip(),
                    'STRIKE_PRICE': cells[11].text.strip(),
                    'PE_CHNG': cells[16].text.strip(),
                    'PE_LTP': cells[17].text.strip(),
                    'PE_VOLUME': cells[19].text.strip(),
                    'PE_CHNG_IN_OI': cells[20].text.strip(),
                    'PE_OI': cells[21].text.strip(),
                })
        records = []
        for item in data:
            records.append({
                'strikePrice': float(item['STRIKE_PRICE'].replace(',', '')),
                'CE': {
                    'openInterest': float(item['CE_OI'].replace(',', '')),
                    'changeinOpenInterest': float(item['CE_CHNG_IN_OI'].replace(',', '')),
                    'totalTradedVolume': float(item['CE_VOLUME'].replace(',', '')),
                    'lastPrice': float(item['CE_LTP'].replace(',', '')),
                    'change': float(item['CE_CHNG'].replace(',', '')),
                },
                'PE': {
                    'openInterest': float(item['PE_OI'].replace(',', '')),
                    'changeinOpenInterest': float(item['PE_CHNG_IN_OI'].replace(',', '')),
                    'totalTradedVolume': float(item['PE_VOLUME'].replace(',', '')),
                    'lastPrice': float(item['PE_LTP'].replace(',', '')),
                    'change': float(item['PE_CHNG'].replace(',', '')),
                }
            })

        # Now I need to find the spot price. It's in a span with id="spotPrice"
        spot_price_span = soup.find('span', {'id': 'spotPrice'})
        spot_price = float(spot_price_span.text.replace(',', '')) if spot_price_span else 0

        return {'records': {'data': records, 'underlyingValue': spot_price}}
    except requests.exceptions.RequestException as e:
        st.error(f"Error fetching data from NSE: {e}")
        return None

def main():
    st.title("Nifty 50 Option Chain Analysis")

    placeholder = st.empty()

    while True:
        with placeholder.container():
            data = fetch_option_chain_data()

            if data:
                spot_price = data['records']['underlyingValue']
                st.write(f"Nifty 50 Spot Price: **{spot_price}**")
                st.write(f"Last updated: {time.strftime('%H:%M:%S')}")

                # Extract strikes
                strikes = [record['strikePrice'] for record in data['records']['data']]

                # Find 4 strikes above and below the spot price
                above_strikes = sorted([s for s in strikes if s > spot_price])[:4]
                below_strikes = sorted([s for s in strikes if s < spot_price], reverse=True)[:4]

                relevant_strikes = sorted(above_strikes + below_strikes)

                # Analyze and classify strikes
                analyzed_data = analyze_strikes(data, relevant_strikes)

                if analyzed_data:
                    df = pd.DataFrame(analyzed_data)
                    df = df.sort_values(by='Strength', ascending=False).head(3)
                    st.table(df.drop(columns=['Strength']))
                else:
                    st.info("No strikes met the specified buildup conditions.")

            time.sleep(300) # Refresh every 5 minutes

def analyze_strikes(data, strikes):
    results = []
    for record in data['records']['data']:
        if record['strikePrice'] in strikes:
            for option_type in ['CE', 'PE']:
                if option_type in record:
                    option = record[option_type]
                    oi_change = option['changeinOpenInterest']
                    price_change = option['change']
                    volume = option['totalTradedVolume']

                    condition = None
                    if oi_change > 0 and price_change > 0 and volume > 0:
                        condition = "Long Buildup"
                    elif oi_change > 0 and price_change < 0 and volume > 0:
                        condition = "Short Buildup"
                    elif oi_change < 0 and price_change > 0 and volume > 0:
                        condition = "Short Covering"
                    elif oi_change < 0 and price_change < 0 and volume > 0:
                        condition = "Long Unwinding"

                    if condition:
                        strength_score = abs(oi_change) + abs(price_change) + volume
                        results.append({
                            'Strike': record['strikePrice'],
                            'Option Type': option_type,
                            'Condition': condition,
                            'Strength': strength_score
                        })
    return results

if __name__ == "__main__":
    main()
