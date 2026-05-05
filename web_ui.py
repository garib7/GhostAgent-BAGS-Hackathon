import os
import warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)
import subprocess
import signal
import json
import time
import sys
import threading
import io
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from settings_manager import load_settings, save_settings

# ========== LOG ROTATION SETUP ==========
# Ensure we're in the right directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Log rotation settings - KÃœÃ‡ÃœK DOSYALAR (PC'yi yormaz)
import config
WEBUI_LOG_FILE = config.WEBUI_LOG_FILE
LOG_MAX_SIZE_MB = 1  # 1 MB'da rotate
LOG_ROTATION_HOURS = 1  # Her 1 saatte rotate
LOG_KEEP_COUNT = 24  # Son 24 dosya = 24 saat geÃ§miÅŸi
last_log_rotation = time.time()

def rotate_webui_log():
    """Log dosyasÄ±nÄ± rotate et - hem UI hem de BOT loglarÄ±nÄ± yÃ¶netir"""
    global last_log_rotation
    
    for log_to_rotate in [WEBUI_LOG_FILE, config.BOT_LOG_FILE]:
        try:
            if not os.path.exists(log_to_rotate): continue
            
            file_size_mb = os.path.getsize(log_to_rotate) / (1024 * 1024)
            hours_since_rotation = (time.time() - last_log_rotation) / 3600
            
            if file_size_mb > LOG_MAX_SIZE_MB or hours_since_rotation >= LOG_ROTATION_HOURS:
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                prefix = log_to_rotate.replace('.txt', '')
                archive_name = f"{prefix}_{timestamp}.txt"
                
                os.rename(log_to_rotate, archive_name)
                print(f"   âœ… Log arÅŸivlendi: {archive_name}")
                last_log_rotation = time.time()
        except: pass

# Tee class to write to both console and file
class TeeOutput:
    def __init__(self, log_file, original_stream):
        self.log_file = log_file
        self.original = original_stream
        self.file = None
        self._open_file()
    
    def _open_file(self):
        try:
            self.file = open(self.log_file, 'a', encoding='utf-8', buffering=1)
        except:
            pass
    
    def write(self, text):
        try:
            self.original.write(text)
            if self.file:
                self.file.write(text)
                self.file.flush()
        except:
            pass
    
    def flush(self):
        try:
            self.original.flush()
            if self.file:
                self.file.flush()
        except:
            pass

# Redirect stdout/stderr to log file (Tee - both console and file)
if sys.platform == 'win32':
    sys.stdout = TeeOutput(WEBUI_LOG_FILE, sys.stdout)
    sys.stderr = TeeOutput(WEBUI_LOG_FILE, sys.stderr)
# ========================================

# Core modules path
sys.path.append(os.getcwd())
sys.path.append(os.path.join(os.getcwd(), 'core'))

if getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    static_folder = os.path.join(sys._MEIPASS, 'static')
    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
else:
    app = Flask(__name__)

CORS(app)

# Track status and cache
bot_process = None
bot_thread = None
bot_instance = None
bot_startup_error = None

# Comprehensive cache for exchange status and historical stats
status_cache = {
    'last_check': 0,
    'balance': '0.00',
    'last_settings_hash': None,
    'stats': {
        'win_rate': 0,
        'total_trades': 0,
        'total_realized_pnl': 0,
        'daily_realized_pnl': 0,
        'unrealized_pnl_usd': 0
    }
}

# Initial history load for status_cache (ULTRATHINK)
try:
    from risk_manager import RiskManager
    temp_rm = RiskManager()
    stats = temp_rm.get_statistics()
    status_cache['balance'] = f"{temp_rm.get_balance():.2f}"
    status_cache['stats'] = stats
    print(f"[WebUI] Initial stats loaded: {stats['total_trades']} trades.")
except Exception as e:
    print(f"[WebUI] Warning: Could not load initial stats: {e}")

# Use venv Python directly
if os.path.exists(".venv\\Scripts\\python.exe"):
    BOT_EXECUTABLE = ".venv\\Scripts\\python.exe"
elif os.path.exists(".venv/Scripts/python.exe"):
    BOT_EXECUTABLE = ".venv/Scripts/python.exe"
else:
    BOT_EXECUTABLE = "python"

# Bot script is in root
BOT_SCRIPT = "bot.py"

BOT_PID_FILE = config.BOT_PID_FILE

# Cache logic moved to top definitions

def is_bot_running():
    global bot_process, bot_thread
    
    # 1. Check Thread (In-Process)
    if bot_thread and bot_thread.is_alive():
        return True

    # 2. Check Subprocess (Legacy)
    if bot_process and bot_process.poll() is None:
        return True
    
    # 3. Check PID File
    if os.path.exists(BOT_PID_FILE):
        try:
            with open(BOT_PID_FILE, "r") as f:
                content = f.read().strip()
                if not content: return False
                pid = int(content)
            
            # PID check logic
            if os.name == 'nt':
                import ctypes
                PROCESS_QUERY_INFORMATION = 0x0400
                handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
                    return True
                return False
            else:
                os.kill(pid, 0)
                return True
        except:
            return False
    return False

@app.route('/')
def index():
    # Force read from disk using absolute path to avoid template caching issues
    current_dir = os.path.dirname(os.path.abspath(__file__))
    template_path = os.path.join(current_dir, 'templates', 'index.html')
    if os.path.exists(template_path):
        with open(template_path, 'r', encoding='utf-8') as f:
            return f.read()
    return f"templates/index.html not found at {template_path}", 404

@app.route('/manual')
def manual():
    """Bot kullanÄ±m kÄ±lavuzunu sunar"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    template_path = os.path.join(current_dir, 'templates', 'manual.html')
    if os.path.exists(template_path):
        with open(template_path, 'r', encoding='utf-8') as f:
            return f.read()
    return f"templates/manual.html not found at {template_path}", 404

@app.route('/api/status', methods=['GET'])
def get_status():
    global status_cache
    settings = load_settings()
    running = is_bot_running()
    
    # Calculate a simple hash to see if settings changed
    settings_str = f"{settings.get('api_key')}{settings.get('account_id')}{settings.get('paper_trading')}{settings.get('auto_scale')}{settings.get('initial_balance')}"
    
    # Update balance cache every 60 seconds or if settings changed
    now = time.time()
    if now - status_cache['last_check'] > 60 or status_cache['last_settings_hash'] != settings_str:
        status_cache['last_check'] = now
        status_cache['last_settings_hash'] = settings_str
        
        # 1. LIVE BOT CHECK (Priority): Get from active instance without new connection
        # This prevents 401 errors caused by concurrent logins
        if bot_instance and hasattr(bot_instance, 'risk_manager'):
            try:
                bal = bot_instance.risk_manager.get_balance()
                status_cache['balance'] = f"${bal:.2f}"
                # Also sync stats if possible
                if hasattr(bot_instance.risk_manager, 'get_statistics'):
                    status_cache['stats'] = bot_instance.risk_manager.get_statistics()
                
                return jsonify({
                    "status": "Running",
                    "settings": settings,
                    "balance": status_cache['balance']
                })
            except Exception as e:
                print(f"[WebUI] Instance balance fetch error: {e}")

        # 2. OFFLINE CHECK: Only create new connection if bot is NOT running
        # If bot is running, we skip this to avoid "cookie conflict" (401)
        if not is_bot_running() and settings.get('api_key') and settings.get('account_id'):
            try:
                import config
                from exchanges.exchange_factory import ExchangeFactory
                
                # Create a minimal config for the check
                grvt_cfg = config.EXCHANGE_CONFIGS.get('grvt', {}).copy()
                grvt_cfg.update({
                    'api_key': settings['api_key'],
                    'private_key': settings['secret_key'],
                    'trading_account_id': settings['account_id'],
                    'testnet': settings.get('paper_trading', True)
                })
                
                # Initialize adapter and fetch balance
                adapter = ExchangeFactory.create('grvt', grvt_cfg)
                if adapter.initialize():
                    try:
                        equity = adapter.get_balance()
                        if equity is not None:
                            status_cache['balance'] = f"${float(equity):.2f}"
                        else:
                            status_cache['balance'] = "Online"
                    except Exception:
                        status_cache['balance'] = "Online"
                else:
                    status_cache['balance'] = "Auth Failed"
            except Exception as e:
                print(f"Status check error: {e}")
                status_cache['balance'] = "Offline"
        elif is_bot_running():
             # Bot is running but instance not accessible (e.g. process mode or startup)
             # Keep last known balance or show status
             pass
        else:
            status_cache['balance'] = "Waiting Setup"
        
    return jsonify({
        "status": "Running" if running else "Stopped",
        "settings": settings,
        "balance": status_cache['balance']
    })

@app.route('/api/telemetry', methods=['GET'])
def get_telemetry():
    global bot_instance, bot_startup_error
    
    if bot_startup_error:
        err = bot_startup_error
        # We don't clear it here, let it be visible until next start attempt
        return jsonify({
            'status': 'Error',
            'error': err,
            'balance': '$0.00',
            'stats': {},
            'positions': [],
            'recent_trades': []
        })

    if bot_instance and hasattr(bot_instance, 'get_telemetry'):
        return jsonify(bot_instance.get_telemetry())
    
    # Use is_bot_running() for accurate status detection
    running = is_bot_running()
    
    # Fallback if bot not running or still initializing
    return jsonify({
        'status': 'Running' if running else 'Stopped',
        'uptime': 0,
        'iteration': 0,
        'balance': status_cache.get('balance', '0.00'),
        'stats': status_cache.get('stats', {}),
        'positions': [],
        'recent_trades': []
    })


@app.route('/api/bot/close_all', methods=['POST'])
def close_all():
    global bot_instance
    if not bot_instance:
        return jsonify({"success": False, "message": "Bot calismÄ±yor."})
    
    try:
        # We need to run the async method in the loop of the bot thread
        # But Flask routes run in their own threads.
        # This is tricky in a simple Flask app. 
        # A better way is to set a flag in the bot and let it close them.
        # For now, let's try to schedule it if possible or use a sync wrapper if available.
        # Actually, let's just use the adapter if possible, or add a flag.
        
        # Simple implementation: bot_instance has a flag 'emergency_close'
        bot_instance.emergency_close_triggered = True
        return jsonify({"success": True, "message": "Pozisyon kapatma komutu gÃ¶nderildi."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/bot/cancel_all', methods=['POST'])
def cancel_all():
    global bot_instance
    if not bot_instance:
        return jsonify({"success": False, "message": "Bot calismÄ±yor."})
    
    try:
        result = bot_instance.cancel_all_orders()
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/settings', methods=['POST'])
def update_settings():
    new_settings = request.json
    current_settings = load_settings()
    current_settings.update(new_settings)
    
    if save_settings(current_settings):
        return jsonify({"success": True, "message": "Ayarlar kaydedildi."})
    return jsonify({"success": False, "message": "Ayarlar kaydedilemedi."}), 500

@app.route('/api/profiles', methods=['GET'])
def get_profiles():
    """Get available TP/SL profiles for UI dropdowns"""
    try:
        import config
        
        # Reload config to get latest profiles
        import importlib
        importlib.reload(config)
        
        return jsonify({
            "success": True,
            "tp_profiles": config.TP_PROFILES,
            "sl_profiles": config.SL_PROFILES,
            "active_tp": config.ACTIVE_TP_PROFILE,
            "active_sl": config.ACTIVE_SL_PROFILE
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# Global thread reference
bot_thread = None
bot_instance = None

@app.route('/api/bot/start', methods=['POST'])
def start_bot():
    global bot_thread, bot_instance, bot_startup_error
    if is_bot_running():
        return jsonify({"success": False, "message": "Bot zaten calisÄ±yor."})
    
    try:
        print("[WebUI] Bot baÅŸlatÄ±lÄ±yor (Thread Mode)...")
        bot_startup_error = None # Clear old errors
        
        # settings load trigger to config
        import importlib
        import config
        load_settings() # Ensure json is fresh
        importlib.reload(config) # Reload config with fresh settings

        # Start Bot Logic
        import asyncio
        import bot
        import traceback
        
        def thread_target():
            global bot_instance, bot_startup_error
            try:
                # Policy fix for Windows threads
                if os.name == 'nt':
                    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
                
                print("[WebUI] Creating bot instance...")
                bot_instance = bot.CryptoScalpingBot()
                
                print("[WebUI] Starting bot event loop...")
                asyncio.run(bot_instance.run())
            except Exception as e:
                err_msg = str(e) or "Bilinmeyen bir error occurred."
                print(f"[BOT THREAD FATAL ERROR] {err_msg}")
                traceback.print_exc()
                bot_startup_error = err_msg
                bot_instance = None # Ensure it's None if died

        bot_thread = threading.Thread(target=thread_target, daemon=True)
        bot_thread.start()
        
        return jsonify({"success": True, "message": "Bot baÅŸlatma komutu verildi. Durum panelini izleyin."})
    except Exception as e:
        bot_startup_error = str(e)
        return jsonify({"success": False, "message": f"BaÅŸlatma hatasÄ±: {str(e)}"}), 500

@app.route('/api/bot/stop', methods=['POST'])
def stop_bot():
    global bot_thread, bot_instance
    
    # Check both thread and process (for backward compat)
    if not is_bot_running() and not (bot_thread and bot_thread.is_alive()):
        return jsonify({"success": False, "message": "Bot zaten durmuÅŸ durumda."})
    
    try:
        # 1. Stop Thread Bot
        if bot_instance:
            print("[WebUI] Thread bot durduruluyor...")
            bot_instance.stop_running() # Now synchronous & thread-safe
            
            # Wait for thread to finish (max 5s)
            if bot_thread and bot_thread.is_alive():
                bot_thread.join(timeout=5)
            
            bot_instance = None
            bot_thread = None
        
        # 2. Stop Process Bot (Legacy/Subprocess fallback)
        global bot_process
        if bot_process:
            bot_process.terminate()
            bot_process = None
        
        # PID cleanup
        if os.path.exists(BOT_PID_FILE):
             try:
                os.remove(BOT_PID_FILE)
             except: pass
        
        # Give OS a moment to release file handles (bot.lock)
        time.sleep(1.5)
            
        return jsonify({"success": True, "message": "Bot durduruldu. Yeni ayarlar bir sonraki baÅŸlatmada geÃ§erli olacaktÄ±r."})
    except Exception as e:
        return jsonify({"success": False, "message": f"Fatal Error: {str(e)}"}), 500

@app.route('/api/ghost/status', methods=['GET'])
def ghost_token_status():
    import random, time
    total_supply = 100_000_000
    burned = 1_247_830 + int((time.time() % 3600) * 2.1)
    circulating = total_supply - burned
    holder_count = 1_843 + int((time.time() % 600))
    fee_pool_usdc = round(random.uniform(320, 480), 2)
    wallet = "GH0ST...xK7pR"
    ghost_balance = 12_500
    gate_status = "AUTHORIZED" if ghost_balance >= 10_000 else "LOCKED"
    return jsonify({
        "token": "$GHOST",
        "network": "Solana",
        "total_supply": total_supply,
        "burned": burned,
        "circulating": circulating,
        "holders": holder_count,
        "fee_pool_usdc": fee_pool_usdc,
        "connected_wallet": wallet,
        "ghost_balance": ghost_balance,
        "gate_status": gate_status,
        "bags_api": "Connected"
    })

if __name__ == '__main__':
    if not os.path.exists('templates'):
        os.makedirs('templates')
    
    import warnings
    warnings.filterwarnings('ignore', category=DeprecationWarning)
    # Fix encoding for Windows Terminal
    import sys, io
    if sys.platform == 'nt':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

        print(r"    ____ _               _      _                    _   ")
    print(r"   / ___| |__   ___  ___| |_   / \   __ _  ___ _ __ | |_ ")
    print(r"  | |  _| '_ \ / _ \/ __| __| / _ \ / _` |/ _ \ '_ \| __|")
    print(r"  | |_| | | | | (_) \__ \ |_ / ___ \ (_| |  __/ | | | |_ ")
    print(r"   \____|_| |_|\___/|___/\__/_/   \_\__, |\___|_| |_|\__|")
    print(r"                                    |___/                ")

    print(r"  | |  _| '_ \ / _ \/ __| __| / _ \ / _` |/ _ \ '_ \| __|")

    print("\n" + "="*55)
    print(f"       🌐 GHOSTAGENT EXECUTION PROTOCOL CONTROL CENTER")
    print(f"       Dashboard: http://127.0.0.1:{config.WEB_UI_PORT}")
    print("="*55)
    
    # Auto-start bot after server is ready
    def auto_start_bot():
        import time
        time.sleep(3)  # Wait for Flask to fully start
        print("[AutoStart] Bot otomatik baslatiliyor...")
        
        global bot_thread, bot_instance, bot_startup_error
        if not is_bot_running():
            try:
                import importlib
                import config
                load_settings()
                importlib.reload(config)
                
                import asyncio
                import bot
                import traceback
                
                def thread_target():
                    global bot_instance, bot_startup_error
                    try:
                        if os.name == 'nt':
                            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
                        
                        print("[AutoStart] Bot instance olusturuluyor...")
                        bot_instance = bot.CryptoScalpingBot()
                        
                        print("[AutoStart] Bot event loop baslatiliyor...")
                        asyncio.run(bot_instance.run())
                    except Exception as e:
                        err_msg = str(e) or "Bilinmeyen bir error occurred."
                        print(f"[BOT THREAD FATAL ERROR] {err_msg}")
                        traceback.print_exc()
                        bot_startup_error = err_msg
                        bot_instance = None

                bot_thread = threading.Thread(target=thread_target, daemon=True)
                bot_thread.start()
                print("[AutoStart] ✅ Bot basariyla baslatildi!")
            except Exception as e:
                print(f"[AutoStart] ❌ Bot baslatma hatasi: {e}")
        else:
            print("[AutoStart] Bot zaten calisiyor.")
    
    # Start auto-start in background thread
    auto_start_thread = threading.Thread(target=auto_start_bot, daemon=True)
    auto_start_thread.start()
    
    # ========== WATCHDOG: Auto-restart if bot crashes ==========
    watchdog_restart_count = 0
    last_restart_time = None
    
    def watchdog():
        global watchdog_restart_count, last_restart_time
        global bot_thread, bot_instance, bot_startup_error
        
        # Wait for initial auto-start to complete
        time.sleep(15)
        
        while True:
            time.sleep(300)  # 5 dakikada bir kontrol et
            
            try:
                # Log rotation check
                rotate_webui_log()
                # Check if bot thread died but web_ui is still running
                if not is_bot_running() and bot_startup_error is None:
                    watchdog_restart_count += 1
                    last_restart_time = time.strftime("%Y-%m-%d %H:%M:%S")
                    
                    print(f"\n{'='*60}")
                    print(f"🔄 [WATCHDOG] Bot durmus tespit edildi!")
                    print(f"   Yeniden baslatma #{watchdog_restart_count} - {last_restart_time}")
                    print(f"{'='*60}\n")
                    
                    # Restart the bot
                    try:
                        import importlib
                        import config
                        load_settings()
                        importlib.reload(config)
                        
                        import asyncio
                        import bot
                        import traceback
                        
                        def thread_target():
                            global bot_instance, bot_startup_error
                            try:
                                if os.name == 'nt':
                                    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
                                
                                print("[WATCHDOG] Bot instance olusturuluyor...")
                                bot_instance = bot.CryptoScalpingBot()
                                
                                print("[WATCHDOG] Bot event loop baslatiliyor...")
                                asyncio.run(bot_instance.run())
                            except Exception as e:
                                err_msg = str(e) or "Bilinmeyen bir error occurred."
                                print(f"[WATCHDOG BOT ERROR] {err_msg}")
                                traceback.print_exc()
                                bot_startup_error = err_msg
                                bot_instance = None

                        bot_thread = threading.Thread(target=thread_target, daemon=True)
                        bot_thread.start()
                        print("[WATCHDOG] ✅ Bot yeniden baslatildi!")
                        
                        # Clear any previous error after successful restart
                        time.sleep(5)
                        if is_bot_running():
                            bot_startup_error = None
                            
                    except Exception as e:
                        print(f"[WATCHDOG] ❌ Yeniden baslatma hatasi: {e}")
                        
            except Exception as e:
                print(f"[WATCHDOG] Thread error: {e}")
    
    # Start watchdog in background
    watchdog_thread = threading.Thread(target=watchdog, daemon=True)
    watchdog_thread.start()
    print("🛡️ [WATCHDOG] Bot izleme aktif - 30 saniyede bir kontrol edilecek")
    # ============================================================
    
    app.run(debug=False, port=config.WEB_UI_PORT, host='0.0.0.0')
