import ccxt
import time
import logging
from binance import ThreadedWebsocketManager
import asyncio
from datetime import datetime
import threading
from functools import wraps
import os
from dotenv import load_dotenv  # Import load_dotenv to load .env files
load_dotenv()

# Set event loop policy for Windows
if hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Logging configuration
logging.basicConfig(level=logging.INFO)

# API Credentials and Exchange Configuration
API_KEY = os.environ.get('API_KEY')
API_SECRET = os.environ.get('SECRET_KEY')
TESTNET = False  # Set to True for testnet

exchange_config = {
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {
        'adjustForTimeDifference': True,
        'defaultType': 'spot',
    },
}

if TESTNET:
    exchange_config['urls'] = {
        'api': {
            'public': 'https://testnet.binance.vision/api/v3',
            'private': 'https://testnet.binance.vision/api/v3',
        }
    }

exchange = ccxt.binance(exchange_config)

# Load time difference
try:
    exchange.load_time_difference()
    logging.info("Time synchronization loaded successfully.")
except ccxt.BaseError as e:
    logging.error(f"Error loading time difference: {e}")

# Global Variables
last_is_closed = False
first_run = True
total_profit_or_loss = 0
has_open_trade = False
fixed_profit_or_loss = 0
initial_balance = 2  # Initial balance (example)

# New Global Variables for Tracking Prices
last_buy_price = None
last_sell_price = None

# Retry Decorator
def retry(retries=3, delay=2, exceptions=(Exception,)):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(1, retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    logging.error(f"Error in {func.__name__} (attempt {attempt}): {e}")
                    if attempt < retries:
                        time.sleep(delay)
                    else:
                        logging.error(f"All {retries} attempts failed for {func.__name__}.")
                        return None
        return wrapper
    return decorator

# Utility Functions
@retry(retries=3, delay=2, exceptions=(ccxt.NetworkError, ccxt.ExchangeError, ValueError))
def sync_time():
    server_time = exchange.fetch_time()  # Returns time in milliseconds
    server_timestamp = server_time // 1000  # Convert to seconds
    local_timestamp = int(time.time())
    time_difference = server_timestamp - local_timestamp
    logging.info(f"Time difference: {time_difference} seconds")
    return time_difference

@retry(retries=3, delay=2, exceptions=(ccxt.NetworkError, ccxt.ExchangeError, ValueError))
def calculate_trade_amount(symbol, usd_amount):
    ticker = exchange.fetch_ticker(symbol)
    current_price = ticker['last']
    if current_price <= 0:
        raise ValueError("Invalid price value.")
    logging.info(f"Current price for {symbol}: {current_price}")
    return usd_amount / current_price

@retry(retries=3, delay=2, exceptions=(ccxt.NetworkError, ccxt.ExchangeError))
def fetch_balance():
    balance = exchange.fetch_balance()
    usdt = balance['total'].get('USDT', 0)
    asset = balance['total'].get('TROY', 0)
    logging.info(f"USDT Balance: {usdt}, Asset Balance: {asset}")
    return usdt, asset

@retry(retries=3, delay=2, exceptions=(ccxt.NetworkError, ccxt.ExchangeError, ValueError))
def fetch_order_book(symbol):
    depth = exchange.fetch_order_book(symbol, limit=5)
    if not depth or 'bids' not in depth or 'asks' not in depth:
        raise ValueError("Invalid order book data.")
    highest_bid = sum([bid[1] for bid in depth['bids']])
    lowest_ask = sum([ask[1] for ask in depth['asks']])
    logging.info(f"Market liquidity - Bid: {highest_bid}, Ask: {lowest_ask}")
    return highest_bid, lowest_ask

@retry(retries=3, delay=2, exceptions=(ccxt.NetworkError, ccxt.ExchangeError, ValueError))
def get_current_candle(symbol, timeframe='1m'):
    candles = exchange.fetch_ohlcv(symbol, timeframe)
    if not candles:
        raise ValueError("No candles returned.")
    latest_candle = candles[-1]
    open_price, high_price, low_price, close_price, volume = latest_candle[1:6]
    if close_price > open_price:
        color = 'Green'
    elif close_price < open_price:
        color = 'Red'
    else:
        color = 'Neutral'
    logging.info(f"Latest candle for {symbol} on {timeframe}: Open: {open_price}, High: {high_price}, Low: {low_price}, Close: {close_price}, Volume: {volume}, Color: {color}")
    return {
        'open': open_price,
        'close': close_price,
        'color': color,
    }

def execute_order(order_type, symbol, quantity, price=None):
    global last_buy_price, last_sell_price, total_profit_or_loss, initial_balance
    try:
        if order_type == 'buy':
            logging.info(f"Executing Buy order for {quantity} of {symbol} at price {price}")
            # Uncomment the line below to enable actual order execution
            # order = exchange.create_market_buy_order(symbol, quantity)
            last_buy_price = price  # Record the buy price
            logging.info(f"Buy order executed for {quantity} of {symbol} at price {price}")
        elif order_type == 'sell':
            logging.info(f"Executing Sell order for {quantity} of {symbol} at price {price}")
            # Uncomment the line below to enable actual order execution
            # order = exchange.create_market_sell_order(symbol, quantity)
            last_sell_price = price  # Record the sell price
            # Calculate profit or loss based on last buy price
            if last_buy_price:
                profit_or_loss = (price - last_buy_price) * quantity
                total_profit_or_loss += profit_or_loss
                initial_balance += profit_or_loss
                logging.info(f"Sell order executed for {quantity} of {symbol} at price {price}. Profit/Loss: {profit_or_loss}. Total Profit: {total_profit_or_loss}")
            else:
                logging.warning("Sell order executed without a recorded buy price.")
    except (ccxt.NetworkError, ccxt.ExchangeError) as e:
        logging.error(f"{order_type.capitalize()} Order Error: {e}")
    except Exception as e:
        logging.error(f"Unexpected error during {order_type} order: {e}")

def get_sleep_time(timeframe):
    unit = timeframe[-1]
    value = int(timeframe[:-1])
    if unit == 'm':
        return value * 60
    elif unit == 'h':
        return value * 3600
    elif unit == 'd':
        return value * 86400
    else:
        return 60  # Default to 1 minute

# WebSocket Message Handler
def handle_socket_message(msg):
    global last_is_closed, first_run, total_profit_or_loss, has_open_trade, fixed_profit_or_loss

    try:
        if 'k' not in msg or 's' not in msg:
            raise KeyError("Missing candle data ('k') or symbol ('s') in message.")

        candle = msg['k']
        timestamp = candle['t']
        is_closed = candle['x']
        open_price = float(candle['o'])
        close_price = float(candle['c'])
        symbol_ws = msg['s']
    except KeyError as e:
        logging.error(f"KeyError: {e}")
        return
    except ValueError as e:
        logging.error(f"ValueError: {e}")
        return
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        return

    if None in [timestamp, symbol_ws]:
        logging.error("Missing critical data in message.")
        return

    readable_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp / 1000))
    has_open_trade = total_profit_or_loss != 0

    if is_closed:
        if not has_open_trade:
            total_profit_or_loss += fixed_profit_or_loss
            has_open_trade = True
            logging.info(f"Trade opened at {readable_time}, Profit/Loss: {fixed_profit_or_loss}")
        else:
            total_profit_or_loss += fixed_profit_or_loss
            has_open_trade = False
            logging.info(f"Trade closed at {readable_time}, Total Profit/Loss: {total_profit_or_loss}")

# Initialize WebSocket
def start_websocket():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    twm = ThreadedWebsocketManager(API_KEY, API_SECRET, testnet=TESTNET)
    twm.start()
    twm.start_kline_socket(callback=handle_socket_message, symbol='TROYUSDT', interval='1m')
    twm.join()
# Start WebSocket in a separate thread
ws_thread = threading.Thread(target=start_websocket)
ws_thread.start()

# Trade Execution Logic
def trade_logic():
    global first_run, initial_balance, has_open_trade, total_profit_or_loss, last_buy_price, last_sell_price

    symbol = 'TROY/USDT'
    timeframe = '1m'
    liquidity_threshold = 5
    last_color = None
    attempt_counter = 0
    max_attempts = 10

    time_diff = sync_time()

    while attempt_counter < max_attempts:
        try:
            if time_diff:
                current_timestamp = int(time.time()) + time_diff
                # exchange.options['timestamp'] = current_timestamp  # Uncomment if needed

            usdt_balance, asset_balance = fetch_balance()
            if usdt_balance < initial_balance:
                logging.warning(f"Insufficient USDT balance: {usdt_balance}. Cannot execute trade.")
                attempt_counter += 1
                time.sleep(5)
                continue

            highest_bid, lowest_ask = fetch_order_book(symbol)
            if highest_bid < liquidity_threshold or lowest_ask < liquidity_threshold:
                logging.warning(f"Insufficient liquidity. Bid: {highest_bid}, Ask: {lowest_ask}")
                attempt_counter += 1
                time.sleep(5)
                continue

            current_candle = get_current_candle(symbol, timeframe)
            if not current_candle:
                logging.error('Failed to fetch the latest candle.')
                attempt_counter += 1
                continue

            open_price = current_candle['open']
            close_price = current_candle['close']
            current_color = current_candle['color']

            if first_run:
                logging.info(f"[{datetime.now()}] - Initial Balance: {initial_balance}")
                first_run = False

            logging.info(f"Current candle color: {current_color}")

            trade_quantity = calculate_trade_amount(symbol, initial_balance)
            if not trade_quantity:
                logging.error("Invalid trade quantity.")
                attempt_counter += 1
                continue

            # Implementing the Buy/Sell Conditions
            if current_color != 'Neutral' and current_color != last_color:
                if current_color == 'Green' and not has_open_trade:
                    # **Buy Condition**: Current Buy Price < Last Sell Price
                    execute_order('buy', symbol, trade_quantity, price=close_price)
                    has_open_trade = True
                elif current_color == 'Red' and has_open_trade:
                    # **Sell Condition**: Current Sell Price > Last Buy Price
                    if last_buy_price is not None and close_price > last_buy_price:
                        execute_order('sell', symbol, trade_quantity, price=close_price)
                        has_open_trade = False
                    else:
                        logging.info(f"Sell condition not met. Current price {close_price} is not greater than last buy price {last_buy_price}.")
                last_color = current_color
            else:
                logging.info(f"No change in candle color ({current_color}).")

            sleep_time = get_sleep_time(timeframe)
            logging.info(f"Sleeping for {sleep_time} seconds...")
            time.sleep(sleep_time)

        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logging.error(f"Exchange Error in main loop: {e}")
            time.sleep(5)
            attempt_counter += 1
        except Exception as e:
            logging.error(f"Unexpected error in main loop: {e}")
            time.sleep(5)
            attempt_counter += 1

    logging.info(f"Max attempts reached ({max_attempts}). Exiting the loop.")

# Start the trade logic in a separate thread
trade_thread = threading.Thread(target=trade_logic)
trade_thread.start()
