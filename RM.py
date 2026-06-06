import os
import sys
import time
import json
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import win32gui
import MetaTrader5 as mt5

# Win32 Window message definitions to toggle the global Algo Trading button
MT_WMCMD_EXPERTS = 32851
WM_COMMAND = 0x0111

CONFIG_NAME = "RMConfig.json"
CACHE_NAME = "news_cache.json"
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

class StandaloneRiskManager:
    def __init__(self):
        print(">>> Initializing Standalone Risk Manager Module...")
        if not mt5.initialize():
            print(f" [!] MT5 Initialization Failed: {mt5.last_error()}")
            sys.exit(1)
            
        self.config = {}
        self.cached_events = []
        self.last_news_refresh = datetime.min.replace(tzinfo=timezone.utc)
        
        self.load_config()
        # Bootstrap immediately from local backup cache on start so we are never blind
        self.load_calendar_from_cache() 
        self.fetch_economic_calendar()

    def load_config(self):
        """Hot-reloads config dynamically. Automatically exports default JSON if missing."""
        default_config = {
            "Prenewsbreak": 5,
            "postnewsbreak": 5,
            "loss": 300,
            "newsimpact": "high, low, medume",
            "Newsrefresh": 1
        }
        
        try:
            if not os.path.exists(CONFIG_NAME):
                with open(CONFIG_NAME, 'w') as f:
                    json.dump(default_config, f, indent=4)
                print(f" [+] Config file '{CONFIG_NAME}' not found. Automatically exported default settings.")
                self.config = default_config
            else:
                with open(CONFIG_NAME, 'r') as f:
                    self.config = json.load(f)
        except Exception as e:
            print(f" [!] Error handling configuration file: {e}")
            self.config = default_config

    def save_calendar_to_cache(self):
        """Serializes current active memory events into a local JSON backup."""
        try:
            serializable_events = []
            for ev in self.cached_events:
                serializable_events.append({
                    "title": ev["title"],
                    "currency": ev["currency"],
                    "impact": ev["impact"],
                    "utc_time": ev["utc_time"].isoformat() # Convert datetime to string
                })
            with open(CACHE_NAME, 'w') as f:
                json.dump(serializable_events, f, indent=4)
        except Exception as e:
            print(f" [!] Failed to write local news backup cache file: {e}")

    def load_calendar_from_cache(self):
        """Recovers calendar events from local JSON storage if CDN is unreachable."""
        if not os.path.exists(CACHE_NAME):
            return False
        try:
            with open(CACHE_NAME, 'r') as f:
                stored_events = json.load(f)
            
            loaded_events = []
            for ev in stored_events:
                loaded_events.append({
                    "title": ev["title"],
                    "currency": ev["currency"],
                    "impact": ev["impact"],
                    "utc_time": datetime.fromisoformat(ev["utc_time"]) # Convert string back to timezone datetime
                })
            self.cached_events = loaded_events
            print(f" [+] Local Backup Cache loaded. Loaded {len(self.cached_events)} macro restriction points.")
            self.print_upcoming_schedule()
            return True
        except Exception as e:
            print(f" [!] Error reading local news backup cache file: {e}")
            return False

    def fetch_economic_calendar(self):
        """Fetches data from CDN with a robust fallback system to handle 429 rate limits."""
        now_utc = datetime.now(timezone.utc)
        refresh_interval = timedelta(hours=self.config.get("Newsrefresh", 1))
        
        if now_utc - self.last_news_refresh < refresh_interval:
            return

        print(f" >>> [{now_utc.astimezone().strftime('%H:%M:%S')}] Updating Economic Calendar from Fair Economy CDN...")
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        
        try:
            response = requests.get(NEWS_URL, headers=headers, timeout=15)
            
            # Catch HTTP issues (like 429 Rate Limits or 503 Server Down)
            if response.status_code != 200:
                print(f" [!] CDN Request Failed (Status: {response.status_code}). Triggering local data fallback...")
                self.load_calendar_from_cache()
                # Cool down network attempts for 15 mins so the 429 limit expires naturally
                self.last_news_refresh = now_utc - refresh_interval + timedelta(minutes=15)
                return
                
            root = ET.fromstring(response.content)
            new_events = []
            
            impact_str = self.config.get("newsimpact", "high, low, medume").lower()
            allowed_impacts = []
            if "high" in impact_str: allowed_impacts.append("high")
            if "low" in impact_str: allowed_impacts.append("low")
            if "med" in impact_str or "medume" in impact_str: allowed_impacts.append("medium")

            for item in root.findall('event'):
                impact = item.find('impact').text.strip().lower()
                if impact not in allowed_impacts:
                    continue
                    
                date_str = item.find('date').text.strip()
                time_str = item.find('time').text.strip()
                currency = item.find('country').text.strip()
                title = item.find('title').text.strip()
                
                if "am" not in time_str.lower() and "pm" not in time_str.lower():
                    continue
                    
                try:
                    event_utc = datetime.strptime(f"{date_str} {time_str}", "%m-%d-%Y %I:%M%p")
                    event_utc = event_utc.replace(tzinfo=timezone.utc)
                    
                    new_events.append({
                        "title": title,
                        "currency": currency,
                        "impact": impact.upper(),
                        "utc_time": event_utc
                    })
                except Exception:
                    continue
                    
            self.cached_events = new_events
            self.last_news_refresh = now_utc
            
            # Success! Commit fresh data to our offline storage file
            self.save_calendar_to_cache()
            self.print_upcoming_schedule()
            
        except Exception as e:
            print(f" [!] Network Error/Parse Exception: {e}. Recovering from local backup files...")
            self.load_calendar_from_cache()
            self.last_news_refresh = now_utc - refresh_interval + timedelta(minutes=15)

    def print_upcoming_schedule(self):
        """Pastes upcoming filtered news events adjusted dynamically to system local time."""
        now_utc = datetime.now(timezone.utc)
        tomorrow_utc = now_utc + timedelta(days=1)
        
        print("\n================== UPCOMING MACRO EVENTS SCHEDULE ==================")
        found = False
        for ev in self.cached_events:
            ev_utc = ev["utc_time"]
            if ev_utc.date() == now_utc.date() or ev_utc.date() == tomorrow_utc.date():
                if ev_utc >= now_utc - timedelta(minutes=60):
                    local_display = ev_utc.astimezone()
                    print(f" [{ev['impact']}] {local_display.strftime('%Y-%m-%d %H:%M')} | {ev['currency']} - {ev['title']}")
                    found = True
        if not found:
            print(" No matching impact events detected for today or tomorrow.")
        print("====================================================================\n")

    def get_broker_day_start_timestamp(self):
        """Finds the precise beginning of the broker day using dynamic sequence checking."""
        discovery_pairs = ["EURUSD", "BTCUSD", "GBPUSD", "USDJPY", "XAUUSD"]
        
        for pair in discovery_pairs:
            for variant in [pair, pair.lower(), f"{pair}m", f"{pair}+", f"{pair}.pro"]:
                rates = mt5.copy_rates_from_pos(variant, mt5.TIMEFRAME_D1, 0, 1)
                if rates is not None and len(rates) > 0:
                    return int(rates[0]['time'])

        local_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return int(local_midnight.timestamp())

    def get_daily_candle_pnl(self):
        """Calculates precise net P&L accumulated since the current daily candle open."""
        d1_open_timestamp = self.get_broker_day_start_timestamp()
        
        closed_pnl = 0.0
        deals = mt5.history_deals_get(d1_open_timestamp, int(time.time() + 86400))
        if deals:
            for deal in deals:
                if deal.type in [mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL]:
                    closed_pnl += (deal.profit + deal.swap + deal.commission)
                    
                if deal.type == mt5.DEAL_TYPE_BALANCE and deal.profit < -abs(self.config["loss"]):
                    closed_pnl += deal.profit

        floating_pnl = 0.0
        positions = mt5.positions_get()
        if positions:
            for pos in positions:
                floating_pnl += (pos.profit + pos.swap)
                
        return closed_pnl + floating_pnl

    def is_inside_news_window(self):
        """Checks if absolute UTC time sits inside the pre-news or post-news restriction blocks."""
        now_utc = datetime.now(timezone.utc)
        pre_break = self.config.get("Prenewsbreak", 5)
        post_break = self.config.get("postnewsbreak", 5)
        
        for ev in self.cached_events:
            ev_utc = ev["utc_time"]
            start_block = ev_utc - timedelta(minutes=pre_break)
            end_block = ev_utc + timedelta(minutes=post_break)
            
            if start_block <= now_utc <= end_block:
                return True, ev
                
        return False, None

    def set_algo_trading_state(self, target_state: bool):
        """Communicates via background Windows Messaging API to force the MT5 state button."""
        term_info = mt5.terminal_info()
        if not term_info:
            return
            
        current_state = term_info.trade_allowed
        if current_state == target_state:
            return 
            
        def enum_windows_callback(hwnd, extra):
            try:
                title = win32gui.GetWindowText(hwnd).lower()
                class_name = win32gui.GetClassName(hwnd).lower()
                
                if "metatrader5cl" in class_name or "metatrader 5" in title or "mt5" in title:
                    extra.append(hwnd)
            except Exception:
                pass
            return True

        hwnds = []
        win32gui.EnumWindows(enum_windows_callback, hwnds)
        
        if hwnds:
            win32gui.PostMessage(hwnds[0], WM_COMMAND, MT_WMCMD_EXPERTS, 0)
            print(f" [>>>] Risk Manager shifted global MT5 Algo Trading state to: {target_state}")
            time.sleep(1.0) 
        else:
            print(" [!] Critical: MetaTrader 5 interface window not detected.")
            print("     -> Hint: If MT5 is running as Admin, you MUST run this script terminal as Admin too!")

    def run_monitor_loop(self):
        """Core engine monitoring execution loops."""
        print(">>> Risk Manager running. Press Ctrl+C to terminate application.")
        
        while True:
            try:
                self.load_config()
                self.fetch_economic_calendar()
                
                daily_pnl = self.get_daily_candle_pnl()
                max_loss_limit = -abs(self.config.get("loss", 300))
                in_news_window, breaking_event = self.is_inside_news_window()
                
                if daily_pnl <= max_loss_limit:
                    print(f" [!!!] SHUTDOWN: Daily Loss Reach Limit. PnL: ${daily_pnl:.2f} <= ${max_loss_limit:.2f}")
                    self.set_algo_trading_state(False)
                    
                elif in_news_window:
                    print(f" [!!!] NEWS HALO: Block active for [{breaking_event['impact']}] {breaking_event['title']}")
                    self.set_algo_trading_state(False)
                    
                else:
                    self.set_algo_trading_state(True)
                    
                sys.stdout.write(f"\r Daily PnL: ${daily_pnl:+.2f} | News Filter Status: {'BLOCKED' if in_news_window else 'CLEAR'} ")
                sys.stdout.flush()
                
                time.sleep(5) 
                
            except KeyboardInterrupt:
                print("\n>>> Risk Manager shutdown cleanly.")
                mt5.shutdown()
                break
            except Exception as e:
                print(f"\n [!] Internal Monitor Exception Loop Error: {e}")
                time.sleep(5)

if __name__ == "__main__":
    manager = StandaloneRiskManager()
    manager.run_monitor_loop()