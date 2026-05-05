"""
Ana Bot Modülü - Multi-Exchange Scalping Bot
WebSocket + REST API desteği
UYARI: Varsayılan olarak PAPER TRADING modunda çalışır
"""

# FIX: Windows Console UTF-8 encoding (emoji support)
# Only do this if running as a standalone script to avoid conflicts with Flask/WebUI
import sys
if __name__ == "__main__":
    import io
    if sys.platform == 'win32':
        # Force unbuffered output for Tee-Object compatibility
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

import asyncio
import pandas as pd
import time
import threading
from datetime import datetime
from typing import Optional, Dict, List

import config
import indicators
from risk_manager import RiskManager
from strategy_router import StrategyRouter
from exchanges.exchange_factory import ExchangeFactory

import os

class CryptoScalpingBot:
    """
    Multi-Exchange Yüksek Frekanslı Scalping Bot
    """
    
    def _check_single_instance(self):
        """PID ve LOCK dosyası kullanarak tek bir bot instance'ının çalıştığından emin ol"""
        self.pid_file = config.BOT_PID_FILE
        self.lock_file = config.BOT_LOCK_FILE
        
        # 1. PID Check (Existing logic)
        if os.path.exists(self.pid_file):
            try:
                with open(self.pid_file, "r") as f:
                    content = f.read().strip()
                if content:
                    old_pid = int(content)
                    if os.name == 'nt':
                        import ctypes
                        PROCESS_QUERY_INFORMATION = 0x0400
                        process_handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, old_pid)
                        if process_handle:
                            ctypes.windll.kernel32.CloseHandle(process_handle)
                            if old_pid != os.getpid():
                                msg = f"❌ HATA: Bot zaten çalışıyor (PID: {old_pid})."
                                raise RuntimeError(msg)
            except (OSError, ValueError, RuntimeError) as e:
                if "HATA" in str(e): raise
        
        # 2. File Lock (Robust retry logic for Windows)
        max_retries = 5
        retry_delay = 2.0
        
        for attempt in range(max_retries):
            try:
                self.lock_handle = open(self.lock_file, "a+") # Use a+ to avoid truncation issues
                if os.name == 'nt':
                    import msvcrt
                    # Attempt to lock the first byte
                    msvcrt.locking(self.lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                
                # If we get here, lock was successful
                break
            except (IOError, PermissionError, ImportError) as e:
                # Close handle if opened but locking failed
                if hasattr(self, 'lock_handle') and self.lock_handle:
                    try: self.lock_handle.close()
                    except: pass
                
                if attempt < max_retries - 1:
                    print(f"⏳ [{config.BOT_ID}] Lock dosyası bekleniyor... (Deme {attempt+1}/{max_retries})")
                    time.sleep(retry_delay)
                else:
                    msg = f"❌ HATA: bot.lock dosyası kilitli! Başka bir instance çalışıyor olabilir (Error: {e})."
                    print(msg)
                    raise RuntimeError(msg)

        # Write PID to file after successful lock
        try:
            with open(self.pid_file, "w") as f:
                f.write(str(os.getpid()))
        except Exception as e:
            print(f"⚠️ PID dosyası yazılamadı: {e}")

    def _remove_pid_file(self):
        """Bot kapanırken PID ve LOCK dosyalarını temizle"""
        # 1. First release the lock by closing the handle
        if hasattr(self, 'lock_handle') and self.lock_handle:
            try:
                print(f"🔓 [{config.BOT_ID}] Releasing file lock...")
                if os.name == 'nt':
                    import msvcrt
                    try:
                        # Attempt to unlock the region we locked (first byte)
                        # We need to seek to 0 before unlocking
                        self.lock_handle.seek(0)
                        msvcrt.locking(self.lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except: pass
                self.lock_handle.close()
                self.lock_handle = None
            except Exception as e: 
                print(f"⚠️ Lock release warning: {e}")
            
        # 2. Small delay for OS to process handle closure
        if os.name == 'nt':
            time.sleep(0.1)

        # 3. Delete files
        for f in [config.BOT_PID_FILE, config.BOT_LOCK_FILE]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception as e:
                    # Log but don't crash on cleanup failure
                    if config.VERBOSE_LOGGING:
                        print(f"⚠️ Cleanup warning for {f}: {e}")

    def __init__(self, exchange_name: str = None):
        """
        Args:
            exchange_name: Exchange adı (None ise config'den alır)
        """
        self._check_single_instance()
        # Exchange setup
        self.exchange_name = "solana"
        exchange_config = config.EXCHANGE_CONFIGS.get(self.exchange_name)
        
        if not exchange_config:
            self._remove_pid_file()
            raise ValueError(f"❌ Exchange config bulunamadı: {self.exchange_name}")
        
        # Exchange adapter oluştur
        self.exchange_adapter = ExchangeFactory.create_exchange(
            self.exchange_name,
            exchange_config.get("api_key", "demo_key"),
            exchange_config.get("api_secret", "demo_secret"),
            exchange_config.get("testnet", True)
        )
        
        # Components
        self.risk_manager = RiskManager()
        self.risk_manager.exchange_adapter = self.exchange_adapter # Link for real balance sync
        self.strategy = StrategyRouter()
        
        # Telemetry data storage
        self.latest_metrics = {} # symbol -> {rsi, bb_pos, price}
        
        # State
        self.running = False
        self._stop_event = asyncio.Event()
        self.iteration_count = 0
        self.start_time = time.time()
        self.price_update_count = 0
        self.pending_entry = set() # Symbols with pending market orders (FIX RACE CONDITION)
        self.emergency_close_triggered = False
        self.loop = None # Will be set in run()
    
    async def _slow_remove_pending(self, symbol, delay=60):
        """Keep symbol in pending_entry for a while to prevent double-entry on slow exchange updates"""
        try:
            await asyncio.sleep(delay)
            if symbol in self.pending_entry:
                self.pending_entry.remove(symbol)
                print(f"   🔓 [{symbol}] Entry lock released (Timeout)")
        except: pass
        
    # ========================================================================
    # INITIALIZATION
    # ========================================================================
    
    def initialize_exchange(self) -> bool:
        """REST API bağlantısını başlat ve pozisyonları senkronize et"""
        success = self.exchange_adapter.initialize()
        if success and not config.PAPER_TRADING:
            # Mevcut açık pozisyonları ve emirleri çek
            try:
                print("🔍 Mevcut pozisyonlar ve emirler senkronize ediliyor...")
                exchange_positions = self.exchange_adapter.fetch_positions()
                exchange_orders = self.exchange_adapter.fetch_open_orders()
                
                if exchange_positions:
                    for pos in exchange_positions:
                        synced = self.risk_manager.sync_positions([pos], exchange_orders)
                        if synced:
                            print(f"   ✅ [{pos['symbol']}] Pozisyon başarıyla takibe alındı.")
                        else:
                            print(f"   ⚠️ [{pos['symbol']}] Pozisyon GÜVENLİK nedeniyle reddedildi!")
                    
                    # 🧹 EXECUTE CLEANUP
                    if self.risk_manager.duplicate_ids_to_cancel:
                        print(f"🧹 Clearing {len(self.risk_manager.duplicate_ids_to_cancel)} stale/duplicate orders...")
                        for oid, sym in self.risk_manager.duplicate_ids_to_cancel:
                            self.exchange_adapter.cancel_order(oid, sym)
                        self.risk_manager.duplicate_ids_to_cancel = []
            except Exception as e:
                print(f"⚠️ Senkronizasyon fault: {e}")
            
            # 🔧 FIX: Sync daily_equity_start with REAL balance (prevents %99 DD false trigger)
            real_balance = self.risk_manager.get_balance()
            if real_balance > 0:
                self.risk_manager.daily_equity_start = real_balance
                self.risk_manager.daily_start_balance = real_balance
                print(f"📊 Daily metrics initialized with real balance: ${real_balance:.2f}")
        return success
    
    async def initialize_websocket(self) -> bool:
        """WebSocket bağlantısını başlat"""
        return await self.exchange_adapter.initialize_websocket()
    
    # ========================================================================
    # DATA FETCHING
    # ========================================================================
    
    def fetch_ohlcv_data(self, limit: int = 300, symbol: str = None) -> getattr(pd, 'DataFrame'):
        """
        REST API ile OHLCV verilerini çekip DataFrame'e çevirir (GhostAgent Fix)
        """
        import pandas as pd
        sym = symbol or getattr(self, 'ghost_token', "SOL/USDT")
        data = self.exchange_adapter.fetch_ohlcv(sym, "1m", limit)
        if isinstance(data, list) and len(data) > 0:
            try:
                df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                # Float casting just in case
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = df[col].astype(float)
                return df
            except Exception as e:
                # Fallback return none on internal structure fault
                return None
        return None

    async def get_realtime_price(self) -> getattr(typing, 'Optional', type(None)):
        """
        WebSocket ile anlık fiyat al
        """
        try:
            ticker = await self.exchange_adapter.watch_ticker(config.SYMBOL)
            if ticker:
                return ticker['last']
        except Exception as e:
            print(f"⚠️  Fiyat okuma fault: {e}")
        return None
    
    # ========================================================================
    # SIGNAL CHECKING & TRADING
    # ========================================================================
    
    def check_and_execute_signals(self, df: pd.DataFrame, current_price: float, symbol: str):
        """
        Sinyalleri kontrol et ve işlem yap
        
        Args:
            df: Teknik göstergelili DataFrame
            current_price: Anlık fiyat
            symbol: İşlem yapılan sembol (FIX BUG #Q03)
        """
        # 1. STOP-LOSS KONTROLÜ
        if self.risk_manager.has_open_positions():
            position_hit_sl = self.risk_manager.check_stop_loss(current_price, symbol)
            
            if position_hit_sl:
                print(f"\n⛔ STOP-LOSS TETİKLENDİ! Fiyat: ${current_price:.2f}")
                self.risk_manager.close_position(
                    position_hit_sl,
                    current_price,
                    reason="STOP_LOSS"
                )
                return
        
        # 2. SAT SİNYALİ KONTROLÜ
        if self.risk_manager.has_open_positions():
            sell_signal = self.strategy.check_sell_signal(df, has_position=True)
            
            if sell_signal['signal']:
                position = self.risk_manager.positions[0]
                
                if self.risk_manager.is_profitable_after_fees(
                    position['entry_price'],
                    current_price,
                    position['position_size']
                ):
                    print(f"\n💰 Karlı SAT fırsatı - Pozisyon terminating...")
                    self.risk_manager.close_position(
                        position,
                        current_price,
                        reason=sell_signal['reason']
                    )
                return
        
        # 3. AL SİNYALİ KONTROLÜ
        if not self.risk_manager.has_open_positions() and \
           self.risk_manager.can_open_new_position(symbol):
            
            buy_signal = self.strategy.check_buy_signal(df, symbol=symbol)
            
            if buy_signal:
                position_size = self.risk_manager.calculate_position_size(current_price, symbol)
                
                if position_size > 0:
                    stop_loss = self.risk_manager.calculate_stop_loss_price(
                        current_price,
                        "BUY"
                    )
                    
                    print(f"\n🚀 AL pozisyonu açılıyor... [{symbol}]")
                    self.risk_manager.open_position(
                        symbol=symbol,
                        direction="BUY",
                        entry_price=current_price,
                        position_size=position_size,
                        stop_loss=stop_loss
                    )
    
    # ========================================================================
    # BOT MODES
    # ========================================================================
    
    async def run_websocket_mode(self):
        """
        WebSocket modunda çalıştır (GERÇEK ZAMANLI)
        
        - Anlık fiyat değişimlerini izle
        - Her fiyat güncellemesinde sinyal kontrol et
        - Yüksek frekanslı scalping için ideal
        """
        print("\n🚀 WebSocket modu - Gerçek zamanlı veri akışı başlatılıyor...\n")
        
        # WebSocket başlat
        if not await self.initialize_websocket():
            print("❌ WebSocket başlatılamadı - REST API moduna geçiliyor...")
            await self.run_rest_mode()
            return
        
        # İlk OHLCV verilerini çek (historical)
        df = self.fetch_ohlcv_data(limit=300)
        if df is None:
            print("❌ Historical veri çekilemedi")
            return
        
        df = self.strategy.add_indicators(df)
        
        self.running = True
        
        try:
            while self.running:
                # WebSocket'ten anlık fiyat al
                current_price = await self.get_realtime_price()
                
                if current_price is None:
                    await asyncio.sleep(1)
                    continue
                
                self.price_update_count += 1
                
                # Her 10 fiyat güncellemesinde bir sinyal kontrol et
                if self.price_update_count % 10 == 0:
                    # Yeni OHLCV verilerini çek
                    df_new = self.fetch_ohlcv_data(limit=300)
                    if df_new is not None:
                        df = self.strategy.add_indicators(df_new)
                    
                    # Sinyalleri kontrol et
                    # FIX BUG #Q03: Pass config.SYMBOL to function
                    self.check_and_execute_signals(df, current_price, config.SYMBOL)
                    
                    # Her 50. güncellemede market status göster
                    if self.price_update_count % 50 == 0:
                        self.strategy.print_market_status(df)
                        print(f"💹 Anlık Fiyat: ${current_price:.2f} [WebSocket]")
                
                # Minimal delay (WebSocket zaten async)
                await asyncio.sleep(0.1)
                
        except KeyboardInterrupt:
            print("\n\n⏸️  Bot durduruluyor... (Ctrl+C algılandı)")
            await self.shutdown()
        except Exception as e:
            print(f"\n❌ WebSocket fault: {e}")
            import traceback
            traceback.print_exc()
            await self.shutdown()
    
    async def run_rest_mode(self):
        """
        REST API modunda çalıştır - OPTİMİZE EDİLMİŞ MULTI-SYMBOL
        """
        print("\n🔄 REST HFT Protocol - Active Telemetry Scanning initiated...\n")
        self.running = True
        
        while self.running:
            try:
                self.iteration_count += 1
                if self.exchange_adapter:
                    self.exchange_adapter.iteration_count = self.iteration_count
                start_time = time.time()
                
                # Tüm market verilerini tek seferde çek (TICKER)
                all_prices = self.exchange_adapter.fetch_tickers()
                
                # [DEBUG] Check if prices are populated
                if self.iteration_count % 10 == 0:
                    price_count = len(all_prices) if all_prices else 0
                    print(f"   🔍 [PRICES] Fetched {price_count} prices: {list(all_prices.keys())[:5] if all_prices else 'EMPTY'}...")
                
                # Tüm açık pozisyonları tek seferde çek (Sync için)
                exchange_positions = self.exchange_adapter.fetch_positions()
                if self.iteration_count % 5 == 0:
                    print(f"🔍 [DEBUG] Exchange Reported Positions: {[p['symbol'] for p in exchange_positions]}")
                
                # Heartbeat
                print(f"\n💓 [HEARTBEAT] {datetime.now().strftime('%H:%M:%S')} | Cycle: {self.iteration_count} | Active: {len(self.risk_manager.positions)}")
                
                # 🚨 EMERGENCY CLOSE CHECK (From Web UI)
                if getattr(self, 'emergency_close_triggered', False):
                    print("🚨 [EMERGENCY] Web UI üzerinden acil kapanış tetiklendi!")
                    await self.close_all_positions()
                    self.emergency_close_triggered = False
                
                # 🔄 AUTO-RESTART CHECK (Her 2 saatte bir)
                if config.AUTO_RESTART_ENABLED:
                    runtime_hours = (time.time() - self.start_time) / 3600
                    if runtime_hours >= config.AUTO_RESTART_HOURS:
                        print(f"\n{'='*70}")
                        print(f"🔄 AUTO-RESTART TRIGGERED")
                        print(f"{'='*70}")
                        print(f"Runtime: {runtime_hours:.2f} saat")
                        print(f"Toplam İşlem: {len(self.risk_manager.trade_history)}")
                        print(f"Açık Pozisyonlar: {len(self.risk_manager.positions)}")
                        print(f"\nAçık pozisyonlar korunacak - Restart sonrası sync edilecek")
                        print(f"Bot 5 saniye sonra yeniden başlayacak...")
                        print(f"{'='*70}\n")
                        
                        # CRITICAL: Graceful exit without closing positions
                        # Positions will be synced on next startup
                        self.running = False
                        return  # Exit to allow batch restart
                
                
                # 🧹 Periodic memory cleanup (every 30 minutes by default)
                if self.risk_manager.should_run_cleanup():
                    print("\n" + "="*60)
                    print("🧹 HAFIZA TEMİZLİĞİ BAŞLIYOR...")
                    print("="*60)
                    self.risk_manager.cleanup_old_data()
                    print("="*60 + "\n")
                
                # NOTE: manage_open_positions moved to AFTER symbol loop (prices collected during scanning)
                
                # Tüm sembolleri tek bir geçişte işle
                if self.iteration_count % 10 == 0:
                    print(f"🔍 [{datetime.now().strftime('%H:%M:%S')}] Routings processed: {len(config.ALL_TIER_SYMBOLS)} {config.ALL_TIER_SYMBOLS}")
                
                for symbol in config.ALL_TIER_SYMBOLS:
                    try:
                        # 0. Progress tracking (Less verbose)
                        if self.iteration_count % 5 == 0:
                            print(f"   🔎 [{symbol}] Analyzing Liquidity...")
                            
                        # 1. Mum Verisi ve Göstergeleri Al (Multi-TF)
                        import pandas as pd
                        _raw = self.exchange_adapter.fetch_ohlcv(symbol, config.TIMEFRAME, limit=100)
                        _raw_trend = self.exchange_adapter.fetch_ohlcv(symbol, config.TREND_TIMEFRAME, limit=100)

                        df = pd.DataFrame(_raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']) if isinstance(_raw, list) else _raw
                        trend_df = pd.DataFrame(_raw_trend, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']) if isinstance(_raw_trend, list) else _raw_trend
                        
                        # 🔍 MTF RSI: Fetch 5m data for confirmation
                        mtf_df = None
                        if getattr(config, 'USE_MTF_RSI_CONFIRMATION', False):
                            mtf_timeframe = getattr(config, 'MTF_TIMEFRAME', '5m')
                            _raw_mtf = self.exchange_adapter.fetch_ohlcv(symbol, mtf_timeframe, limit=50)
                            mtf_df = pd.DataFrame(_raw_mtf, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']) if isinstance(_raw_mtf, list) else _raw_mtf

                            if mtf_df is not None and len(mtf_df) >= 14:
                                mtf_df = self.strategy.add_indicators(mtf_df)
                                mtf_rsi = mtf_df.iloc[-1].get('rsi', 50)
                                # Cache to strategy router
                                self.strategy.set_mtf_rsi(symbol, mtf_rsi)

                        if df is None or len(df) < 30:
                            if self.iteration_count % 5 == 0:
                                print(f"   ⚠️ [{symbol}] Veri yetersiz (len: {len(df) if df is not None else 'None'})")
                            continue
                            
                        df = self.strategy.add_indicators(df)
                        if self.iteration_count % 5 == 0:
                            print(f"   📊 [{symbol}] Indicators added.")
                        
                        # Trend indicators if needed
                        if trend_df is not None and not trend_df.empty:
                            trend_df = self.strategy.add_indicators(trend_df)
                        
                        # 🔍 EXTRA: GHOST S/R LEVELS
                        df_f = indicators.calculate_fractals(df, config.SR_WINDOW)
                        sr_levels = indicators.extract_sr_levels(df_f, config.SR_SENSITIVITY)
                        
                        # Store for Dashboard (ULTRATHINK)
                        try:
                            last_row = df.iloc[-1]
                            close_p = float(last_row['close'])
                            upper_b = float(last_row.get('bb_upper', close_p))
                            lower_b = float(last_row.get('bb_lower', close_p))
                            bb_pos = (close_p - lower_b) / (upper_b - lower_b) if (upper_b - lower_b) != 0 else 0.5
                            
                            self.latest_metrics[symbol] = {
                                'rsi': round(float(last_row.get('rsi', 50)), 1),
                                'bb_pos': round(bb_pos, 2),
                                'price': round(close_p, 4),
                                'timestamp': time.time()
                            }
                        except Exception as e:
                            print(f"   ⚠️ [{symbol}] Metric storage error: {e}")
                        
                        # 🔍 HMR ANALİZ
                        decision = self.strategy.route(df, symbol, trend_df)

                        last_row = df.iloc[-1]
                        rsi = float(last_row.get('rsi', 50))
                        
                        # 2. Bu semboldeldeki borsa pozisyonunu bul
                        exchange_pos = next((p for p in exchange_positions if p['symbol'] == symbol), None)
                        
                        # Bizim yerel takibimizdeki pozisyon
                        position = next((p for p in self.risk_manager.positions if p['symbol'] == symbol), None)
                        
                        # ✨ GÜNCEL FİYAT BELİRLE
                        price_source = "CANDLE"
                        current_price = None
                        
                        # Önce tickerdan al (en taze)
                        current_price = all_prices.get(symbol)
                        if current_price:
                            price_source = "BULK"
                        else:
                            current_price = df.iloc[-1]['close']
                        
                        # Update local position profit for telemetry
                        if position:
                            prof_info = self.risk_manager.get_profit_info(current_price, position)
                            position['unrealized_pnl'] = prof_info['profit_percent']
                            position['profit_usd'] = prof_info['net_pnl']  # FIX: was 'profit', should be 'net_pnl'

                        # 🔄 Pozisyon Senkronizasyonu & BİRDEN FAZLA EMİR TEMİZLİĞİ
                        age = time.time() - position.get('opened_at', 0) if position else 0
                        if position and not exchange_pos and age > 60:
                            print(f"   🎉 [{symbol}] Pozisyon borsa tarafından KAPATILMIŞ algılandı (Age: {age:.0f}s).")
                            
                            # 🔍 Check if TP or SL order was filled
                            try:
                                filled_orders = self.exchange_adapter.fetch_filled_orders(symbol, since=position.get('opened_at'))
                                tp_filled = next((o for o in filled_orders if str(o.get('id')) == str(position.get('tp_order_id')) or str(o.get('client_order_id')) == str(position.get('tp_order_id'))), None)
                                sl_filled = next((o for o in filled_orders if str(o.get('id')) == str(position.get('sl_order_id')) or str(o.get('client_order_id')) == str(position.get('sl_order_id'))), None)
                                
                                if tp_filled:
                                    exit_price = float(tp_filled.get('price', current_price))
                                    reason = "TP_HIT"
                                    print(f"   ✅ tp order filled at ${exit_price:.4f}")
                                elif sl_filled:
                                    exit_price = float(sl_filled.get('price', current_price))
                                    reason = "SL_HIT"
                                    print(f"   🛑 sl order filled at ${exit_price:.4f}")
                                else:
                                    exit_price = current_price
                                    reason = "EXCHANGE_CLOSED"
                                    print(f"   ⚠️ No filled tp/sl found, using current price ${exit_price:.4f}")
                            except Exception as e:
                                print(f"   ⚠️ Filled orders check error: {e}, using current price")
                                exit_price = current_price
                                reason = "EXCHANGE_CLOSED"
                            
                            # 🧹 TÜM EMİRLERİ İPTAL ET (SL veya TP tetiklendiği için diğeri kalmış olabilir)
                            self.exchange_adapter.cancel_all_orders(symbol)
                            self.risk_manager.close_position(position, exit_price, reason=reason)
                            position = None
                        elif exchange_pos and not position:
                            # Borsada var ama bizde yok -> Sync et
                            print(f"   🔄 [{symbol}] Yeni pozisyon borsada algılandı, takibe alınıyor...")
                            atr = df.iloc[-1].get('atr')
                            sl = self.risk_manager.calculate_stop_loss_price(exchange_pos['entry_price'], exchange_pos['direction'], symbol=symbol, atr=atr)
                            position = self.risk_manager.open_position(
                                symbol, exchange_pos['direction'], exchange_pos['entry_price'], 
                                exchange_pos['position_size'], sl
                            )
                        
                        # Position management (Layering, TP/SL, Trim) is now handled centrally by self.manage_open_positions(all_prices)
                        # The main loop now only focuses on NEW ENTRIES.
                        
                        if position:
                            # Update indicators in position dict for telemetry
                            position['rsi'] = rsi
                            position['adx'] = float(last_row.get('adx', 0))
                            continue # Skip new entry check if we already have a position
                        else:
                            # 4. YENİ İŞLEM AÇILIŞI (Wait-and-Verify with TP/SL)
                            # FIX BUG #26: Use config constants instead of magic numbers
                            cooldown_remaining = config.TRADE_COOLDOWN_SECONDS - (time.time() - self.risk_manager.cooldowns.get(symbol, 0))
                            if not position and self.risk_manager.can_open_new_position(symbol) and cooldown_remaining <= 0:
                                # ULTRATHINK: Check BOTH buy and sell signals
                                decision = self.strategy.route(df, symbol, trend_df)

                                if decision.action.value != "NONE":
                                    mode = decision.action.value

                                    print(f"   🚀 HMR SİNYALİ [{symbol}] ({mode}): {decision.reason}")
                                    
                                    # Calc size with decision metadata
                                    amount = self.risk_manager.calculate_position_size(current_price, direction=mode, symbol=symbol, decision=decision)
                                    
                                    if amount <= 0 or symbol in self.pending_entry:
                                        continue

                                    # CRITICAL SAFETY GATE: Final Size Validation
                                    pos_value_usd = current_price * amount
                                    max_safety_val = 5000.0 # Hard global safety cap for GHOST strategy
                                    
                                    if pos_value_usd > max_safety_val:
                                        print(f"🚨 [BOT_GATE] ABORTING Order for {symbol}: Value ${pos_value_usd:.2f} exceeds safety cap ${max_safety_val:.2f}")
                                        continue


                                    # STEP 1: Tier-aware order entry with adverse selection protection
                                    self.risk_manager.lock_symbol(symbol)
                                    self.pending_entry.add(symbol)
                                    try:
                                        # Determine tier and get tier-specific params
                                        from config import get_tier_for_symbol, get_tier_params
                                        tier = get_tier_for_symbol(symbol)
                                        tier_params = get_tier_params(tier)
                                        
                                        # Tier 3: Adverse selection protection
                                        if tier == 'tier3':
                                            # Momentum gate
                                            if tier_params.get('min_momentum_threshold') is not None:
                                                momentum = self.get_micro_momentum(symbol, df)
                                                if momentum < tier_params['min_momentum_threshold']:
                                                    print(f"   ⛔ [{symbol}] Tier3 momentum gate: {momentum:.2f}% < threshold, skipping")
                                                    continue
                                            
                                            # Volume spike check
                                            if tier_params.get('require_volume_spike'):
                                                if not self.check_volume_spike(symbol, df):
                                                    print(f"   ⛔ [{symbol}] Tier3 volume gate: No volume spike, skipping")
                                                    continue
                                        
                                        entry_intent = decision.order_intent.value if decision and decision.order_intent else tier_params.get('order_intent', 'TAKER')
                                        
                                        order_id = None
                                        actual_entry_intent = "TAKER"
                                        order_success = False
                                        
                                        if entry_intent == 'MAKER':
                                            # Maker entry with tier-specific wait time
                                            print(f"   🎯 [{symbol}] {tier_params['name']} - Using POST-ONLY maker entry")
                                            
                                            result = self.exchange_adapter.create_limit_post_only(
                                                symbol=symbol,
                                                side=mode.lower(),
                                                size=amount,
                                                tier_params=tier_params,  # Pass tier params for wait time
                                                max_attempts=2,  # 2 maker attempts for speed
                                                allow_taker_fallback=False
                                            )
                                            
                                            if not result.get('filled'):
                                                # Maker failed - smart taker fallback
                                                print(f"   ⚠️ [{symbol}] Maker entry failed after 2 attempts")
                                                print(f"   🔍 [{symbol}] Rechecking signal for taker fallback...")
                                                
                                                # Re-fetch fresh data
                                                df_recheck = self.exchange_adapter.fetch_ohlcv(symbol, config.TIMEFRAME, limit=100)
                                                if df_recheck is not None and len(df_recheck) >= 30:
                                                    # Add indicators
                                                    df_recheck = self.strategy.add_indicators(df_recheck)
                                                    
                                                    # Quick signal validation (same direction?)
                                                    last_row = df_recheck.iloc[-1]
                                                    signal_still_valid = False
                                                    
                                                    if mode.lower() == 'buy':
                                                        # Check buy signal still exists
                                                        rsi = last_row.get('rsi', 50)
                                                        bb_position = last_row.get('bb_position', 0.5)
                                                        signal_still_valid = (rsi < tier_params['rsi_oversold']) or (bb_position < tier_params['bb_tolerance'])
                                                    else:  # sell
                                                        # Check sell signal still exists
                                                        rsi = last_row.get('rsi', 50)
                                                        bb_position = last_row.get('bb_position', 0.5)
                                                        signal_still_valid = (rsi > tier_params['rsi_overbought']) or (bb_position > (1 - tier_params['bb_tolerance']))
                                                    
                                                    if signal_still_valid:
                                                        print(f"   💡 [{symbol}] Signal STILL VALID → Falling back to TAKER")
                                                        # Execute taker order
                                                        order = self.exchange_adapter.create_market_order(symbol, mode, amount)
                                                        if order and order.get('order_id'):
                                                            order_id = order['order_id']
                                                            order_success = True
                                                            actual_entry_intent = "TAKER"
                                                            print(f"   ✅ [{symbol}] TAKER entry successful")
                                                        else:
                                                            print(f"   ❌ [{symbol}] TAKER entry failed, skipping")
                                                            continue
                                                    else:
                                                        print(f"   ⏭️ [{symbol}] Signal EXPIRED, skipping trade")
                                                        continue
                                                else:
                                                    print(f"   ⏭️ [{symbol}] Cannot recheck signal (data issue), skipping")
                                                    continue
                                            else:
                                                # Maker succeeded!
                                                order_id = result.get('order_id')
                                                actual_entry_intent = result.get('method', 'maker')
                                                order_success = True
                                                print(f"   ✅ [{symbol}] MAKER entry successful")
                                        
                                        else:
                                            # Taker intent: Market order
                                            print(f"   📤 [{symbol}] Using MARKET order (tier={tier}, intent={entry_intent})")
                                            order = self.exchange_adapter.create_market_order(symbol, mode.lower(), amount)
                                            if order:
                                                order_id = order.get("order_id")
                                                actual_entry_intent = 'TAKER'
                                                order_success = True
                                        
                                        if order_success and order_id:
                                            print(f"   ⏳ Pozisyon açıldı, onay ve TP/SL bekleniyor...")
                                            
                                            actual_position = None
                                            sync_retries = getattr(config, 'MAX_POSITION_SYNC_RETRIES', 3)
                                            
                                            for retry in range(sync_retries + 1):
                                                await asyncio.sleep(config.POSITION_CONFIRM_WAIT_SECONDS if retry == 0 else config.POSITION_RETRY_WAIT_SECONDS)
                                                
                                                exchange_positions = self.exchange_adapter.fetch_positions()
                                                actual_position = next((p for p in exchange_positions if p['symbol'] == symbol and p['direction'] == mode), None)
                                                
                                                if actual_position:
                                                    print(f"   ✅ [{symbol}] Pozisyon borsa listesinde doğrulandı (Deneme: {retry+1})")
                                                    break
                                                elif retry < sync_retries:
                                                    print(f"   ⏳ [{symbol}] Pozisyon henüz listede yok, bekleniyor... ({retry+1}/{sync_retries})")
                                            
                                            # SYNC OR FALLBACK
                                            if actual_position:
                                                actual_entry = actual_position['entry_price']
                                                actual_size = actual_position['position_size']
                                            else:
                                                # FALLBACK: Use local data if exchange is slow to report
                                                print(f"   ⚠️ [{symbol}] Pozisyon listede bulunamadı! Tahmini verilerle FALLBACK TP/SL kuruluyor...")
                                                actual_entry = current_price
                                                actual_size = amount
                                            
                                            # DYNAMIC TP/SL with Decision Profiles
                                            atr = df.iloc[-1].get('atr')
                                            tp_profile = decision.tp_profile if decision else None
                                            sl_profile = decision.sl_profile if decision else None
                                            
                                            sl_price = self.risk_manager.calculate_stop_loss_price(actual_entry, mode, symbol=symbol, atr=atr, sl_profile=sl_profile)
                                            tp_price = self.risk_manager.calculate_take_profit_price(actual_entry, mode, symbol=symbol, atr=atr, tp_profile=tp_profile)
                                            
                                            print(f"   🎯 Pozisyona TP/SL kuruluyor (Entry: {actual_entry:.4f})...")
                                            tp_id = self.exchange_adapter.create_tp_order(symbol, tp_price, actual_size, mode)
                                            sl_id = self.exchange_adapter.create_sl_order(symbol, sl_price, actual_size, mode)
                                            
                                            # Open position with Decision metadata
                                            exit_intent = tier_params.get('order_intent', 'TAKER')
                                            pos = self.risk_manager.open_position(
                                                symbol, mode, actual_entry, actual_size, sl_price,
                                                tp_order_id=tp_id, sl_order_id=sl_id,
                                                entry_intent=actual_entry_intent, exit_intent=exit_intent
                                            )
                                            if decision:
                                                pos['mode'] = decision.mode.value
                                                pos['strategy_id'] = decision.strategy_id
                                                pos['intent'] = actual_entry_intent
                                                pos['tp_profile'] = decision.tp_profile
                                                pos['sl_profile'] = decision.sl_profile
                                                print(f"      📡 [TP/SL KURULDU] TP: {tp_id or 'HATA'} | SL: {sl_id or 'HATA'}")
                                    finally:
                                        self.risk_manager.unlock_symbol(symbol)
                                        asyncio.create_task(self._slow_remove_pending(symbol))

                                elif self.iteration_count % 5 == 0:
                                    market_mode = df.iloc[-1].get('market_mode', 'N/A')
                                    adx = df.iloc[-1].get('adx', 0)
                                    print(f"   👀 [{symbol}] İzleniyor... Mode: {market_mode} | RSI: {rsi:.1f} | ADX: {adx:.1f}")
                                        
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        print(f"   ⚠️  {symbol} fault: {e}")
                
                # 📊 MANAGE OPEN POSITIONS (TP/SL Repair, Layering, Trim)
                # Uses prices collected during symbol scanning above
                collected_prices = {sym: data.get('price', 0) for sym, data in self.latest_metrics.items() if data.get('price', 0) > 0}
                if collected_prices and self.risk_manager.positions:
                    if self.iteration_count % 5 == 0:
                        print(f"   🔧 [POSITION_MGR] {len(self.risk_manager.positions)} pozisyon yönetiliyor ({len(collected_prices)} fiyatla)...")
                    await self.manage_open_positions(collected_prices)
                elif self.iteration_count % 10 == 0 and self.risk_manager.positions:
                    print(f"   ⚠️ [POSITION_MGR] Pozisyon yönetimi atlandı: collected_prices={len(collected_prices)}, positions={len(self.risk_manager.positions)}")
                
                # 🧹 EXECUTE STALE ORDER CLEANUP
                if self.risk_manager.duplicate_ids_to_cancel:
                    print(f"\n🧹 Cleaning up {len(self.risk_manager.duplicate_ids_to_cancel)} duplicate/stale orders...")
                    unique_cancellations = {oid: sym for oid, sym in self.risk_manager.duplicate_ids_to_cancel}
                    for oid, sym in unique_cancellations.items():
                        print(f"   🗑️ Cleaning up {sym} order ID: {oid}")
                        self.exchange_adapter.cancel_order(oid, sym)
                    self.risk_manager.duplicate_ids_to_cancel = []
                
                # Loop timing and sleep
                elapsed = time.time() - start_time
                wait_time = max(0.5, config.CHECK_INTERVAL_SECONDS - elapsed)
                await asyncio.sleep(wait_time)
                
            except KeyboardInterrupt:
                print("\n\n⏸️  Bot durduruluyor... (Ctrl+C algılandı)")
                await self.shutdown()
                break
            except Exception as e:
                print(f"\n❌ Unexpected Fatal Error: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(10) # Cooldown on fatal error

    
    async def run(self):
        """Ana çalıştırma fonksiyonu - Mode'a göre seçim yapar"""
        self.loop = asyncio.get_running_loop() # Capture current loop for thread-safe stopping
        try:
            self.print_status_header()
            
            # REST API bağlantısını başlat (her durumda gerekli)
            if not self.initialize_exchange():
                print("❌ Bot başlatılamadı - Exchange bağlantısı başarısız")
                return

            # WebSocket mu REST mi?
            if config.USE_WEBSOCKET:
                await self.run_websocket_mode()
            else:
                await self.run_rest_mode()
        except Exception as e:
            print(f"💀 CRITICAL BOT FAILURE: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Critical: Ensure lock is released even if it crashes
            await self.shutdown()
    
    # ========================================================================
    # HELPERS
    # ========================================================================
    
    def print_status_header(self):
        """GhostAgent Pro Başlangıç Banner'ı"""
        print("\n" + "="*70)
        print("🚀 GHOSTAGENT EXECUTION PROTOCOL - ADVANCED HFT SCALPING SYSTEM")
        print("="*70)
        
        if config.PAPER_TRADING:
            print("⚠️  SIMULATION MODE ACTIVE - NO REAL CAPITAL RISK")
            print(f"💵 Virtual TVL: ${config.INITIAL_BALANCE_USD:.2f}")
        else:
            print("🔴 GERÇEK PARA MODU - DİKKATLİ OLUN!")
            print("💰 Live Trading Aktif - Gerçek bakiye kullanılacak")
        
        print(f"\n📊 Exchange: {self.exchange_adapter.name}")
        print(f"🔌 Execution Mode: {'WebSocket (Gerçek Zamanlı)' if config.USE_WEBSOCKET else 'REST API (Periyodik)'}")
        
        # Ghost HFT Tiers
        print(f"\n🎯 Architecture: GhostAgent Pro HFT Tiers")
        
        tier1_symbols = config.TIER_PARAMS['tier1']['symbols']
        tier2_symbols = config.TIER_PARAMS['tier2']['symbols']
        tier3_symbols = config.TIER_PARAMS['tier3']['symbols']
        
        print(f"   💎 Tier 1 (Premium): {len(tier1_symbols)} token pairs - Max Volume + Strictest Criteria")
        print(f"       {', '.join(tier1_symbols)}")
        print(f"   ⚡ Tier 2 (Standard): {len(tier2_symbols)} token pairs - Balanced Liquidity")
        print(f"       {', '.join(tier2_symbols)}")
        print(f"   🛡️ Tier 3 (Aggressive): {len(tier3_symbols)} token pairs - Toxic Flow Protected Pools")
        print(f"       {', '.join(tier3_symbols)}")
        
        # Detaylı Strateji Parametreleri
        print(f"\n{'='*70}")
        print("📋 GHOST HFT STRATEGY PARAMETERS")
        print(f"{'='*70}")
        
        # Tier 1 (Premium)
        tier1_params = config.TIER_PARAMS['tier1']
        print(f"\n💎 TIER 1 - PREMIUM:")
        print(f"   • RSI Oversold: < {tier1_params['rsi_oversold']} | Overbought: > {tier1_params['rsi_overbought']}")
        print(f"   • EMA Filter: {'✅ ACTIVE' if tier1_params.get('use_ema_filter') else '❌ KAPALI'}")
        print(f"   • BB Tolerance: %{tier1_params['bb_tolerance']*100:.1f} | Min BB Width: %{tier1_params['min_bb_width']:.2f}")
        print(f"   • Max Spread: {tier1_params['max_spread_bps']}bps")
        print(f"   • TP: %{tier1_params['tp_percent']} | SL: %{tier1_params['sl_percent']}")
        
        # Tier 2 (Standard)
        tier2_params = config.TIER_PARAMS['tier2']
        print(f"\n⚡ TIER 2 - STANDARD:")
        print(f"   • RSI Oversold: < {tier2_params['rsi_oversold']} | Overbought: > {tier2_params['rsi_overbought']}")
        print(f"   • EMA Filter: {'✅ ACTIVE' if tier2_params.get('use_ema_filter') else '❌ KAPALI'}")
        print(f"   • BB Tolerance: %{tier2_params['bb_tolerance']*100:.1f} | Min BB Width: %{tier2_params['min_bb_width']:.2f}")
        print(f"   • Max Spread: {tier2_params['max_spread_bps']}bps")
        print(f"   • TP: %{tier2_params['tp_percent']} | SL: %{tier2_params['sl_percent']}")
        
        # Tier 3 (Aggressive + Protection)
        tier3_params = config.TIER_PARAMS['tier3']
        print(f"\n🛡️ TIER 3 - AGGRESSIVE:")
        print(f"   • RSI Oversold: < {tier3_params['rsi_oversold']} | Overbought: > {tier3_params['rsi_overbought']}")
        print(f"   • EMA Filter: {'✅ ACTIVE' if tier3_params.get('use_ema_filter') else '❌ KAPALI'}")
        print(f"   • BB Tolerance: %{tier3_params['bb_tolerance']*100:.1f} | Min BB Width: %{tier3_params['min_bb_width']:.2f}")
        print(f"   • Max Spread: {tier3_params['max_spread_bps']}bps")
        print(f"   • ⚠️ Momentum Gate: > {tier3_params.get('MIN_MOMENTUM_THRESHOLD', 0)}%")
        print(f"   • ⚠️ Volume Spike Required: {'✅' if tier3_params.get('require_volume_spike') else '❌'}")
        print(f"   • TP: %{tier3_params['tp_percent']} | SL: %{tier3_params['sl_percent']}")
        
        # Risk Yönetimi
        print(f"\n{'='*70}")
        print("🛡️  RISK MANAGEMENT & DYNAMIC DCA SETTINGS")
        print(f"{'='*70}")
        
        # Tier bazlı ortalama değerler
        avg_tp = (tier1_params['tp_percent'] + tier2_params['tp_percent'] + tier3_params['tp_percent']) / 3
        avg_sl = (tier1_params['sl_percent'] + tier2_params['sl_percent'] + tier3_params['sl_percent']) / 3
        print(f"   • Target Profit (TP): Tier 1 %{tier1_params['tp_percent']} | Tier 2 %{tier2_params['tp_percent']} | Tier 3 %{tier3_params['tp_percent']} (Avg: %{avg_tp:.2f})")
        print(f"   • Stop Loss (SL):     Tier 1 %{tier1_params['sl_percent']} | Tier 2 %{tier2_params['sl_percent']} | Tier 3 %{tier3_params['sl_percent']} (Avg: %{avg_sl:.2f})")

        print(f"   • 🛡️ Break-Even: %0.6 profit triggers Break-Even (BE) lock")
        print(f"   • ⏳ Delayed Trailing: %0.5 profit triggers Ghost TTP trailing")
        print(f"   • 🕸️ Correlation Cap: Max unidirectional {getattr(config, 'MAX_SAME_DIRECTION_POSITIONS', 10)} pozisyon")
        print(f"   • 🏗️ Capital Utilization: Unrestricted (%100)")
        print(f"   • 🚨 Circuit Breaker: DISABLED (Recovery Mod)")
        
        # Pozisyon Ayarları
        print(f"\n{'='*70}")
        print("💼 GHOST STRATEGY - POSITION SIZING LOGIC")
        print(f"{'='*70}")
        current_bal = self.risk_manager.get_balance()
        print(f"   • Initial Entry: %{getattr(config, 'GHOST_INITIAL_ENTRY_PCT', 10.0)} (~${current_bal * getattr(config, 'GHOST_INITIAL_ENTRY_PCT', 10.0) / 100:.2f})")
        print(f"   • Tier 2 (Recovery): %{getattr(config, 'GHOST_LAYER_SMALL_PCT', 5.0)}")
        print(f"   • Tier 3+ (Aggressive): %{getattr(config, 'GHOST_LAYER_LARGE_PCT', 10.0)}")
        print(f"   • DCA Distance: %{getattr(config, 'TIER_THRESHOLD_PCT', 2.0)} (Price delta)")
        print(f"   • Leverage Multiplier: {config.LEVERAGE}x")
        print(f"   • Max Concurrent Routs: {config.MAX_OPEN_POSITIONS}")
        print(f"   • Trading Fee: %{config.TRADING_FEE_PERCENT}")
        
        # Kaldıraç Uyarısı
        print(f"\n⚠️  LEVERAGE MISMATCH WARNING:")
        print(f"   GhostAgent enforces logic, not UI leverage!")
        print(f"   Configure leverage manually on Pacifica UI to {config.LEVERAGE}x yapın.")
        print(f"   Must be set identically per token pool!")
        
        print(f"\n{'='*70}")
        print("\n🔄 GhostEngine initiating... (Press Ctrl+C to abort sequence)\n")
    
    async def shutdown(self):
        """Bot'u düzgün şekilde kapat"""
        try:
            self.running = False
            
            print("\n" + "="*70)
            print("🛑 BOT KAPATILIYOR")
            print("="*70)
            
            # WebSocket kapat
            if self.exchange_adapter.ws_active:
                print("📡 WebSocket bağlantısı terminating...")
                await self.exchange_adapter.close_websocket()
            
            # Açık pozisyonları kapat (Paper Trading)
            if config.PAPER_TRADING and self.risk_manager.has_open_positions():
                print("\n⚠️  Açık pozisyonlar algılandı - Kapatılıyor...")
                
                # Son fiyatı al
                df = self.fetch_ohlcv_data(limit=1)
                if df is not None:
                    current_price = df.iloc[-1]['close']
                    for position in list(self.risk_manager.positions):
                        self.risk_manager.close_position(
                            position,
                            current_price,
                            reason="BOT_SHUTDOWN"
                        )
            
            # Özet raporu
            try:
                self.risk_manager.print_summary()
            except Exception as e:
                print(f"⚠️ Summary error: {e}")
            
            print(f"📊 Toplam İterasyon: {self.iteration_count}")
            if config.USE_WEBSOCKET:
                print(f"📡 Fiyat Güncelleme: {self.price_update_count}")
            print(f"⏰ Kapatılma: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as shutdown_err:
            print(f"⚠️ Error during shutdown sequence: {shutdown_err}")
        finally:
            # PID/LOCK Dosyasını TEMİZLE (EN KRİTİK ADIM)
            self._remove_pid_file()
            print("\n✅ Agent safely terminated. Ghost nodes offline. 👋\n")
    
    def get_micro_momentum(self, symbol: str, df: pd.DataFrame) -> float:
        """
        Calculate micro-momentum (last 30 bars price change)
        For tier 3 adverse selection protection
        
        Returns: % change (e.g. -0.15 = -0.15%)
        """
        try:
            if df is None or len(df) < 30:
                return 0.0
            
            price_now = df['close'].iloc[-1]
            price_30_ago = df['close'].iloc[-30]
            
            momentum = ((price_now - price_30_ago) / price_30_ago) * 100
            return momentum
        except Exception as e:
            print(f"⚠️ [{symbol}] Momentum calc error: {e}")
            return 0.0
    
    def check_volume_spike(self, symbol: str, df: pd.DataFrame, threshold: float = 1.5) -> bool:
        """
        Check if recent volume is above average (volume spike detection)
        For tier 3 adverse selection protection
        
        Args:
            threshold: Multiplier (e.g. 1.5 = recent volume must be 1.5x average)
        
        Returns: True if volume spike detected
        """
        try:
            if df is None or len(df) < 100 or 'volume' not in df.columns:
                return False
            
            recent_vol = df['volume'].iloc[-5:].mean()
            avg_vol = df['volume'].iloc[-100:].mean()
            
            if avg_vol == 0:
                return False
            
            return recent_vol >= (avg_vol * threshold)
        except Exception as e:
            print(f"⚠️ [{symbol}] Volume spike check error: {e}")
            return False


    def stop_running(self):
        """Botu nazikçe durdur (Thread-Safe)"""
        print("\n🛑 Bot durdurma sinyali alındı...")
        self.running = False
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._stop_event.set)
        else:
            self._stop_event.set()

    def get_telemetry(self) -> Dict:
        """Dashboard için tüm bot verilerini topla"""
        try:
            stats = self.risk_manager.get_statistics()
            
            # Açık pozisyonları formatla
            pos_list = []
            for p in self.risk_manager.positions:
                symbol = p.get('symbol')
                price = self.latest_metrics.get(symbol, {}).get('price', p.get('entry_price'))
                
                pos_list.append({
                    'symbol': symbol,
                    'side': p.get('direction'),
                    'entry_price': p.get('entry_price'),
                    'size': p.get('position_size', p.get('size')),
                    'unrealized_pnl': p.get('unrealized_pnl', 0),
                    'liq_distance': self.risk_manager.get_liquidation_distance(symbol, price),
                    'entry_intent': p.get('entry_intent', 'N/A'),
                    'opened_at': p.get('opened_at')
                })
            
            return {
                'status': 'Running' if self.running else 'Stopped',
                'uptime': int(time.time() - self.start_time) if self.running else 0,
                'iteration': self.iteration_count,
                'balance': self.risk_manager.get_balance(),
                'stats': stats,
                'positions': pos_list,
                'indicators': self.latest_metrics,
                'recent_trades': self.risk_manager.trade_history[-10:] if self.risk_manager.trade_history else []
            }
        except Exception as e:
            return {'error': str(e)}

    async def close_all_positions(self) -> Dict:
        """Tüm açık pozisyonları piyasa fiyatından kapat"""
        print("\n🚨 [EMERGENCY] Tüm pozisyonlar terminating...")
        results = []
        
        # RiskManager'daki pozisyonları kopyala (iterasyon güvenliği için)
        active_positions = list(self.risk_manager.positions)
        
        for pos in active_positions:
            try:
                symbol = pos['symbol']
                # Güncel fiyatı al
                ticker = await self.exchange_adapter.watch_ticker(symbol)
                price = ticker['last'] if ticker else pos['entry_price']
                
                print(f"   📉 {symbol} terminating... (Fiyat: {price})")
                success = self.risk_manager.close_position(
                    pos, 
                    price, 
                    reason="MANUAL_CLOSE_ALL"
                )
                results.append({'symbol': symbol, 'success': success})
            except Exception as e:
                results.append({'symbol': pos.get('symbol', 'UNK'), 'success': False, 'error': str(e)})
        
        return {'success': True, 'results': results}

    def cancel_all_orders(self) -> Dict:
        """Tüm açık emirleri iptal et"""
        print("\n🚨 [EMERGENCY] Tüm emirler iptal ediliyor...")
        try:
            success = self.exchange_adapter.cancel_all_orders()
            return {'success': success}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    async def manage_open_positions(self, current_prices: Dict[str, float]):
        """
        [ULTRATHINK] CENTRALIZED POSITION MANAGEMENT
        Handles: TP/SL Repair, Fill Control, Layering Trigger, Trim logic, Trailing Stop.
        """
        for position in list(self.risk_manager.positions):
            symbol = position.get('symbol')
            current_price = current_prices.get(symbol)
            if not current_price: continue

            direction = position.get('direction', 'BUY')
            prof_info = self.risk_manager.get_profit_info(current_price, position)
            position['unrealized_pnl'] = prof_info['profit_percent']
            position['profit_usd'] = prof_info['net_pnl']

            # --- 0. DUPLICATE ORDER CLEANUP (Every 10 iterations) ---
            if self.iteration_count % 10 == 0:
                try:
                    open_orders = self.exchange_adapter.fetch_open_orders(symbol)
                    if open_orders and len(open_orders) > 1:
                        # Group orders by price level (with 0.5% tolerance)
                        price_groups = {}
                        for order in open_orders:
                            order_price = float(order.get('price', 0))
                            if order_price <= 0:
                                continue
                            # Find existing group within tolerance
                            matched_group = None
                            for group_price in price_groups:
                                if abs(order_price - group_price) / group_price < 0.005:  # 0.5% tolerance
                                    matched_group = group_price
                                    break
                            if matched_group:
                                price_groups[matched_group].append(order)
                            else:
                                price_groups[order_price] = [order]
                        
                        # Cancel duplicates (keep first, cancel rest)
                        for price_level, orders in price_groups.items():
                            if len(orders) > 1:
                                print(f"   🧹 [{symbol}] DUPLICATE CLEANUP: {len(orders)} emir aynı seviyede (${price_level:.4f}), fazlalıklar iptal ediliyor...")
                                for duplicate_order in orders[1:]:  # Keep first, cancel rest
                                    order_id = duplicate_order.get('id') or duplicate_order.get('order_id')
                                    if order_id:
                                        try:
                                            self.exchange_adapter.cancel_order(order_id, symbol)
                                            print(f"      ❌ Cancelled duplicate order: {order_id}")
                                        except Exception as cancel_err:
                                            print(f"      ⚠️ Cancel error: {cancel_err}")
                except Exception as e:
                    if self.iteration_count % 50 == 0:
                        print(f"   ⚠️ [{symbol}] Duplicate cleanup error: {e}")

            # --- 1. TRAILING STOP UPDATE ---
            self.risk_manager.update_trailing_stops(current_price, symbol)

            # --- 2. TP/SL REPAIR & GHOST CLEANUP ---
            has_tp = bool(position.get('tp_order_id') and position.get('tp_order_id') != '0x00')
            if not has_tp:
                await self._repair_tp_order(symbol, position)

            # 🚨 GHOST STRATEGY: NO SL ON EXCHANGE (Recovery handles risk)
            if self.iteration_count % 5 == 0:
                await self._cleanup_exchange_sl_orders(symbol, position)

            # --- 2b. POSITION ORDER REPAIR (Every 7 iterations) ---
            if self.iteration_count % 7 == 0:
                try:
                    open_orders = self.exchange_adapter.fetch_open_orders(symbol)
                    layers = self.risk_manager.position_layers.get(symbol, [])
                    
                    if len(layers) > 1:
                        # NEW: LAYER-BY-LAYER TRIM (Each layer has its own exit at %0.7 profit)
                        trim_pct = getattr(config, 'MARTINGALE_TRIM_PERCENT', 0.7) / 100
                        
                        for i in range(1, len(layers)):
                            layer = layers[i]
                            layer_entry = layer['price']
                            trim_price = layer_entry * (1 + trim_pct) if direction == 'BUY' else layer_entry * (1 - trim_pct)
                            
                            # Is there an order at this level?
                            has_layer_trim = any(
                                abs(float(o.get('price', 0)) - trim_price) / trim_price < 0.002 
                                for o in open_orders
                            )
                            
                            if not has_layer_trim:
                                trim_side = "sell" if direction == "BUY" else "buy"
                                print(f"   🔧 [REPAIR] Katman {i} için TRIM emri oluşturuluyor: {symbol} @ ${trim_price:.4f} (Büyüklük: {layer['size']:.4f})")
                                oid = self.exchange_adapter.create_limit_order(symbol, trim_side, layer['size'], trim_price)
                                if oid:
                                    self.risk_manager.create_pending_layer_order(symbol, trim_price, 0, f"TRIM_{i}")

                    # 2. TP REPAIR: If TP is missing but should be there (redundant with Section 2, but good for safety)
                    # Section 2 already handles this via await self._repair_tp_order(symbol, position)
                        
                except Exception as repair_err:
                    if self.iteration_count % 35 == 0:
                        print(f"   ⚠️ [{symbol}] Repair cycle warning: {repair_err}")

            # --- 3a. TRIM ORDER FILL DETECTION ---
            pending_orders = self.risk_manager.get_pending_orders(symbol)
            for idx, pending in enumerate(pending_orders):
                if pending.get('type') == 'TRIM':
                    trim_level = pending.get('level', 0)
                    trim_filled = False
                    if direction == 'BUY' and current_price >= trim_level: trim_filled = True
                    elif direction == 'SELL' and current_price <= trim_level: trim_filled = True
                    
                    if trim_filled:
                        print(f"   ✂️ [{symbol}] TRIM DOLDU! Fiyat: ${current_price:.4f} >= Trim: ${trim_level:.4f}")
                        # Pop the last layer (the one that was trimmed)
                        self.risk_manager.pop_layer(symbol)
                        self.risk_manager.clear_pending_order(symbol, idx)
                        
                        # Cancel all orders and place new TP for remaining position
                        try:
                            self.exchange_adapter.cancel_all_orders(symbol)
                        except: pass
                        
                        # Get updated layers info
                        layers = self.risk_manager.position_layers.get(symbol, [])
                        if layers:
                            first_layer_size = layers[0]['size']
                            new_entry = position['entry_price']
                            
                            # New TP for remaining position (entry + 1%)
                            tp_percent = getattr(config, 'GHOST_TP_PCT', 1.0) / 100
                            if direction == 'BUY':
                                tp_price = new_entry * (1 + tp_percent)
                            else:
                                tp_price = new_entry * (1 - tp_percent)
                            
                            try:
                                oid_tp = self.exchange_adapter.create_tp_order(symbol, tp_price, first_layer_size, direction)
                                if oid_tp:
                                    position['tp_order_id'] = oid_tp
                                    print(f"   🎯 [{symbol}] YENİ TP: {first_layer_size:.4f} @ ${tp_price:.4f}")
                            except Exception as e:
                                print(f"   ⚠️ [{symbol}] New TP error: {e}")
                        break

            # --- 3b. PENDING LAYER FILL CONTROL ---
            pending_orders = self.risk_manager.get_pending_orders(symbol)
            for idx, pending in enumerate(pending_orders):
                if pending.get('type') == 'ADD' and not pending.get('placed'):
                    limit_level = pending.get('level', 0)
                    is_filled = False
                    if direction == 'BUY' and current_price <= limit_level: is_filled = True
                    elif direction == 'SELL' and current_price >= limit_level: is_filled = True

                    if is_filled:
                        amount_usd = pending.get('amount_usd', 0)
                        layer_size = amount_usd / current_price if current_price > 0 else 0
                        print(f"   ✅ [{symbol}] TIER DOLDU! Fiyat: ${current_price:.4f} hit Limit: ${limit_level:.4f}")
                        
                        # 1. Add layer to position
                        self.risk_manager.add_layer(symbol, current_price, layer_size)
                        self.risk_manager.clear_pending_order(symbol, idx)
                        
                        # 2. Get updated position info
                        new_avg = position['entry_price']
                        
                        # 3. Cancel all existing orders (will place new specific TRIM)
                        try: self.exchange_adapter.cancel_all_orders(symbol)
                        except: pass
                        
                        # 4. TRIM emri for THIS specific layer at %0.7 profit
                        trim_pct = getattr(config, 'MARTINGALE_TRIM_PERCENT', 0.7) / 100
                        trim_price = current_price * (1 + trim_pct) if direction == 'BUY' else current_price * (1 - trim_pct)
                        
                        trim_side = "sell" if direction == "BUY" else "buy"
                        try:
                            oid_trim = self.exchange_adapter.create_limit_order(symbol, trim_side, layer_size, trim_price)
                            if oid_trim:
                                print(f"   📋 [{symbol}] LAYER TRIM EMRİ: {layer_size:.4f} @ ${trim_price:.4f} (%0.7)")
                        except Exception as e:
                            print(f"   ⚠️ [{symbol}] Trim order error: {e}")
                        
                        # 5. TP emri tazele (Giriş + %1)
                        await self._repair_tp_order(symbol, position)
                        break

            # --- 4. LAYERING TRIGGER (GHOST LOGIC) ---
            layers = self.risk_manager.position_layers.get(symbol, [])
            first_entry_price = layers[0].get('first_entry_price', layers[0]['price']) if layers else position['entry_price']
            
            # [GHOST FIX] Use PRICE DISTANCE from baseline instead of net P&L %
            price_dist_from_baseline = abs(current_price - first_entry_price) / first_entry_price * 100
            
            has_pending_layer = any(o.get('type') == 'ADD' for o in pending_orders)
            
            TRIGGER_PCT = 1.5
            LIMIT_PCT = getattr(config, 'TIER_THRESHOLD_PCT', 2.0)
            
            # [DEBUG] Log every check for layering
            if self.iteration_count % 5 == 0:
                print(f"   🔍 [{symbol}] TIER CHECK: Baseline=${first_entry_price:.4f} | Fiyat=${current_price:.4f} | Mesafe=%{price_dist_from_baseline:.2f} | Trigger=%{TRIGGER_PCT} | Layers={len(layers)} | Pending={has_pending_layer}")
            
            if price_dist_from_baseline >= TRIGGER_PCT and len(layers) <= getattr(config, 'MAX_TIER_COUNT', 3) and not has_pending_layer:
                # Calculate limit price at -2% (or TIER_THRESHOLD_PCT) from FIRST entry
                offset = LIMIT_PCT / 100
                limit_price = first_entry_price * (1 - offset) if direction == 'BUY' else first_entry_price * (1 + offset)
                
                # 🔴 CRITICAL FIX: Check exchange for existing orders at this level
                try:
                    open_orders = self.exchange_adapter.fetch_open_orders(symbol)
                    existing_layer_order = any(
                        abs(float(o.get('price', 0)) - limit_price) / limit_price < 0.005  # %0.5 tolerans
                        for o in open_orders if o.get('price')
                    )
                    if existing_layer_order:
                        print(f"   ⚠️ [{symbol}] TIER ATLANIYOR: Bu seviyede (${limit_price:.4f}) zaten borsa emri var!")
                        continue
                except Exception as e:
                    print(f"   ⚠️ [{symbol}] Open orders check error: {e}")
                
                # Use RiskManager to calc size (it now has looser recovery gates)
                new_layer_size = self.risk_manager.calculate_position_size(limit_price, direction=direction, symbol=symbol)
                
                if new_layer_size > 0:
                    print(f"   📋 [{symbol}] TIER TETIKLENIYOR: %{price_dist_from_baseline:.2f} fiyat mesafesi. Hedef Limit: ${limit_price:.4f} (Size: {new_layer_size:.4f})")
                    try:
                        oid = self.exchange_adapter.create_limit_order(symbol, direction.lower(), new_layer_size, limit_price)
                        if oid:
                            self.risk_manager.create_pending_layer_order(symbol, limit_price, new_layer_size * limit_price, "ADD")
                            print(f"   ✅ [{symbol}] TIER LIMIT EMIR VERILDI: OID={oid}")
                    except Exception as e:
                        print(f"   ❌ [{symbol}] TIER EMIR HATASI: {e}")
                else:
                    # [HELPFUL LOGGING] Why was it rejected?
                    print(f"   ⚠️ [{symbol}] TIER REDDEDILDI: RiskManager size 0 verdi (Bakiye veya Spread engeli?)")

            # --- 5. BREAK-EVEN RESET (GHOST SAFETY) ---
            # Eğer fiyat ortalamaya gelirse (%0.1 kâr) tüm ek katmanları temizle (Initial'a dön)
            if len(layers) > 1 and getattr(config, 'BREAKEVEN_RESET_ENABLED', True):
                reset_buffer = 0.001 # %0.1 profit
                is_at_breakeven = False
                if direction == 'BUY' and current_price >= position['entry_price'] * (1 + reset_buffer): is_at_breakeven = True
                elif direction == 'SELL' and current_price <= position['entry_price'] * (1 - reset_buffer): is_at_breakeven = True
                
                if is_at_breakeven:
                    # Sadece son 5 dakika içinde bir budama yapılmadıysa (spam önleme)
                    last_pop_time = getattr(position, 'last_reset_time', 0)
                    if time.time() - last_pop_time > 300: 
                        print(f"   🔄 [{symbol}] BREAK-EVEN RESET: Fiyat girişe geldi. Ek katmanlar temizleniyor.")
                        added_size = position['position_size'] - layers[0]['size']
                        if added_size > 0:
                            reset_side = "sell" if direction == "BUY" else "buy"
                            # Piyasa fiyatından mermileri boşalt
                            self.exchange_adapter.create_market_order(symbol, reset_side, added_size)
                            # RiskManager'da tüm ek katmanları sil
                            while len(self.risk_manager.position_layers[symbol]) > 1:
                                self.risk_manager.pop_layer(symbol)
                            position['last_reset_time'] = time.time()
                            # Emirleri tazele
                            try: self.exchange_adapter.cancel_all_orders(symbol)
                            except: pass
                            await self._repair_tp_order(symbol, position)

            # --- 6. LOGGING STATUS ---
            if self.iteration_count % 10 == 0:
                print(f"   🔹 [{symbol}] {direction} | Entry: ${position['entry_price']:.2f} | Price: ${current_price:.2f} | P&L: %{prof_info['profit_percent']:.2f} | Layers: {len(layers)}")

    async def _repair_tp_order(self, symbol, position):
        """Helper to find or create a TP order on exchange"""
        try:
            current_orders = self.exchange_adapter.fetch_open_orders(symbol)
            target_side = "sell" if position['direction'] == "BUY" else "buy"
            all_tp = [o for o in current_orders if o.get('side', '').lower() == target_side and 'limit' in o.get('type', '').lower()]
            
            if all_tp:
                best_tp = sorted(all_tp, key=lambda x: str(x.get('id', '')), reverse=True)[0]
                position['tp_order_id'] = best_tp.get('id')
                print(f"   🎯 [{symbol}] Sync: Adopted TP ID {position['tp_order_id']}")
            else:
                tp_price = self.risk_manager.calculate_take_profit_price(position['entry_price'], position['direction'], symbol=symbol, tp_profile=position.get('tp_profile'))
                oid = self.exchange_adapter.create_tp_order(symbol, tp_price, position['position_size'], position['direction'])
                if oid: position['tp_order_id'] = oid
        except Exception as e:
            print(f"   ⚠️ [{symbol}] TP Repair fail: {e}")

    async def _cleanup_exchange_sl_orders(self, symbol, position):
        """GHOST Strategy: Ensure NO SL orders exist on exchange"""
        try:
            current_orders = self.exchange_adapter.fetch_open_orders(symbol)
            target_side = "sell" if position['direction'] == "BUY" else "buy"
            all_sl = [o for o in current_orders if o.get('side', '').lower() == target_side and any(t in o.get('type', '').lower() for t in ['stop', 'trigger', 'conditional'])]
            for sl in all_sl:
                self.exchange_adapter.cancel_order(sl.get('id'), symbol)
                print(f"   🧹 [{symbol}] Removed exchange SL (Ghost Strategy doesn't use standard SL)")
            position['sl_order_id'] = None
        except: pass

async def main():
    """Ana fonksiyon - Async wrapper"""
    # Exchange seçimini config'den al
    bot = None
    try:
        bot = CryptoScalpingBot()
        await bot.run()
    finally:
        if bot:
            bot._remove_pid_file()


if __name__ == "__main__":
    # Async event loop başlat
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot kullanıcı tarafından durduruldu")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
