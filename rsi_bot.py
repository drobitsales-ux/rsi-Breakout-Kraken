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

DB_PATH = 'bot_prop.db'  
TOKEN = os.getenv('TELEGRAM_TOKEN')
GROUP_CHAT_ID = -1003955653290  
KRAKEN_API_KEY = os.getenv('KRAKEN_API_KEY')
KRAKEN_SECRET = os.getenv('KRAKEN_SECRET')

RISK_PER_TRADE = 0.005
MAX_POSITIONS = 3           
LEVERAGE = 5                
MAX_SPREAD_PERCENT = 1.0    
MIN_VOLUME_USDT = 0         
COOLDOWN_CACHE = {}

TRADE_TIMEOUT_PROFIT_HOURS = 1.5  
TRADE_TIMEOUT_ANY_HOURS = 3.0     

SMC_TIMEFRAME = '15m'       
MIN_RR_RATIO = 1.5          
MIN_SL_PCT = 1.2            
MAX_SL_PCT = 4.0   

EXCLUDED_KEYWORDS = [
    'FART', 'PEPE', 'SHIB', 'DOGE', 'WIF', 'BONK', 'FLOKI', 'BOME',
    'MEME', 'TURBO', 'SATS', 'RATS', 'ORDI', 'PEOPLE'
]

GLOBAL_STOP_UNTIL = None
CONSECUTIVE_LOSSES = 0

daily_stats = {'trades': 0, 'wins': 0, 'sl_hits': 0, 'timeouts': 0, 'pnl': 0.0, 'prev_winrate': 0.0, 'start_balance': 0.0}
active_positions = []

bot = telebot.TeleBot(TOKEN)
exchange = ccxt.krakenfutures({
    'apiKey': KRAKEN_API_KEY, 
    'secret': KRAKEN_SECRET,
    'enableRateLimit': True
})
exchange.set_sandbox_mode(True)

def get_db_conn(): return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    conn = get_db_conn(); c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS active_positions (id INTEGER PRIMARY KEY, data TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_stats (id INTEGER PRIMARY KEY, pnl REAL, trades INTEGER, wins INTEGER)''')
    try: c.execute("ALTER TABLE daily_stats ADD COLUMN prev_winrate REAL DEFAULT 0.0")
    except: pass
    try: c.execute("ALTER TABLE daily_stats ADD COLUMN start_balance REAL DEFAULT 0.0")
    except: pass
    conn.commit(); conn.close()

def save_positions():
    try:
        conn = get_db_conn(); c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO active_positions (id, data) VALUES (1, ?)", (json.dumps(active_positions),))
        c.execute("INSERT OR REPLACE INTO daily_stats (id, pnl, trades, wins, prev_winrate, start_balance) VALUES (1, ?, ?, ?, ?, ?)", 
                  (daily_stats.get('pnl', 0.0), daily_stats['trades'], daily_stats['wins'], 
                   daily_stats.get('prev_winrate', 0.0), daily_stats.get('start_balance', 0.0)))
        conn.commit(); conn.close()
    except Exception as e: logging.error(f"Save error: {e}")

def load_positions():
    global active_positions, daily_stats
    try:
        conn = get_db_conn(); c = conn.cursor()
        c.execute("SELECT data FROM active_positions WHERE id = 1"); row = c.fetchone()
        if row: active_positions = json.loads(row[0])
        c.execute("SELECT pnl, trades, wins, prev_winrate, start_balance FROM daily_stats WHERE id = 1"); stat_row = c.fetchone()
        if stat_row: 
            daily_stats['pnl'], daily_stats['trades'], daily_stats['wins'], daily_stats['prev_winrate'], daily_stats['start_balance'] = stat_row
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
        ohlcv = exchange.fetch_ohlcv('BTC/USD:USD', timeframe='15m', limit=50)
        c = np.array([x[4] for x in ohlcv], dtype=float)
        trend = 'Long' if c[-1] > np.mean(c[-21:-1]) else 'Short' 
        return trend, True
    except: 
        return 'Neutral', False

def execute_trade(sym, signal_data):
    global active_positions, COOLDOWN_CACHE
    if len(active_positions) >= MAX_POSITIONS or any(p['symbol'] == sym for p in active_positions): return
    if sym in COOLDOWN_CACHE and time.time() < COOLDOWN_CACHE[sym]: return
        
    direction, current_price = signal_data['mode'], signal_data['price']
    sl_price, tp1_price, atr = signal_data['sl_price'], signal_data['tp1_price'], signal_data['atr']
    
    try:
        bal = exchange.fetch_balance()
        free_usd = float(bal.get('USDT', {}).get('free', 0)) or float(bal.get('USD', {}).get('free', 0))
        if free_usd <= 0: return

        actual_sl_dist = abs(current_price - sl_price)
        sl_pct = (actual_sl_dist / current_price) * 100
        
        # ЗАЩИТА ОТ ДИКИХ СТОПОВ И ПРОСКАЛЬЗЫВАНИЙ
        if sl_pct > MAX_SL_PCT:
            logging.info(f"Отмена входа {sym}: Слишком широкий SL ({sl_pct:.1f}%)")
            COOLDOWN_CACHE[sym] = time.time() + 3600
            return
            
        risk_amount = free_usd * RISK_PER_TRADE
        
        market = exchange.markets.get(sym, {})
        contract_size = float(market.get('contractSize', 1.0))
        qty_coins = risk_amount / actual_sl_dist if actual_sl_dist > 0 else 0
        qty = float(exchange.amount_to_precision(sym, qty_coins / contract_size if contract_size > 0 else qty_coins))
        if qty <= 0: return
        
        pos_side = 'LONG' if direction == 'Long' else 'SHORT'
        try: exchange.set_leverage(LEVERAGE, sym)
        except: pass 
        
        exchange.create_market_order(sym, 'buy' if direction == 'Long' else 'sell', qty)
        sl_ord = exchange.create_order(sym, 'stop_market', 'sell' if direction == 'Long' else 'buy', qty, params={'stopLossPrice': sl_price})
        
        active_positions.append({
            'symbol': sym, 'direction': direction, 'entry_price': current_price, 'initial_qty': qty, 
            'sl_price': sl_price, 'tp1': tp1_price, 'tp1_hit': False, 'sl_order_id': sl_ord['id'], 
            'position_side': pos_side, 'open_time': datetime.now(timezone.utc).isoformat(), 'atr': atr
        })
        save_positions()
        
        # ОТПРАВКА СООБЩЕНИЯ С АНАЛИТИКОЙ
        msg = (
            f"💥 <b>ВЫСТРЕЛ [RSI Bot]: {sym.split(':')[0]}</b>\n"
            f"Направление: <b>#{direction.upper()}</b>\n\n"
            f"Цена: {current_price}\n"
            f"Объем (контр.): {qty}\n"
            f"SL: {sl_price} ({sl_pct:.2f}%)\n"
            f"TP1: {tp1_price}\n\n"
            f"📊 <b>Аналитика сетапа:</b>\n"
            f"🔸 Уровень RSI: {signal_data['rsi']:.1f}\n"
            f"🔸 Отклон. от SMA50: {signal_data['sma_dist']:.2f}%\n"
            f"🔸 BTC Тренд: {signal_data['btc_trend']}\n"
            f"🔸 Объем 24ч: {signal_data['vol']/1000000:.1f}M$"
        )
        bot.send_message(GROUP_CHAT_ID, msg, parse_mode="HTML")
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
                daily_stats['pnl'] = daily_stats.get('pnl', 0.0) + pnl
                
                if pnl > 0: 
                    daily_stats['wins'] += 1
                    CONSECUTIVE_LOSSES = 0
                    bot.send_message(GROUP_CHAT_ID, f"✅ <b>{clean_name} закрыта в плюс!</b>\nPNL: {pnl:+.2f} USD", parse_mode="HTML")
                else: 
                    daily_stats['sl_hits'] = daily_stats.get('sl_hits', 0) + 1
                    CONSECUTIVE_LOSSES += 1
                    COOLDOWN_CACHE[sym] = time.time() + 14400
                    bot.send_message(GROUP_CHAT_ID, f"🛑 <b>{clean_name} выбита по SL.</b>\nPNL: {pnl:.2f} USD\n❄️ Монета заморожена.", parse_mode="HTML")
                continue

            if 'open_time' not in pos: pos['open_time'] = datetime.now(timezone.utc).isoformat()
            hours_passed = (datetime.now(timezone.utc) - datetime.fromisoformat(pos['open_time'])).total_seconds() / 3600
            pnl = (ticker - pos['entry_price']) * float(curr['contracts']) if is_long else (pos['entry_price'] - ticker) * float(curr['contracts'])

            if hours_passed >= TRADE_TIMEOUT_ANY_HOURS or (hours_passed >= TRADE_TIMEOUT_PROFIT_HOURS and pnl > 0):
                try:
                    exchange.create_market_order(sym, 'sell' if is_long else 'buy', float(curr['contracts']))
                    if pos.get('sl_order_id'): exchange.cancel_order(pos['sl_order_id'], sym)
                    daily_stats['trades'] += 1
                    daily_stats['timeouts'] = daily_stats.get('timeouts', 0) + 1 
                    daily_stats['pnl'] = daily_stats.get('pnl', 0.0) + pnl
                    
                    if pnl > 0: daily_stats['wins'] += 1; CONSECUTIVE_LOSSES = 0
                    else: CONSECUTIVE_LOSSES += 1; COOLDOWN_CACHE[sym] = time.time() + 14400
                        
                    bot.send_message(GROUP_CHAT_ID, f"{'✅' if pnl > 0 else '🛑'} <b>{clean_name} закрыта по ТАЙМАУТУ!</b>\nPNL: {pnl:+.2f} USD", parse_mode="HTML")
                    continue
                except: pass

            try:
                ohlcv = exchange.fetch_ohlcv(sym, timeframe='1m', limit=2)
                high_p = max([float(c[2]) for c in ohlcv]) if ohlcv else ticker
                low_p = min([float(c[3]) for c in ohlcv]) if ohlcv else ticker
            except: high_p = low_p = ticker
            
            if not pos.get('tp1_hit'):
                if (is_long and high_p >= pos['tp1']) or (not is_long and low_p <= pos['tp1']):
                    try:
                        close_qty = float(exchange.amount_to_precision(sym, float(curr['contracts']) * 0.50))
                        if close_qty > 0: 
                            exchange.create_market_order(sym, 'sell' if is_long else 'buy', close_qty)
                            pos['initial_qty'] = float(curr['contracts']) - close_qty 
                        if pos.get('sl_order_id'): exchange.cancel_order(pos['sl_order_id'], sym)
                        
                        be_p = float(exchange.price_to_precision(sym, pos['entry_price'] * (1.002 if is_long else 0.998)))
                        new_sl = exchange.create_order(sym, 'stop_market', 'sell' if is_long else 'buy', pos['initial_qty'], params={'stopLossPrice': be_p})
                        
                        atr_step = pos.get('atr', abs(pos['entry_price'] - pos['sl_price']) * 0.5)
                        pos.update({
                            'tp1_hit': True, 'sl_order_id': new_sl['id'], 
                            'micro_step': atr_step * 0.3, 'trail_distance': atr_step * 0.8, 
                            'trail_trigger': pos['tp1'] + (atr_step * 0.3 * (1 if is_long else -1))
                        })
                        bot.send_message(GROUP_CHAT_ID, f"💰 <b>{clean_name} TP1 взят!</b> (50% закрыто). 🛡 Трейлинг запущен.", parse_mode="HTML")
                    except: pass
            
            if pos.get('tp1_hit'):
                tt, ms, td = pos.get('trail_trigger'), pos.get('micro_step'), pos.get('trail_distance')
                if tt and ms and td:
                    if (is_long and high_p >= tt) or (not is_long and low_p <= tt):
                        try:
                            if pos.get('sl_order_id'): exchange.cancel_order(pos['sl_order_id'], sym)
                            nsl = float(exchange.price_to_precision(sym, tt - td if is_long else tt + td))
                            new_sl_order = exchange.create_order(sym, 'stop_market', 'sell' if is_long else 'buy', pos['initial_qty'], params={'stopLossPrice': nsl})
                            pos.update({'sl_order_id': new_sl_order['id'], 'trail_trigger': tt + ms if is_long else tt - ms})
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
                if market.get('active') is False: continue
                if not (sym.endswith(':USD') or sym.endswith(':USDT')): continue
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
                    sma_50 = np.mean(c[-50:])
                    avg_vol = np.mean(v[-22:-2]) if len(v) >= 22 else np.mean(v[:-2])
                    is_high_volume = v[-2] > (avg_vol * 1.30)
                    
                    o_3, c_3 = o[-3], c[-3]
                    o_2, c_2, h_2, l_2 = o[-2], c[-2], h[-2], l[-2]
                    
                    is_green_3 = c_3 > o_3; is_red_3 = c_3 < o_3
                    is_green_2 = c_2 > o_2; is_red_2 = c_2 < o_2
                    
                    body_2 = abs(c_2 - o_2)
                    lower_wick_2 = min(c_2, o_2) - l_2; upper_wick_2 = h_2 - max(c_2, o_2)
                    
                    is_bullish_engulfing = is_red_3 and is_green_2 and (c_2 > o_3)
                    is_bearish_engulfing = is_green_3 and is_red_2 and (c_2 < o_3)
                    is_bullish_pinbar = is_green_2 and (lower_wick_2 > body_2 * 1.5) and (upper_wick_2 < body_2)
                    is_bearish_pinbar = is_red_2 and (upper_wick_2 > body_2 * 1.5) and (lower_wick_2 < body_2)
                    
                    has_bullish_pattern = is_bullish_engulfing or is_bullish_pinbar
                    has_bearish_pattern = is_bearish_engulfing or is_bearish_pinbar
                    
                    direction = None; sl_price = 0
                    
                    if btc_trend == 'Long' and rsi < 30 and has_bullish_pattern and is_high_volume and current_price > sma_50:
                        direction = 'Long'
                        sl_price = np.min(l[-4:-1]) - (atr * 1.5)
                        
                    elif btc_trend == 'Short' and rsi > 70 and has_bearish_pattern and is_high_volume and current_price < sma_50:
                        direction = 'Short'
                        sl_price = np.max(h[-4:-1]) + (atr * 1.5)
                        
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
                    
                    sma_dist = abs(current_price - sma_50) / current_price * 100
                    signal_data = {
                        'mode': direction, 'price': current_price, 'sl_price': sl_price, 
                        'tp1_price': tp1_price, 'atr': atr, 'rsi': rsi, 
                        'sma_dist': sma_dist, 'btc_trend': btc_trend, 'vol': float(ticker.get('quoteVolume', 0))
                    }
                    
                    execute_trade(sym, signal_data)
                    time.sleep(1)

# === ОТПРАВКА ЕЖЕДНЕВНОГО ОТЧЕТА ===
def send_daily_report():
    global daily_stats
    trades = daily_stats.get('trades', 0)
    if trades == 0: return
    
    wins = daily_stats.get('wins', 0)
    sl_hits = daily_stats.get('sl_hits', 0)
    timeouts = daily_stats.get('timeouts', 0)
    pnl = daily_stats.get('pnl', 0.0)
    winrate = (wins / trades * 100) if trades > 0 else 0.0
    
    try:
        bal = exchange.fetch_balance()
        current_balance = float(bal.get('USDT', {}).get('total', 0)) or float(bal.get('USD', {}).get('total', 0))
    except: current_balance = 0.0
    
    start_bal = daily_stats.get('start_balance', 0.0)
    pct_change = ((current_balance - start_bal) / start_bal * 100) if start_bal > 0 else 0.0
    
    report = (
        f"🗓 <b>ИТОГИ ДНЯ (Kraken RSI Bot):</b> {datetime.now(timezone.utc).strftime('%d.%m.%Y')}\n\n"
        f"📉 Закрыто сделок: {trades}\n"
        f"✅ В плюс (TP/Таймаут): {wins}\n"
        f"🛑 По стопу: {sl_hits}\n"
        f"⏳ По таймауту (общ.): {timeouts}\n\n"
        f"🎯 Винрейт: {winrate:.1f}%\n"
        f"💵 PNL сделок: {pnl:+.2f} USD\n\n"
        f"🏦 <b>Баланс аккаунта:</b> {current_balance:.2f} USD\n"
        f"📊 <b>Изменение за день:</b> {pct_change:+.2f}%"
    )
    
    try: bot.send_message(GROUP_CHAT_ID, report, parse_mode="HTML")
    except Exception as e: logging.error(f"Daily Report Error: {e}")
    
    daily_stats = {'trades': 0, 'wins': 0, 'sl_hits': 0, 'timeouts': 0, 'pnl': 0.0, 'prev_winrate': winrate, 'start_balance': current_balance}
    save_positions()

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"Kraken Prop RSI Bot Active")
    def log_message(self, format, *args): return

def run_server():
    server = HTTPServer(('0.0.0.0', int(os.environ.get('PORT', 10000))), HealthCheckHandler)
    server.serve_forever()

if __name__ == '__main__':
    init_db()
    load_positions()
    
    # Фиксируем баланс при первом запуске
    if daily_stats.get('start_balance', 0.0) == 0.0:
        try:
            bal = exchange.fetch_balance()
            curr = float(bal.get('USDT', {}).get('total', 0)) or float(bal.get('USD', {}).get('total', 0))
            daily_stats['start_balance'] = curr
            save_positions()
        except: pass
        
    logging.info("🚀 Запуск KRAKEN RSI БОТА (Prop Firm: 0.5% Risk, SMA50, Meme-Filter)...")
    
    try:
        bot.send_message(GROUP_CHAT_ID, "🟢 <b>KRAKEN RSI БОТ</b> успешно запущен и готов к работе!", parse_mode="HTML")
    except Exception as e:
        logging.error(f"TG Error: {e}")
        
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_market_scan, 'interval', seconds=60)
    scheduler.add_job(monitor_positions_job, 'interval', seconds=15)
    scheduler.add_job(send_daily_report, 'cron', hour=20, minute=0, timezone='UTC')
    scheduler.start()
    
    Thread(target=run_server, daemon=True).start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: pass
