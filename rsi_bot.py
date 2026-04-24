import telebot
import numpy as np
import time
import os
import logging
import sqlite3
import json
import ccxt
import gc
from datetime import datetime, timezone, timedelta
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')
logging.getLogger('apscheduler.executors.default').setLevel(logging.WARNING)
logging.getLogger('apscheduler.scheduler').setLevel(logging.WARNING)

# === НАСТРОЙКИ v8.12 (Macro Trend Filter Disabled) ===
DB_PATH = 'bot.db'  
TOKEN = os.getenv('TELEGRAM_TOKEN')
GROUP_CHAT_ID = int(os.getenv('GROUP_CHAT_ID', -1003407154454))
BINGX_API_KEY = os.getenv('BINGX_API_KEY')
BINGX_SECRET = os.getenv('BINGX_SECRET')

RISK_PER_TRADE = 0.02       
MAX_POSITIONS = 3           
LEVERAGE = 10               
MAX_SPREAD_PERCENT = 0.002  
MIN_VOLUME_USDT = 2500000
COOLDOWN_CACHE = {}

TRADE_TIMEOUT_PROFIT_HOURS = 1.5  
TRADE_TIMEOUT_ANY_HOURS = 3.0     

SMC_TIMEFRAME = '15m'       
MIN_RR_RATIO = 1.5          
MIN_SL_PCT = 1.2            
MAX_SL_PCT = 4.0   

EXCLUDED_KEYWORDS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT', 'NCS', 'NCFX', 'NCCO', 'NCSI', 'NIKKEI', 'NASDAQ', 'SP500', 'GOLD', 'SILVER', 'AUT', 'XAU', 'PAXG', 'EUR', '1000', 'LUNC', 'USTC', 'USDC']

GLOBAL_STOP_UNTIL = None
CONSECUTIVE_LOSSES = 0
START_OF_DAY_BALANCE = 0.0

daily_stats = {
    'trades': 0,
    'wins': 0,       
    'sl_hits': 0,    
    'timeouts': 0,
    'pnl': 0.0,
    'prev_winrate': 0.0
}
active_positions = []

bot = telebot.TeleBot(TOKEN)
exchange = ccxt.bingx({
    'apiKey': BINGX_API_KEY, 'secret': BINGX_SECRET,
    'options': {'defaultType': 'swap', 'marginMode': 'isolated'}, 'enableRateLimit': True
})

def get_real_balance():
    try:
        bal = exchange.fetch_balance()
        return float(bal['USDT']['total']) 
    except:
        return 0.0

def get_db_conn(): return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    conn = get_db_conn(); c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS active_positions (id INTEGER PRIMARY KEY, data TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_stats (id INTEGER PRIMARY KEY, pnl REAL, trades INTEGER, wins INTEGER)''')
    try: c.execute("ALTER TABLE daily_stats ADD COLUMN prev_winrate REAL DEFAULT 0.0")
    except: pass
    conn.commit(); conn.close()

def save_positions():
    try:
        conn = get_db_conn(); c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO active_positions (id, data) VALUES (1, ?)", (json.dumps(active_positions),))
        c.execute("INSERT OR REPLACE INTO daily_stats (id, pnl, trades, wins, prev_winrate) VALUES (1, ?, ?, ?, ?)", 
                  (daily_stats.get('pnl', 0.0), daily_stats['trades'], daily_stats['wins'], daily_stats.get('prev_winrate', 0.0)))
        conn.commit(); conn.close()
    except Exception as e: logging.error(f"Save error: {e}")

def load_positions():
    global active_positions, daily_stats
    try:
        conn = get_db_conn(); c = conn.cursor()
        c.execute("SELECT data FROM active_positions WHERE id = 1"); row = c.fetchone()
        if row: active_positions = json.loads(row[0])
        c.execute("SELECT pnl, trades, wins, prev_winrate FROM daily_stats WHERE id = 1"); stat_row = c.fetchone()
        if stat_row: 
            daily_stats['pnl'], daily_stats['trades'], daily_stats['wins'], daily_stats['prev_winrate'] = stat_row
        conn.close()
    except Exception: pass

def calculate_rsi(prices, window=14):
    if len(prices) < window: return 50
    diffs = np.diff(prices)
    gains = np.maximum(diffs, 0); losses = np.abs(np.minimum(diffs, 0))
    avg_gain = np.mean(gains[-window:]); avg_loss = np.mean(losses[-window:])
    if avg_loss == 0: return 100
    return 100 - (100 / (1 + (avg_gain / avg_loss)))

def get_market_context():
    try:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT', timeframe='15m', limit=50)
        c = np.array([x[4] for x in ohlcv], dtype=float)
        trend = 'Long' if c[-1] > np.mean(c[-21:-1]) else 'Short' 
        return trend, True
    except: return 'Neutral', False

def execute_trade(sym, direction, current_price, sl_price, tp1_price, atr):
    global COOLDOWN_CACHE
    if len(active_positions) >= MAX_POSITIONS or any(p['symbol'] == sym for p in active_positions): return
    if sym in COOLDOWN_CACHE and time.time() < COOLDOWN_CACHE[sym]: return
        
    try:
        bal = exchange.fetch_balance()
        free_usdt = float(bal['USDT']['free'])
        if free_usdt <= 0: return

        actual_sl_dist = abs(current_price - sl_price)
        risk_amount = free_usdt * RISK_PER_TRADE
        
        contract_size = float(exchange.market(sym).get('contractSize', 1.0))
        qty_coins = risk_amount / actual_sl_dist if actual_sl_dist > 0 else 0
        qty = float(exchange.amount_to_precision(sym, qty_coins / contract_size if contract_size > 0 else qty_coins))
        
        pos_side = 'LONG' if direction == 'Long' else 'SHORT'
        exchange.set_leverage(LEVERAGE, sym, params={'side': pos_side})
        exchange.create_market_order(sym, 'buy' if direction == 'Long' else 'sell', qty, params={'positionSide': pos_side})
        sl_ord = exchange.create_order(sym, 'stop_market', 'sell' if direction == 'Long' else 'buy', qty, params={'triggerPrice': sl_price, 'positionSide': pos_side, 'stopLossPrice': sl_price})
        
        active_positions.append({
            'symbol': sym, 'direction': direction, 'entry_price': current_price, 'initial_qty': qty, 
            'sl_price': sl_price, 'tp1': tp1_price, 'tp1_hit': False, 'sl_order_id': sl_ord['id'], 
            'position_side': pos_side, 'open_time': datetime.now(timezone.utc).isoformat(), 'atr': atr
        })
        save_positions()
        
        sl_pct = (actual_sl_dist / current_price) * 100
        bot.send_message(GROUP_CHAT_ID, f"💥 **ВЫСТРЕЛ [RSI Bot v8.12]: {sym.split(':')[0]}**\nНаправление: **#{direction.upper()}**\n\nЦена: {current_price}\nОбъем: {qty} контр.\nSL: {sl_price} ({sl_pct:.2f}%)\nTP1: {tp1_price}")
    except Exception as e: logging.error(f"Trade error {sym}: {e}")

def monitor_positions_job():
    global active_positions, daily_stats, CONSECUTIVE_LOSSES, COOLDOWN_CACHE
    try:
        if not active_positions: return
        positions_raw = exchange.fetch_positions()
        symbols_to_fetch = [p['symbol'] for p in active_positions]
        tickers = exchange.fetch_tickers(symbols_to_fetch)
        
        updated = []
        for pos in active_positions:
            sym = pos['symbol']
            clean_name = sym.split(':')[0]
            is_long = pos['direction'] == 'Long'
            curr = next((r for r in positions_raw if r['symbol'] == sym and float(r.get('contracts', 0)) > 0), None)
            ticker = tickers.get(sym, {}).get('last', pos['entry_price'])
            
            if not curr:
                pnl = (ticker - pos['entry_price']) * pos['initial_qty'] if is_long else (pos['entry_price'] - ticker) * pos['initial_qty']
                daily_stats['trades'] += 1
                
                if pnl > 0: 
                    daily_stats['wins'] += 1
                    CONSECUTIVE_LOSSES = 0
                    bot.send_message(GROUP_CHAT_ID, f"✅ **{clean_name} закрыта в плюс!**\nPNL: +{pnl:.2f} USDT")
                else: 
                    daily_stats['sl_hits'] = daily_stats.get('sl_hits', 0) + 1
                    CONSECUTIVE_LOSSES += 1
                    COOLDOWN_CACHE[sym] = time.time() + 14400
                    bot.send_message(GROUP_CHAT_ID, f"🛑 **{clean_name} выбита по SL.**\nPNL: {pnl:.2f} USDT\n❄️ Монета заморожена на 4 часа.")
                continue

            if 'open_time' not in pos: pos['open_time'] = datetime.now(timezone.utc).isoformat()
            hours_passed = (datetime.now(timezone.utc) - datetime.fromisoformat(pos['open_time'])).total_seconds() / 3600
            pnl = (ticker - pos['entry_price']) * float(curr['contracts']) if is_long else (pos['entry_price'] - ticker) * float(curr['contracts'])

            if hours_passed >= TRADE_TIMEOUT_ANY_HOURS or (hours_passed >= TRADE_TIMEOUT_PROFIT_HOURS and pnl > 0):
                try:
                    exchange.create_market_order(sym, 'sell' if is_long else 'buy', float(curr['contracts']), params={'positionSide': pos['position_side']})
                    if pos.get('sl_order_id'): exchange.cancel_order(pos['sl_order_id'], sym)
                    
                    daily_stats['trades'] += 1
                    daily_stats['timeouts'] = daily_stats.get('timeouts', 0) + 1 
                    
                    if pnl > 0: 
                        daily_stats['wins'] += 1
                        CONSECUTIVE_LOSSES = 0
                    else: 
                        CONSECUTIVE_LOSSES += 1
                        COOLDOWN_CACHE[sym] = time.time() + 14400
                        
                    bot.send_message(GROUP_CHAT_ID, f"{'✅' if pnl > 0 else '🛑'} **{clean_name} закрыта по ТАЙМАУТУ!**\nPNL: {pnl:.2f} USDT")
                    continue
                except: pass

            try:
                ohlcv = exchange.fetch_ohlcv(sym, timeframe='1m', limit=2)
                if ohlcv and len(ohlcv) > 0:
                    high_p = max([float(c[2]) for c in ohlcv])
                    low_p = min([float(c[3]) for c in ohlcv])
                else:
                    high_p = low_p = ticker
            except:
                high_p = low_p = ticker
            
            if not pos.get('tp1_hit'):
                if (is_long and high_p >= pos['tp1']) or (not is_long and low_p <= pos['tp1']):
                    try:
                        close_qty = float(exchange.amount_to_precision(sym, float(curr['contracts']) * 0.50))
                        if close_qty > 0: exchange.create_market_order(sym, 'sell' if is_long else 'buy', close_qty, params={'positionSide': pos['position_side']}); pos['initial_qty'] = float(curr['contracts']) - close_qty 
                        if pos.get('sl_order_id'): exchange.cancel_order(pos['sl_order_id'], sym)
                        
                        be_p = float(exchange.price_to_precision(sym, pos['entry_price'] * (1.002 if is_long else 0.998)))
                        new_sl = exchange.create_order(sym, 'stop_market', 'sell' if is_long else 'buy', pos['initial_qty'], params={'triggerPrice': be_p, 'positionSide': pos['position_side'], 'stopLossPrice': be_p})
                        
                        atr_step = pos.get('atr', abs(pos['entry_price'] - pos['sl_price']) * 0.5)
                        pos.update({
                            'tp1_hit': True, 
                            'sl_order_id': new_sl['id'], 
                            'micro_step': atr_step * 0.3, 
                            'trail_distance': atr_step * 0.8, 
                            'trail_trigger': pos['tp1'] + (atr_step * 0.3 * (1 if is_long else -1))
                        })
                        bot.send_message(GROUP_CHAT_ID, f"💰 **{clean_name} TP1 взят!** (50% закрыто).\n🛡 Запущен Трейлинг.")
                    except: pass
            
            if pos.get('tp1_hit'):
                tt = pos.get('trail_trigger')
                ms = pos.get('micro_step')
                td = pos.get('trail_distance')
                
                if tt and ms and td:
                    if (is_long and high_p >= tt) or (not is_long and low_p <= tt):
                        try:
                            if pos.get('sl_order_id'): exchange.cancel_order(pos['sl_order_id'], sym)
                            nsl = float(exchange.price_to_precision(sym, tt - td if is_long else tt + td))
                            new_sl_order = exchange.create_order(sym, 'stop_market', 'sell' if is_long else 'buy', pos['initial_qty'], params={'triggerPrice': nsl, 'positionSide': pos['position_side'], 'stopLossPrice': nsl})
                            
                            pos.update({
                                'sl_order_id': new_sl_order['id'], 
                                'trail_trigger': tt + ms if is_long else tt - ms
                            })
                        except: pass

            updated.append(pos)
        active_positions = updated
        save_positions()
    except Exception: pass

def run_market_scan():
    global GLOBAL_STOP_UNTIL
    try: exchange.load_markets()
    except: pass
    
    while True:
        try:
            if GLOBAL_STOP_UNTIL and datetime.now(timezone.utc) < GLOBAL_STOP_UNTIL:
                time.sleep(60); continue
            if len(active_positions) >= MAX_POSITIONS:
                time.sleep(60); continue

            stats = {'total': 0, 'low_vol': 0, 'sl_too_wide': 0, 'rsi_ignored': 0, 'passed': 0}
            markets = exchange.markets
            tickers = exchange.fetch_tickers()
            btc_trend, _ = get_market_context()
            
            for sym, market in markets.items():
                if market.get('type') != 'swap' or not market.get('active'): continue
                if any(kw in sym.upper() for kw in EXCLUDED_KEYWORDS): continue
                if any(pos['symbol'].split(':')[0] == sym.split(':')[0] for pos in active_positions): continue
                
                stats['total'] += 1
                ticker = tickers.get(sym)
                if not ticker: continue
                
                if float(ticker.get('quoteVolume') or 0) < MIN_VOLUME_USDT:
                    stats['low_vol'] += 1; continue
                    
                try:
                    time.sleep(0.3) 
                    
                    ohlcv = exchange.fetch_ohlcv(sym, timeframe=SMC_TIMEFRAME, limit=250)
                    if not ohlcv or len(ohlcv) < 200: continue
                    
                    o = np.array([x[1] for x in ohlcv], dtype=float)
                    h = np.array([x[2] for x in ohlcv], dtype=float)
                    l = np.array([x[3] for x in ohlcv], dtype=float)
                    c = np.array([x[4] for x in ohlcv], dtype=float)
                    v = np.array([x[5] for x in ohlcv], dtype=float)
                    
                    current_price = c[-1]
                    rsi = calculate_rsi(c[:-1], 14)
                    atr = np.mean(np.maximum(h[1:]-l[1:], np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])))[-14:])
                    
                    # Легкий фильтр тренда (SMA 50)
                    sma_50 = np.mean(c[-50:])
                    
                    avg_vol = np.mean(v[-22:-2]) if len(v) >= 22 else np.mean(v[:-2])
                    is_high_volume = v[-2] > (avg_vol * 1.30)
                    
                    o_3, c_3 = o[-3], c[-3]
                    o_2, c_2, h_2, l_2 = o[-2], c[-2], h[-2], l[-2]
                    
                    is_green_3 = c_3 > o_3
                    is_red_3 = c_3 < o_3
                    is_green_2 = c_2 > o_2
                    is_red_2 = c_2 < o_2
                    
                    body_2 = abs(c_2 - o_2)
                    lower_wick_2 = min(c_2, o_2) - l_2
                    upper_wick_2 = h_2 - max(c_2, o_2)
                    
                    is_bullish_engulfing = is_red_3 and is_green_2 and (c_2 > o_3)
                    is_bearish_engulfing = is_green_3 and is_red_2 and (c_2 < o_3)
                    
                    is_bullish_pinbar = is_green_2 and (lower_wick_2 > body_2 * 1.5) and (upper_wick_2 < body_2)
                    is_bearish_pinbar = is_red_2 and (upper_wick_2 > body_2 * 1.5) and (lower_wick_2 < body_2)
                    
                    has_bullish_pattern = is_bullish_engulfing or is_bullish_pinbar
                    has_bearish_pattern = is_bearish_engulfing or is_bearish_pinbar
                    
                    direction = None
                    sl_price = 0
                    
                    # Логика: Тренд BTC + Перепроданность + Паттерн + Объем + Цена ВЫШЕ SMA 50
                    if btc_trend == 'Long' and rsi < 30 and has_bullish_pattern and is_high_volume and current_price > sma_50:
                        direction = 'Long'
                        sl_price = np.min(l[-4:-1]) - (atr * 0.5)
                        
                    # Логика: Тренд BTC + Перекупленность + Паттерн + Объем + Цена НИЖЕ SMA 50
                    elif btc_trend == 'Short' and rsi > 70 and has_bearish_pattern and is_high_volume and current_price < sma_50:
                        direction = 'Short'
                        sl_price = np.max(h[-4:-1]) + (atr * 0.5)
                        
                    if not direction:
                        stats['rsi_ignored'] += 1; continue
                    
                    actual_sl_dist = abs(current_price - sl_price)
                    
                    min_sl_dist = current_price * (MIN_SL_PCT / 100.0)
                    if actual_sl_dist < min_sl_dist:
                        sl_price = current_price - min_sl_dist if direction == 'Long' else current_price + min_sl_dist
                        actual_sl_dist = min_sl_dist
                        
                    if (actual_sl_dist / current_price) * 100 > MAX_SL_PCT:
                        stats['sl_too_wide'] += 1; continue
                        
                    tp1_price = current_price + (actual_sl_dist * MIN_RR_RATIO) if direction == 'Long' else current_price - (actual_sl_dist * MIN_RR_RATIO)
                    
                    stats['passed'] += 1
                    execute_trade(sym, direction, current_price, sl_price, tp1_price, atr)
                    time.sleep(1)
                except Exception as e: 
                    logging.debug(f"Ошибка проверки монеты {sym}: {e}")
                    pass

            logging.info(f"🔎 [РАДАР] Всего: {stats['total']} -> Неликвид: {stats['low_vol']} -> Широкий SL: {stats['sl_too_wide']} -> Ждем Сетап: {stats['rsi_ignored']} -> ВХОДЫ: {stats['passed']}")
            gc.collect(); time.sleep(150)
        except Exception as e: logging.error(f"Scan Error: {e}"); time.sleep(60)

def log_bot_status():
    winrate = (daily_stats['wins'] / daily_stats['trades'] * 100) if daily_stats['trades'] > 0 else 0
    active = ", ".join([f"{p['symbol'].split(':')[0]} ({p['direction']})" for p in active_positions]) or "Нет"
    logging.info(f"📊 [СТАТИСТИКА] Сделок: {daily_stats['trades']} | Винрейт: {winrate:.1f}% | PNL: {daily_stats.get('pnl', 0.0):.2f}$ | Открыто: {active}")

@bot.message_handler(commands=['stats'])
def get_stats(message):
    winrate = (daily_stats['wins'] / daily_stats['trades'] * 100) if daily_stats['trades'] > 0 else 0
    active = "\n".join([f"🔸 {p['symbol'].split(':')[0]} ({p['direction']})" for p in active_positions]) or "Нет открытых позиций."
    rep = f"📊 **СТАТИСТИКА RSI БОТА:**\nСделок: {daily_stats['trades']}\nВинрейт: {winrate:.1f}%\nPNL: {daily_stats.get('pnl', 0.0):.2f} $\n\n🔥 **Активные сделки:**\n{active}"
    bot.send_message(message.chat.id, rep)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"RSI Bot v8.12 Active")
    def log_message(self, format, *args): return

def run_server():
    server = HTTPServer(('0.0.0.0', int(os.environ.get('PORT', 10000))), HealthCheckHandler)
    server.serve_forever()

def send_daily_exchange_report():
    global START_OF_DAY_BALANCE, daily_stats
    
    current_balance = get_real_balance()
    if START_OF_DAY_BALANCE <= 0:
        START_OF_DAY_BALANCE = current_balance
        
    real_pnl_usdt = current_balance - START_OF_DAY_BALANCE
    pnl_percent = (real_pnl_usdt / START_OF_DAY_BALANCE * 100) if START_OF_DAY_BALANCE > 0 else 0.0
    
    trades = daily_stats['trades']
    wins = daily_stats['wins']
    sl_hits = daily_stats.get('sl_hits', 0)
    timeouts = daily_stats.get('timeouts', 0)
    
    winrate = (wins / trades * 100) if trades > 0 else 0.0
    
    report = (
        f"🗓 **ИТОГИ ДНЯ (RSI Bot):** {datetime.now(timezone.utc).strftime('%d.%m.%Y')}\n\n"
        f"📈 **Торговая статистика бота:**\n"
        f"Всего закрыто сделок: {trades}\n"
        f"🔸 В плюс (Трейлинг/TP): {wins}\n"
        f"🔸 По таймеру: {timeouts}\n"
        f"🔸 По Stop-Loss: {sl_hits}\n\n"
        f"🎯 **Винрейт:** {winrate:.1f}%\n"
        f"💰 **Чистая прибыль:** {real_pnl_usdt:+.2f} USDT ({pnl_percent:+.2f}%)\n"
        f"🏦 **Баланс аккаунта:** {current_balance:.2f} USDT"
    )
    
    try: bot.send_message(GROUP_CHAT_ID, report)
    except Exception as e: logging.error(f"Ошибка отправки отчета: {e}")
    
    START_OF_DAY_BALANCE = current_balance
    daily_stats = {'trades': 0, 'wins': 0, 'sl_hits': 0, 'timeouts': 0, 'pnl': 0.0, 'prev_winrate': 0.0}

if __name__ == '__main__':
    init_db()
    load_positions()
    START_OF_DAY_BALANCE = get_real_balance()
    logging.info("🚀 Запуск RSI БОТА v8.12 (Exchange API Reporting & VSA)...")
    
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_market_scan, 'interval', seconds=60)
    scheduler.add_job(monitor_positions_job, 'interval', seconds=15)
    scheduler.add_job(send_daily_exchange_report, 'cron', hour=20, minute=0)
    scheduler.start()
    
    Thread(target=run_server, daemon=True).start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: pass
