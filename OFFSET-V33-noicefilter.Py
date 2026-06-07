import os
import sys
import time
import json
import requests
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import win32gui
import joblib
import numpy as np
import pandas as pd
import MetaTrader5 as mt5
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor

# Win32 Window message definitions to toggle the global Algo Trading button
MT_WMCMD_EXPERTS = 32851
WM_COMMAND = 0x0111

CONFIG_NAME = "config.json"
RM_CONFIG_NAME = "RMConfig.json"
RM_CACHE_NAME = "news_cache.json"
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

# — 1. CONFIGURATION MANAGER —

class BotConfig:
    def __init__(self, filename=CONFIG_NAME):
        self.filename = filename
        self.defaults = {
            "Symbols": ["BTCUSDm", "XAUUSD"],
            "MinRr": 5.0,
            "MaxNoise": 0.25,
            "MaxTradePerSymbol": 1,
            "TradingDays": 365,
            "ExecutionTf": "M5",
            "Qty": 0.01,
            "Confidence": 0.80,
            "MinDirProb": 0.50,
            "EV_WAIT": 35,
            "Magic": 778899,
            "SmartBook": True,
            "SmartBookValue": 300.0,
            "SmartLossValue": 150.0,
            "OppositionPower": True,
            "Oldisgold": True,
            "MaxLossDay": 300.0,
            "GlobalLossResetTime": "02:30",  # CONFIGURABLE RESET TIME (UTC)
            "AutoRetrain": True,
            "RRCutter": True,
            "RRCutterValue": 1.0,
            "RM": True  # MASTER TOGGLE: True = Embedded Risk Manager runs, False = Bot acts normal
        }
        self.data = self.load_config()

    def load_config(self):
        if not os.path.exists(self.filename):
            with open(self.filename, 'w') as f:
                json.dump(self.defaults, f, indent=4)
            print(f" [+] Config file '{self.filename}' not found. Automatically exported default settings.")
            return self.defaults
        with open(self.filename, 'r') as f:
            return json.load(f)

# — 2. EMBEDDED RISK MANAGER ENGINE (CONCURRENT BACKGROUND TASK) —

class EmbeddedRiskManager(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True  
        self.config = {}
        self.raw_events = []      
        self.cached_events = []   
        self.last_news_refresh = datetime.min.replace(tzinfo=timezone.utc)
        
        self.load_rm_config()
        self.load_calendar_from_cache()
        self.fetch_economic_calendar()



    def load_rm_config(self):
            """Hot-reloads RM configuration dynamically including the new currency filter tag."""
            default_rm_config = {
                "Prenewsbreak": 5,
                "postnewsbreak": 5,
                "loss": 300,
                "newsimpact": "high, low, medume",
                "newscurrency": "all",  # 👈 NEW: Default to "all", or use specific lists like "USD, EUR, JPY"
                "Newsrefresh": 1,
                "closealltrades": True  
            }
            try:
                if not os.path.exists(RM_CONFIG_NAME):
                    with open(RM_CONFIG_NAME, 'w') as f:
                        json.dump(default_rm_config, f, indent=4)
                    print(f" [+] RM Engine config file '{RM_CONFIG_NAME}' exported automatically.")
                    self.config = default_rm_config
                else:
                    with open(RM_CONFIG_NAME, 'r') as f:
                        self.config = json.load(f)
            except Exception as e:
                print(f" [RM Engine] Error handling configuration file: {e}")
                self.config = default_rm_config

    def apply_news_filter(self):
        """⚡ DYNAMIC DOUBLE-FILTER: Filters raw events instantly based on BOTH Impact levels AND targeted Currencies."""
        # 1. Parse Impact Rules
        impact_str = self.config.get("newsimpact", "high, low, medume").lower()
        allowed_impacts = []
        if "high" in impact_str: allowed_impacts.append("high")
        if "low" in impact_str: allowed_impacts.append("low")
        if "med" in impact_str or "medume" in impact_str: allowed_impacts.append("medium")

        # 2. Parse Currency Rules
        currency_config = self.config.get("newscurrency", "all").lower()
        allow_all_currencies = "all" in currency_config
        
        # Split clean tokens into an array: e.g., ["usd", "eur", "jpy"]
        allowed_currencies = [c.strip() for c in currency_config.split(",") if c.strip()]

        filtered_list = []
        for ev in self.raw_events:
            # Match impact tier
            impact_match = ev["impact"].lower() in allowed_impacts
            
            # Match currency code (e.g., event currency "USD" matches configuration list)
            ev_currency_clean = ev["currency"].lower()
            currency_match = allow_all_currencies or (ev_currency_clean in allowed_currencies)
            
            if impact_match and currency_match:
                filtered_list.append(ev)
                
        self.cached_events = filtered_list
        
        
        
        
        
        
    def save_calendar_to_cache(self):
        try:
            serializable_events = []
            for ev in self.raw_events:
                serializable_events.append({
                    "title": ev["title"], "currency": ev["currency"], "impact": ev["impact"], "utc_time": ev["utc_time"].isoformat()
                })
            with open(RM_CACHE_NAME, 'w') as f:
                json.dump(serializable_events, f, indent=4)
        except Exception as e:
            print(f" [RM Engine] Failed to write local news backup cache file: {e}")

    def load_calendar_from_cache(self):
        if not os.path.exists(RM_CACHE_NAME):
            return False
        try:
            with open(RM_CACHE_NAME, 'r') as f:
                stored_events = json.load(f)
            loaded_events = []
            for ev in stored_events:
                loaded_events.append({
                    "title": ev["title"], "currency": ev["currency"], "impact": ev["impact"], "utc_time": datetime.fromisoformat(ev["utc_time"])
                })
            self.raw_events = loaded_events
            self.apply_news_filter()
            print(f" [RM Engine] Local Backup Cache loaded. Applied active filter rules.")
            self.print_upcoming_schedule()
            return True
        except Exception as e:
            print(f" [RM Engine] Error reading local news backup cache file: {e}")
            return False

    def fetch_economic_calendar(self):
        now_utc = datetime.now(timezone.utc)
        refresh_interval = timedelta(hours=self.config.get("Newsrefresh", 1))
        if now_utc - self.last_news_refresh < refresh_interval:
            return
        print(f" >>> [RM Engine] Updating Economic Calendar from Fair Economy CDN...")
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        try:
            response = requests.get(NEWS_URL, headers=headers, timeout=15)
            if response.status_code != 200:
                self.load_calendar_from_cache()
                self.last_news_refresh = now_utc - refresh_interval + timedelta(minutes=15)
                return
            root = ET.fromstring(response.content)
            new_raw_events = []
            for item in root.findall('event'):
                impact = item.find('impact').text.strip().lower()
                date_str = item.find('date').text.strip()
                time_str = item.find('time').text.strip()
                currency = item.find('country').text.strip()
                title = item.find('title').text.strip()
                if "am" not in time_str.lower() and "pm" not in time_str.lower(): continue
                try:
                    event_utc = datetime.strptime(f"{date_str} {time_str}", "%m-%d-%Y %I:%M%p").replace(tzinfo=timezone.utc)
                    new_raw_events.append({"title": title, "currency": currency, "impact": impact.upper(), "utc_time": event_utc})
                except Exception: continue
            self.raw_events = new_raw_events
            self.last_news_refresh = now_utc
            self.save_calendar_to_cache()
            self.apply_news_filter()
            self.print_upcoming_schedule()
        except Exception as e:
            self.load_calendar_from_cache()
            self.last_news_refresh = now_utc - refresh_interval + timedelta(minutes=15)

    def print_upcoming_schedule(self):
        now_utc = datetime.now(timezone.utc)
        tomorrow_utc = now_utc + timedelta(days=1)
        print("\n================== UPCOMING MACRO EVENTS SCHEDULE ==================")
        found = False
        for ev in self.cached_events:
            ev_utc = ev["utc_time"]
            if ev_utc.date() == now_utc.date() or ev_utc.date() == tomorrow_utc.date():
                if ev_utc >= now_utc - timedelta(minutes=60):
                    print(f" [{ev['impact']}] {ev_utc.astimezone().strftime('%Y-%m-%d %H:%M')} | {ev['currency']} - {ev['title']}")
                    found = True
        if not found: print(" No matching impact events detected for today or tomorrow.")
        print("====================================================================\n")

    def get_daily_candle_pnl(self):
        """Perfectly mirrors the MT5 terminal's native 'Today' filter using auto-detected broker clocks."""
        now_ts = int(time.time())
        from_ts = now_ts - (86400 * 3)
        deals = mt5.history_deals_get(from_ts, now_ts + 86400)
        
        broker_now_ts = None
        max_tick_time = 0
        discovery_pool = ["BTCUSDm", "BTCUSD", "XAUUSD", "EURUSD", "ETHUSD"]
        for sym in discovery_pool:
            tick = mt5.symbol_info_tick(sym)
            if tick is not None and tick.time > max_tick_time:
                max_tick_time = tick.time
        if max_tick_time > 0: broker_now_ts = max_tick_time
        if broker_now_ts is None and deals: broker_now_ts = max(deal.time for deal in deals)
        if broker_now_ts is None: return 0.0
        
        broker_today_str = datetime.fromtimestamp(broker_now_ts).strftime('%Y-%m-%d')
        closed_pnl = 0.0
        if deals:
            for deal in deals:
                if deal.type in [mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL]:
                    if datetime.fromtimestamp(deal.time).strftime('%Y-%m-%d') == broker_today_str:
                        closed_pnl += (deal.profit + deal.swap + deal.commission)
        floating_pnl = 0.0
        positions = mt5.positions_get()
        if positions:
            for pos in positions: floating_pnl += (pos.profit + pos.swap)
        return closed_pnl + floating_pnl

    def is_inside_news_window(self):
        now_utc = datetime.now(timezone.utc)
        pre_break = self.config.get("Prenewsbreak", 5)
        post_break = self.config.get("postnewsbreak", 5)
        for ev in self.cached_events:
            ev_utc = ev["utc_time"]
            if (ev_utc - timedelta(minutes=pre_break)) <= now_utc <= (ev_utc + timedelta(minutes=post_break)):
                return True, ev
        return False, None

    def flatten_all_positions(self):
        """⚡ EMERGENCY PURGE: Close out all current risk immediately across all pairs."""
        positions = mt5.positions_get()
        if not positions:
            return
        print(f"\n [RM Engine ⚠️] FLATTENING TRIGGERED: Purging {len(positions)} active exposure tracks...")
        for p in positions:
            symbol = p.symbol
            tick = mt5.symbol_info_tick(symbol)
            if tick is None: continue
            order_type = mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
            price = tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask
            request = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": p.volume, "type": order_type,
                "position": p.ticket, "price": price, "deviation": 30, "magic": p.magic,
                "comment": "RM-NewsPurge", "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"   [RM Engine ✅] Cleanly liquidated position {p.ticket} [{symbol}]")
            else:
                print(f"   [RM Engine ❌] Liquidation failed for ticket {p.ticket}: {result.comment}")

    def set_algo_trading_state(self, target_state: bool):
        term_info = mt5.terminal_info()
        if not term_info or term_info.trade_allowed == target_state: return 
        def enum_windows_callback(hwnd, extra):
            try:
                title = win32gui.GetWindowText(hwnd).lower()
                class_name = win32gui.GetClassName(hwnd).lower()
                if "metatarader5cl" in class_name or "metatrader 5" in title or "mt5" in title: extra.append(hwnd)
            except Exception: pass
            return True
        hwnds = []
        win32gui.EnumWindows(enum_windows_callback, hwnds)
        if hwnds:
            win32gui.PostMessage(hwnds[0], WM_COMMAND, MT_WMCMD_EXPERTS, 0)
            time.sleep(1.0)

    def run(self):
            print(" >>> [RM Engine] Guard Thread deployed successfully.")
            while True:
                try:
                    self.load_rm_config()       
                    self.apply_news_filter()    
                    self.fetch_economic_calendar()
                    
                    daily_pnl = self.get_daily_candle_pnl()
                    max_loss_limit = -abs(self.config.get("loss", 300))
                    in_news_window, breaking_event = self.is_inside_news_window()
                    
                    # --- SCENARIO 1: MAX LOSS BREACH ---
                    if daily_pnl <= max_loss_limit:
                        print(f"\n [RM Engine !!!] SHUTDOWN: Daily Loss Limit Breached. PnL: ${daily_pnl:.2f} <= ${max_loss_limit:.2f}")
                        self.flatten_all_positions()        # 1. Purge risk FIRST while Algo Trading is still allowed
                        self.set_algo_trading_state(False)   # 2. Shut down the system entry gateway second
                    
                    # --- SCENARIO 2: MACRO NEWS WINDOW ---
                    elif in_news_window:
                        print(f"\n [RM Engine !!!] NEWS BLOCK ACTIVE: [{breaking_event['impact']}] {breaking_event['title']}")
                        
                        # 1. Execute market liquidation FIRST if configured to true
                        if self.config.get("closealltrades", True):
                            self.flatten_all_positions()
                        else:
                            print("   [RM Engine] 'closealltrades' is false. Holding current trades through macro event.")
                            
                        # 2. Turn off the Algo switch SECOND to block any new entries from firing
                        self.set_algo_trading_state(False)
                    
                    # --- SCENARIO 3: NORMAL OPERATING CONDITIONS ---
                    else:
                        self.set_algo_trading_state(True)
                    
                    sys.stdout.write(f"\r [RM Engine Status] Account PnL: ${daily_pnl:+.2f} | Filter Status: {'BLOCKED' if in_news_window else 'CLEAR'} ")
                    sys.stdout.flush()
                    time.sleep(5)
                except Exception as e:
                    print(f"\n [RM Engine Error] Monitoring exception error thread intercept: {e}")
                    time.sleep(5)
                    




# — 3. THE 17-TF SYMMETRIC ML ENGINE —

class BarotSniperML:
    def __init__(self, symbol, config):
        self.symbol = symbol
        self.config = config
        self.model_path = f"models/{symbol}/"
        os.makedirs(self.model_path, exist_ok=True)

        self.timeframes = {
            "M5": mt5.TIMEFRAME_M5, "M6": mt5.TIMEFRAME_M6, "M10": mt5.TIMEFRAME_M10, 
            "M12": mt5.TIMEFRAME_M12, "M15": mt5.TIMEFRAME_M15, "M20": mt5.TIMEFRAME_M20, 
            "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H2": mt5.TIMEFRAME_H2, 
            "H3": mt5.TIMEFRAME_H3, "H4": mt5.TIMEFRAME_H4, "H6": mt5.TIMEFRAME_H6, 
            "H8": mt5.TIMEFRAME_H8, "H12": mt5.TIMEFRAME_H12, "D1": mt5.TIMEFRAME_D1, 
            "W1": mt5.TIMEFRAME_W1, "MN1": mt5.TIMEFRAME_MN1
        }
        self.execution_tf = self.timeframes["M5"]
        self.model_names = ["direction", "tp_reg", "sl_reg", "confidence"]
        self.models = {name: None for name in self.model_names}

    def get_mtf_ema_features(self, symbol, count):
        m5_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, count)
        if m5_rates is None: return None
        main_df = pd.DataFrame(m5_rates)
        main_df['time'] = pd.to_datetime(main_df['time'], unit='s')
        
        high_low = main_df['high'] - main_df['low']
        high_close = (main_df['high'] - main_df['close'].shift()).abs()
        low_close = (main_df['low'] - main_df['close'].shift()).abs()
        main_df['atr_m5'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(14).mean()

        for tf_name, tf_id in self.timeframes.items():
            fetch = count if "M" in tf_name else (count // 4) + 100
            htf_rates = mt5.copy_rates_from_pos(symbol, tf_id, 0, fetch)
            if htf_rates is None: continue
            
            htf_df = pd.DataFrame(htf_rates)
            htf_df['time'] = pd.to_datetime(htf_df['time'], unit='s')
            
            htf_df[f'ema5_{tf_name}'] = htf_df['close'].ewm(span=5, adjust=False).mean().shift(1)
            htf_df[f'gap_p_{tf_name}'] = (htf_df['close'].shift(1) - htf_df[f'ema5_{tf_name}'])
            
            pivot_tfs = ['M10', 'M15', 'M30', 'H1', 'H2', 'H3', 'H4', 'H6', 'H8', 'H12']
            if tf_name in pivot_tfs:
                htf_df[f'{tf_name}_low_pivot'] = htf_df['low'].rolling(2).min().shift(1)
                htf_df[f'{tf_name}_high_pivot'] = htf_df['high'].rolling(2).max().shift(1)
            
            cols = ['time', f'ema5_{tf_name}', f'gap_p_{tf_name}']
            if tf_name in pivot_tfs:
                cols += [f'{tf_name}_low_pivot', f'{tf_name}_high_pivot']
            
            main_df = pd.merge_asof(main_df.sort_values('time'), htf_df[cols].sort_values('time'), on='time', direction='backward')

        return main_df.dropna()

    def train_stack(self):
        print(f"[{self.symbol}] Training: 17-TF Symmetric Structural Brain...")
        bars = (self.config['TradingDays'] * 288) + 1000 
        df = self.get_mtf_ema_features(self.symbol, bars)
        
        if df is None or len(df) < 1500: return

        df['target_h_mag'] = df[[c for c in df.columns if 'high_pivot' in c]].max(axis=1)
        df['target_l_mag'] = df[[c for c in df.columns if 'low_pivot' in c]].min(axis=1)
        
        df['f_high'] = df['high'].rolling(300).max().shift(-300)
        df['f_low'] = df['low'].rolling(300).min().shift(-300)
        df['s_high'] = df['high'].rolling(10).max().shift(1)
        df['s_low'] = df['low'].rolling(10).min().shift(1)

        df = df.dropna().copy()
        df['y_class'] = 0
        
        buy_cond = (df['f_high'] >= df['target_h_mag']) & ((df['close'] - df['s_low']) <= 1.5 * df['atr_m5']) & ((df['close'] - df['s_low']) > 0)
        sell_cond = (df['f_low'] <= df['target_l_mag']) & ((df['s_high'] - df['close']) <= 1.5 * df['atr_m5']) & ((df['s_high'] - df['close']) > 0)
        
        df.loc[buy_cond, 'y_class'] = 1
        df.loc[sell_cond, 'y_class'] = 2

        feature_cols = [c for c in df.columns if 'ema5' in c or 'gap_' in c or 'pivot' in c]
        X, y = df[feature_cols].values, df['y_class'].values

        self.models['direction'] = RandomForestClassifier(n_estimators=150, max_features='log2', class_weight='balanced', min_samples_leaf=10).fit(X, y)

        v_idx = df['y_class'] != 0
        X_v = X[v_idx]
        tp_val = np.where(y[v_idx] == 1, (df.loc[v_idx, 'target_h_mag'] - df.loc[v_idx, 'close']), (df.loc[v_idx, 'close'] - df.loc[v_idx, 'target_l_mag']))
        sl_val = np.where(y[v_idx] == 1, (df.loc[v_idx, 'close'] - df.loc[v_idx, 's_low']), (df.loc[v_idx, 's_high'] - df.loc[v_idx, 'close']))

        self.models['tp_reg'] = RandomForestRegressor(n_estimators=100).fit(X_v, tp_val / df.loc[v_idx, 'close'])
        self.models['sl_reg'] = RandomForestRegressor(n_estimators=100).fit(X_v, sl_val / df.loc[v_idx, 'close'])

        preds = self.models['direction'].predict(X)
        y_conf = (preds == y) & (y != 0)
        meta_X = np.column_stack([preds, self.models['tp_reg'].predict(X), self.models['sl_reg'].predict(X)])
        self.models['confidence'] = RandomForestClassifier(n_estimators=100, class_weight='balanced').fit(meta_X, y_conf)

        for name in self.model_names: joblib.dump(self.models[name], f"{self.model_path}{name}.joblib")
        print(f"[{self.symbol}] 17-TF Training Complete.")

    def load_models(self):
        try:
            for name in self.model_names: self.models[name] = joblib.load(f"{self.model_path}{name}.joblib")
            return True
        except: return False

# — 4. LIVE EXECUTION ENGINE —

class SniperBot:
    def __init__(self):
        self.config = BotConfig().data
        if not mt5.initialize(): quit()
        
        # Deploy Risk Manager Thread if switch is active
        if self.config.get("RM", True):
            print("\n>>> [System Bootstrap] Master Risk Manager switch active. Initializing Core Engine Thread...")
            self.rm_engine = EmbeddedRiskManager()
            self.rm_engine.start()
        else:
            print("\n>>> [System Bootstrap] Master Risk Manager switch disabled. Running normal execution paths.")
            self.rm_engine = None

        self.agents = {}
        self.last_trained_candle_start = {}  
        
        for s in self.config['Symbols']:
            agent = BarotSniperML(s, self.config)
            
            if not agent.load_models(): 
                agent.train_stack()
                rates = mt5.copy_rates_from_pos(s, mt5.TIMEFRAME_D1, 0, 1)
                if rates is not None and len(rates) > 0:
                    with open(f"{agent.model_path}last_train.txt", "w") as f:
                        f.write(str(rates[0]['time']))

            history_file = f"{agent.model_path}last_train.txt"
            if os.path.exists(history_file):
                with open(history_file, "r") as f:
                    try:
                        self.last_trained_candle_start[s] = int(f.read().strip())
                    except:
                        self.last_trained_candle_start[s] = None
            else:
                self.last_trained_candle_start[s] = None
                
            self.agents[s] = agent
        self.last_processed_time = {s: 0 for s in self.config['Symbols']}

    def check_daily_loss_limit(self):
        now = datetime.now(timezone.utc)
        reset_str = self.config.get("GlobalLossResetTime", "02:30")
        try:
            hour, minute = map(int, reset_str.split(":"))
        except:
            hour, minute = 2, 30
            
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < target: target -= timedelta(days=1)
        deals = mt5.history_deals_get(date_from=target, date_to=now)
        if not deals: return False
        
        total_pnl = sum(d.profit + d.swap for d in deals if d.magic == self.config['Magic'])
        if total_pnl <= -self.config['MaxLossDay']:
            print(f"!!! GLOBAL LOSS LIMIT HIT: {total_pnl:.2f}$. Suspending all trades.")
            return True
        return False

    def check_daily_retrain(self, symbol, agent):
        if not self.config['AutoRetrain']: 
            print(f"[{symbol}] Auto-Retrain: Disabled")
            return
            
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 1)
        if rates is None or len(rates) < 1: return
        
        candle_start = rates[0]['time']
        tick = mt5.symbol_info_tick(symbol)
        if tick is None: return
        current_server_time = tick.time
        
        expected_close = candle_start + 86400
        remaining = expected_close - current_server_time
        hours, rem = divmod(max(0, int(remaining)), 3600)
        minutes, _ = divmod(rem, 60)
        
        print(f"[{symbol}] Retrain Countdown: {hours}h {minutes}m")
        
        if self.last_trained_candle_start[symbol] is None:
            self.last_trained_candle_start[symbol] = candle_start
            with open(f"{agent.model_path}last_train.txt", "w") as f:
                f.write(str(candle_start))
            return

        if self.last_trained_candle_start[symbol] != candle_start:
            print(f"[{symbol}] New Daily Candle Session Detected. Starting Auto-Retrain...")
            for m in agent.model_names:
                model_file = f"{agent.model_path}{m}.joblib"
                if os.path.exists(model_file): os.remove(model_file)
            
            agent.train_stack()
            with open(f"{agent.model_path}last_train.txt", "w") as f:
                f.write(str(candle_start))
                
            self.last_trained_candle_start[symbol] = candle_start

    def check_smart_book(self):
        if not self.config['SmartBook']: return
        for symbol in self.config['Symbols']:
            positions = mt5.positions_get(symbol=symbol, magic=self.config['Magic'])
            if positions:
                tp_val = sum(p.profit + p.swap for p in positions)
                if tp_val >= self.config['SmartBookValue'] or tp_val <= -abs(self.config['SmartLossValue']):
                    print(f" >>> [{symbol}] SmartBook Triggered: {tp_val:.2f}")
                    self.close_all_symbol_trades(symbol)

    def close_all_symbol_trades(self, symbol):
        positions = mt5.positions_get(symbol=symbol, magic=self.config['Magic'])
        for p in positions:
            self.close_specific_position(p, symbol)

    def close_specific_position(self, p, symbol):
        t = mt5.symbol_info_tick(symbol)
        if t is None: return
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": p.volume,
            "type": mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
            "position": p.ticket, "price": t.bid if p.type == mt5.POSITION_TYPE_BUY else t.ask,
            "deviation": 20, "magic": self.config['Magic'], "comment": "BarotSymmetryV4",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        mt5.order_send(req)

    def apply_opposition_power(self, symbol, p_buy, p_sell):
        if not self.config.get('OppositionPower', True): return
        positions = mt5.positions_get(symbol=symbol, magic=self.config['Magic'])
        if not positions: return

        current_type = positions[0].type
        should_trim = False

        if current_type == mt5.POSITION_TYPE_SELL and p_buy > p_sell:
            should_trim = True
            print(f" >>> [{symbol}] OPPOSITION POWER: Buy {p_buy:.2f} > Sell {p_sell:.2f}")
        elif current_type == mt5.POSITION_TYPE_BUY and p_sell > p_buy:
            should_trim = True
            print(f" >>> [{symbol}] OPPOSITION POWER: Sell {p_sell:.2f} > Buy {p_buy:.2f}")

        if should_trim:
            sorted_pos = sorted(positions, key=lambda x: x.time_msc)
            if self.config.get('Oldisgold', True):
                to_close = sorted_pos[1:]
                if len(to_close) > 0:
                    print(f" !!! [{symbol}] Oldisgold active: Keeping trade {sorted_pos[0].ticket}, closing others.")
            else:
                to_close = sorted_pos
                print(f" !!! [{symbol}] Oldisgold false: Closing all positions due to probability flip.")
            
            for p in to_close:
                self.close_specific_position(p, symbol)

    def run(self):
        print(f"\n--- Barot Sniper V4: 17-TF MASTER ---")
        while True:
            if self.check_daily_loss_limit():
                time.sleep(60)
                continue

            for symbol, agent in self.agents.items():
                self.check_smart_book()
                
                rates = mt5.copy_rates_from_pos(symbol, agent.execution_tf, 0, 1)
                if rates is None: continue
                ct = rates[0]['time']
                
                if ct > self.last_processed_time[symbol]:
                    tick = mt5.symbol_info_tick(symbol)
                    if tick and (tick.time - ct) >= self.config['EV_WAIT']:
                        print(f"\n[{symbol}] {datetime.now().strftime('%H:%M:%S')} - New Candle Closed. Analyzing...")
                        
                        self.check_daily_retrain(symbol, agent)
                        self.process_trade(symbol, agent)
                        self.last_processed_time[symbol] = ct
            time.sleep(1)

    def process_trade(self, symbol, agent):
        df = agent.get_mtf_ema_features(symbol, 1000)
        if df is None: 
            print(f"[{symbol}] SKIP: MTF Data fetch failed.")
            return
            
        feature_cols = [c for c in df.columns if 'ema5' in c or 'gap_' in c or 'pivot' in c]
        X_last = df[feature_cols].iloc[-1:].values
        price, atr = df.iloc[-1]['close'], df.iloc[-1]['atr_m5']

        probs = agent.models['direction'].predict_proba(X_last)[0]
        p_noise = probs[0]
        p_buy = probs[1] if len(probs) > 1 else 0.0
        p_sell = probs[2] if len(probs) > 2 else 0.0

        if p_noise > self.config.get('MaxNoise', 0.25):
            print(f"[{symbol}] SKIP: High Noise level. [Noise:{p_noise:.2f} > Max:{self.config.get('MaxNoise', 0.25)}, B:{p_buy:.2f}, S:{p_sell:.2f}]")
            return
        
        self.apply_opposition_power(symbol, p_buy, p_sell)

        current_pos = mt5.positions_get(symbol=symbol, magic=self.config['Magic'])
        if len(current_pos) >= self.config['MaxTradePerSymbol']: 
            print(f"[{symbol}] SKIP: Already have max trades ({len(current_pos)}) active.")
            return
            
        bias = "NEUTRAL"
        act_p = 0.0
        if p_buy > p_sell and p_buy >= self.config['MinDirProb']:
            bias, act_p = "BUY", p_buy
        elif p_sell > p_buy and p_sell >= self.config['MinDirProb']:
            bias, act_p = "SELL", p_sell
        
        if bias == "NEUTRAL":
            print(f"[{symbol}] SKIP: Neutral Bias. [B:{p_buy:.2f} S:{p_sell:.2f} Noise:{p_noise:.2f}]")
            return

        p_tp_pct = agent.models['tp_reg'].predict(X_last)[0]
        p_sl_pct = agent.models['sl_reg'].predict(X_last)[0]
        dir_idx = 1 if bias == "BUY" else 2
        conf = agent.models['confidence'].predict_proba(np.array([[dir_idx, p_tp_pct, p_sl_pct]]))[0][1]

        if conf < self.config['Confidence']:
            print(f"[{symbol}] SKIP: Low Confidence. [Bias:{bias} Conf:{conf:.2f} < Req:{self.config['Confidence']}]")
            return

        # ——— NEW STRUCTURAL GATEKEEPER LOGIC ———
        raw_sl_dist = p_sl_pct * price
        tp_dist = p_tp_pct * price
        
        min_sl_allowed = 0.2 * atr
        max_sl_allowed = 1.5 * atr
        
        if raw_sl_dist < min_sl_allowed or raw_sl_dist > max_sl_allowed:
            print(f"[{symbol}] SKIP: SL Gatekeeper. Predicted SL ({raw_sl_dist:.2f}) outside bounds [{min_sl_allowed:.2f} - {max_sl_allowed:.2f} ATR]")
            return
            
        sl_dist = raw_sl_dist
        # ———————————————————————————————————————————
        
        potential_rr = tp_dist / sl_dist if sl_dist > 0 else 0
        if potential_rr < self.config['MinRr']:
            print(f"[{symbol}] SKIP: RR Filter. [Est RR:{potential_rr:.2f} < Req:{self.config['MinRr']}]")
            return

        if self.config.get('RRCutter'):
            adjusted_rr = max(0.1, potential_rr - self.config.get('RRCutterValue', 1.0))
            tp_dist = adjusted_rr * sl_dist

        tp_p = price + tp_dist if bias == "BUY" else price - tp_dist
        sl_p = price - sl_dist if bias == "BUY" else price + sl_dist
        
        print(f"!!! [{symbol}] {bias} SIGNAL VALID !!! Prob:{act_p:.2f} Conf:{conf:.2f} RR:{potential_rr:.2f}")
        print(f"Price: {price} | TP: {tp_p:.2f} | SL: {sl_p:.2f}")
        self.execute(symbol, bias, tp_p, sl_p)

    def execute(self, symbol, side, tp, sl):
        t = mt5.symbol_info_tick(symbol)
        d = mt5.symbol_info(symbol).digits
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": self.config['Qty'],
            "type": mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL,
            "price": t.ask if side == "BUY" else t.bid,
            "sl": round(sl, d), "tp": round(tp, d),
            "magic": self.config['Magic'], "comment": "SymSniperV4",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res.retcode != mt5.TRADE_RETCODE_DONE:
            print(f"[{symbol}] EXECUTION ERROR: {res.comment}")

if __name__ == "__main__":
    bot = SniperBot()
    bot.run()