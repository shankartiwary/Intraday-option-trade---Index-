import streamlit as st
import pandas as pd
from nsepython import nse_optionchain_scrapper
import time

st.set_page_config(layout="wide")

def fetch_option_chain_data():
    """
    Fetches Nifty 50 option chain data using the nsepython library.
    """
    try:
        data = nse_optionchain_scrapper("NIFTY")
        return data
    except Exception as e:
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
