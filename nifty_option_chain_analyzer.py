import streamlit as st
import pandas as pd
import httpx
import time

st.set_page_config(layout="wide")

def fetch_option_chain_data():
    """
    Fetches Nifty 50 option chain data from the NSE website.
    """
    base_url = "https://www.nseindia.com/option-chain"
    api_url = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
    headers = {
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.149 Safari/537.36',
        'accept-language': 'en,gu;q=0.9,hi;q=0.8',
        'accept-encoding': 'gzip, deflate, br'
    }
    try:
        with httpx.Client(http2=True) as client:
            client.get(base_url, headers=headers)
            response = client.get(api_url, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.RequestError as e:
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
                if option_type in record and 'underlying' in record[option_type]: # Check for underlying key
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
