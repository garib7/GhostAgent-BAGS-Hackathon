"""
Configuration file for Crypto Scalping Bot
CRITICAL: PAPER_TRADING is set to True by default to protect capital
"""

# ============================================================================
# BOT IDENTIFICATION (Isolation for Multi-Bot setup)
# ============================================================================
BOT_ID = "GhostAgent"  # Unique ID for this instance
BOT_PID_FILE = f"bot_{BOT_ID}.pid"
BOT_LOCK_FILE = f"bot_{BOT_ID}.lock"
WEB_UI_PORT = 5566  # Changed from 5555 to avoid conflicts

# ============================================================================
# DYNAMIC SETTINGS LOADING
# ============================================================================
try:
    from settings_manager import load_settings
except ImportError:
    try:
        from core.settings_manager import load_settings
    except ImportError:
        def load_settings(): return {}

user_settings = load_settings()

# ============================================================================
# SAFETY SETTINGS - READ CAREFULLY BEFORE CHANGING
# ============================================================================

# PAPER_TRADING: When True, the bot will simulate trades without using real money
PAPER_TRADING = user_settings.get('paper_trading', False)  # MAINNET MODE - GERÇEK PARA 

# ============================================================================
# MULTI-EXCHANGE CONFIGURATION
# ============================================================================

# Aktif exchange ("grvt", "pacifica", "reya", "paradex")
ACTIVE_EXCHANGE = user_settings.get('exchange', "solana") 

# Exchange-specific configurations
EXCHANGE_CONFIGS = {
    "solana": {
        "api_key": "4DtyLwP3GUPjD4updx4QMdr4XZtN8QUNt8kbcd4svGhG",
        "api_secret": "BNU2jJcdPE7pxttsQu7kNzKQKLFVB2chAWsG3p6PUJCg",
        "testnet": True,
        "fee_percent": 0.01,
        "leverage": 1
    },
    "grvt": {
        "api_key": user_settings.get('api_key', "REDACTED - USE settings.json"),
        "private_key": user_settings.get('secret_key', "REDACTED - USE settings.json"),
        "trading_account_id": user_settings.get('account_id', "REDACTED - USE settings.json"),
        "testnet": True,  # TESTNET MODE
        "fee_percent": 0.05,
        "leverage": user_settings.get('leverage', 25),
    },
    "pacifica": {
        "api_key": "REDACTED - USE settings.json",
        "api_secret": "REDACTED - USE settings.json",
        "testnet": True,
        "fee_percent": 0.08,
    },
    "reya": {
        "api_key": "REDACTED - USE settings.json",
        "api_secret": "REDACTED - USE settings.json",
        "testnet": True,
        "fee_percent": 0.05,  # Reya: Low latency, gasless
    },
    "paradex": {
        "api_key": "REDACTED - USE settings.json",
        "api_secret": "REDACTED - USE settings.json",
        "testnet": True,
        "fee_percent": 0.1,  # Paradex: StarkWare tech
    },
}

# ============================================================================
# SPREAD GATE THRESHOLDS (Production Risk Control)
# ============================================================================
MAX_SPREAD_BPS_QUALITY = 5.0    # 0.05% max spread for QUALITY layer
MAX_SPREAD_BPS_VOLUME = 10.0    # 0.10% max spread for VOLUME layer
SLIPPAGE_BUFFER_BPS = 2.0       # 0.02% safety buffer

# Volume layer time-stop (DISABLED per user request - unlimited hold time)
# MAX_HOLD_TIME_VOLUME = 120      # 120 seconds max hold for volume trades


# ============================================================================
# BOT BEHAVIOR SETTINGS
# ============================================================================

# Aktif exchange için testnet ayarı
TESTNET = EXCHANGE_CONFIGS.get(ACTIVE_EXCHANGE, {}).get("testnet", False)

# WebSocket vs REST API Mode
USE_WEBSOCKET = False  # REST API - Daha güvenilir, hemen çalışır

# REST API için check interval (saniye)
CHECK_INTERVAL_SECONDS = 1  # Her saniye kontrol (Maksimum HFT hızı)

# ============================================================================
# TRADING PARAMETERS
# ============================================================================

# --- RISK SAFETY LIMITS (ULTRATHINK) ---
INITIAL_BALANCE_USD = float(user_settings.get('initial_balance', 100.0))  # Reference for scaling
GHOST_AUTO_SCALE_ENABLED = user_settings.get('auto_scale', False) # Dynamic scaling toggle
SAFETY_MAX_POSITION_USD_MULTIPLIER = 1.25 # Reject positions > (MAX_TRADE_SIZE * LEVERAGE * 1.25)
HARD_SAFETY_USD_MARGIN_MAX = 500.0  # UNIVERSAL CEILING: No trade can ever use more than $500 margin

# --- LIQUIDITY GATE ---
MIN_LIQUIDITY_USD = 1000.0   # Minimum liquidity required in depth window
LIQUIDITY_DEPTH_PCT = 0.5    # Window % from mid price to check liquidity
MAX_TRADE_SIZE_USD = float(user_settings.get('margin_per_trade', 5.0))       # User setting
MIN_TRADE_SIZE_USD = 5.0        # Minimum margin
MIN_TRADE_SIZE_COIN = 0.001     # Safety floor for precision (FIX BUG #Q06)

# Leverage configuration
LEVERAGE = user_settings.get('leverage', 10.0)                    # Kullanıcı ayarı (Standart: 10x)
# Position size = MARGIN × LEVERAGE
# Example: $10 margin × 25x = $250 position size

# Symbol-specific MARGIN sizes (actual position = margin × leverage)
# These are MARGIN amounts, NOT position sizes!
# Symbol-specific MARGIN sizes (actual position = margin × leverage)
# These are MARGIN amounts, NOT position sizes!
SYMBOL_TRADE_SIZES_USD = {
    "BTC/USDT": 10.0,
    "ETH/USDT": 10.0,
    "SOL/USDT": 10.0,
    "XRP/USDT": 10.0,
    "LINK/USDT": 10.0,
    "AVAX/USDT": 10.0,
}

# Risk management  
STOP_LOSS_PERCENT = 1.5     # Hard stop-loss at 1.5% loss (increased for crypto volatility)

# ========================================================================
# FEE STRUCTURE (GRVT Maker/Taker) - FIXED!
# ========================================================================
MAKER_FEE_PERCENT = -0.0004    # -0.0004% maker REBATE (you get paid!)
TAKER_FEE_PERCENT = 0.042      # 0.042% taker fee
TRADING_FEE_PERCENT = TAKER_FEE_PERCENT  # Legacy compat

# Default intent per mode (OPTIMIZED)
DEFAULT_ORDER_INTENT_PROFIT = "MAKER"    # ProfitCore: Use maker for rebates
DEFAULT_ORDER_INTENT_VOLUME = "MAKER"    # VolumeSidecar: Maker always better

# Fee-aware minimum move requirements (OPTIMIZED)
MIN_TP_DISTANCE_MAKER_PCT = 0.08   # 0.08% TP (net +0.0808% after rebate)
MIN_TP_DISTANCE_TAKER_PCT = 0.15   # 0.15% TP (net +0.066% after 0.084% fee)

# ============================================================================
# TWO-LAYER STRATEGY CONFIGURATION
# ============================================================================

# İki katmanlı mod (False yaparsanız eski tek strateji kullanılır)
LAYER_MODE = True

# KATMAN 1: Quality Hunter (Yüksek Kar Odaklı - Az İşlem, Yüksek Kar)
QUALITY_SYMBOLS = [
    "BTC/USDT",   # En büyük, en güvenilir, dar spread
    "ETH/USDT",   # İkinci en büyük, stabil
    "SOL/USDT",   # Yüksek volatilite, iyi fırsat
]

# KATMAN 2: Volume Hunter (Çok İşlem Odaklı - Çok İşlem, Orta Kar)
VOLUME_SYMBOLS = [
    "XRP/USDT", "ARB/USDT", "OP/USDT",
    "ATOM/USDT", "POL/USDT", "TON/USDT",
    "LINK/USDT", "AVAX/USDT", "JUP/USDT",
    "SUI/USDT", "AAVE/USDT", "UNI/USDT",
]
# Add more symbols as needed

# Trading pairs - Multi-symbol support (Toplam 13 token pairs)
# CRITICAL FIX: user_settings'i sadece LAYER_MODE kapalıysa kullan
if LAYER_MODE:
    SYMBOLS = QUALITY_SYMBOLS + VOLUME_SYMBOLS
else:
    SYMBOLS = user_settings.get('symbols', [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"
    ])
SYMBOL = SYMBOLS[0]  # Deprecated - backward compatibility
TIMEFRAME = "1m"        # 1m execution timeframe
TREND_TIMEFRAME = "15m"  # 15m trend timeframe
MIN_HOLD_TIME_SECONDS = 5


# ============================================================================
# HIGH-FREQUENCY SCALPING PARAMETERS (YÜKSEK FREKANS AYARLARI)
# ============================================================================

# ========================================================================
# OPTIMIZED PROFIT TARGETS (SAFE & SUSTAINABLE)
# ========================================================================
AGGRESSIVE_MODE = user_settings.get('aggressive_mode', False)
BB_TOLERANCE_PERCENT = 0.08  # Tighter entry
MIN_PROFIT_PERCENT = 0.40
TARGET_PROFIT_PERCENT = 1.0  # Increased for sustainability

# BALANCED STOPS (Improved math)
MIN_STOP_LOSS_FLOOR = 1.5     # 1.5% SL
MIN_PROFIT_TARGET_FLOOR = 0.80

# Strategy exit minimum
STRATEGY_EXIT_MIN_PROFIT = 0.40
STOP_LOSS_PERCENT = 12.0       # Global SL %12.0 (Likidasyon koruması)

# ============================================================================
# TP/SL RISK PROFİLLERİ (Optimized for profit)
# ============================================================================

# TP (Kar Hedefi) Profilleri
TP_PROFILES = {
    "BALANCED": {
        "percent": 1.0,
        "net_profit": 0.90,
        "description": "Standard kâr hedefi. Risk/Ödül dengeli.",
        "risk_level": "✅",
        "current": True  
    },
    "IDEAL": {
        "percent": 1.5,
        "net_profit": 1.40,
        "description": "En iyi risk/ödül oranı.",
        "risk_level": "⭐ İDEAL",
        "recommended": True
    }
}

# SL (Zarar Durdurma) Profilleri
SL_PROFILES = {
    "STANDARD": {
        "percent": 1.5,
        "description": "Kripto volatilitesine uygun dengeli stop.",
        "risk_level": "✅",
        "current": True  
    },
    "WIDE": {
        "percent": 2.0,
        "description": "Geniş stop. Az stop edilme, daha fazla hareket alanı.",
        "risk_level": "💰",
        "ideal_for": "Yüksek risk toleransı"
    }
}

# Aktif profiller (settings.json'dan override edilebilir)
ACTIVE_TP_PROFILE = user_settings.get('tp_profile', 'BALANCED')
ACTIVE_SL_PROFILE = user_settings.get('sl_profile', 'BALANCED')

# Position confirmation and retry settings (FIX BUG #26 - Magic numbers)
POSITION_CONFIRM_WAIT_SECONDS = 10  # Wait time for exchange to register position (Increased for safety)
POSITION_RETRY_WAIT_SECONDS = 2     # Additional wait time on retry
MAX_POSITION_SYNC_RETRIES = 3       # How many times to retry position sync before fallback
TRADE_COOLDOWN_SECONDS = 180        # Cooldown between trades on same symbol (3 minutes)

# STABILITY & ANTI-FEE-BURN SETTINGS
MIN_BB_WIDTH_PERCENT = 0.60         # Min %0.60 band width required to trade (Volatility Filter - Daha kaliteli sinyaller)
# MIN_HOLD_TIME_SECONDS moved up to behavior settings
SIGNAL_CONFIRMATION_COUNT = 2       # Number of checks to confirm a signal

# Error handling settings (FIX BUG #28 - Circuit breaker)
MAX_CONSECUTIVE_ERRORS = 5          # Max errors before shutting down
ERROR_BACKOFF_SECONDS = 5           # Base backoff time (exponential)

# ============================================================================
# TECHNICAL INDICATOR PARAMETERS (Dengeli Ayarlar)
# ============================================================================

# RSI (Gevşetilmiş eşikler - daha fazla sinyal)
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70         
RSI_OVERSOLD = 30           

# Bollinger Bands (Standart sapma artırıldı)
BB_PERIOD = 20
BB_STD = 2.2  # 2.5 → 2.2 (Daha sık sinyal için daraltıldı)                # 1.5 -> 2.0 (Gürültüyü engelle)

# EMA (Hızlı scalping için kısa periyotlar)
EMA_PERIOD = 50
EMA_FAST = 9
EMA_SLOW = 21

# HMR Engine Indicators
ADX_PERIOD = 14
ADX_THRESHOLD = 25  # Strength to switch from SCALPING to MOMENTUM
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 3.5  # 2.5 -> 3.5 (Asimetrik risk yönetimi için genişletildi)
ATR_TP_MULTIPLIER = 3.5  # Hedef kar ATR bazlı korundu

# Trailing Stop settings
TRAILING_STOP_ENABLED = True
TRAILING_STOP_CALLBACK = 0.3  # %0.5 -> %0.3 (Daha sıkı takip, karı kaçırma)

# Volume (0 = devre dışı)
VOLUME_THRESHOLD_MULTIPLIER = 1.2  # Volume divergence check active

# ============================================================================
# RISK LIMITS
# ============================================================================

# Maximum positions & daily limits
MAX_OPEN_POSITIONS = 15              # Total positions
MAX_SAME_DIRECTION_POSITIONS = 10    # Same direction (LONG or SHORT)
MAX_ACCOUNT_EXPOSURE_PERCENT = 100.0 # Ghost Stratejisi: Kasa kısıtlaması yok
MAX_DAILY_TRADES = 1000               
MAX_DAILY_LOSS_PERCENT = 100.0       # Ghost Stratejisi: Günlük stop yok (Recovery mod)

# --- STRATEGY RISK MANAGEMENT (FIXED USD MODE) ---
GHOST_INITIAL_MARGIN_USD = 10.0       # Initial entry margin
GHOST_LAYER_1_MARGIN_USD = 10.0       # Layer 1 margin
GHOST_LAYER_2_MARGIN_USD = 10.0       # Layer 2 margin
GHOST_LAYER_3_MARGIN_USD = 10.0       # Layer 3 margin

GHOST_TP_PCT = 1.5          # Take Profit target
GHOST_SL_PCT = 5.0          # Hard Stop Loss
USE_GHOST_TREND_FILTER = True # Enable trend check
SR_WINDOW = 5              # Candle window for fractals
SR_SENSITIVITY = 0.005     # Sensitivity
TIER_THRESHOLD_PCT = 3.0        # DCA trigger distance
MAX_TIER_COUNT = 3       # Max DCA layers
RSI_OVERSOLD = 30          # Oversold threshold
RSI_OVERBOUGHT = 70

# --- NEW: STRATEGY MODES & MARTINGALE TRIM ---
ACTIVE_STRATEGY_MODE = user_settings.get('active_strategy_mode', 'ECONOMIC') # 'ECONOMIC', 'MARTINGALE', 'CUSTOM'
STRATEGY_MODES = {
    'ECONOMIC': [10.0, 10.0, 10.0, 10.0],
    'MARTINGALE': [10.0, 15.0, 25.0, 40.0],
    'CUSTOM': user_settings.get('custom_layers', [10.0, 10.0, 10.0, 10.0])
}

# Current layer setup from active mode
CURRENT_STRATEGY_LAYERS = STRATEGY_MODES.get(ACTIVE_STRATEGY_MODE, STRATEGY_MODES['ECONOMIC'])

# Martingale Trim & Reset settings
MARTINGALE_TRIM_ENABLED = True
MARTINGALE_TRIM_PERCENT = 0.7  # %0.7 kar alınca o katmanı buda
BREAKEVEN_RESET_ENABLED = True  # Budamalardan sonra ortalamaya gelince eklemeleri temizle

# ============================================================================
# MTF RSI CONFIRMATION (Multi-Timeframe RSI Filter)
# ============================================================================
USE_MTF_RSI_CONFIRMATION = True
MTF_TIMEFRAME = "5m"

# Long entry: 1m RSI < threshold AND 5m RSI < threshold
MTF_RSI_LONG_1M = 30
MTF_RSI_LONG_5M = 40

# Short entry: 1m RSI > threshold AND 5m RSI > threshold
MTF_RSI_SHORT_1M = 70
MTF_RSI_SHORT_5M = 60

MIN_TRADE_SIZE_USD = 5.0             # Borsa alt limit koruması

# Emergency stop
EMERGENCY_STOP_LOSS_PERCENT = 10.0  # Total portfolio için acil durdurma: %10 kayıp

# ========================================================================
# GUARDRAILS (Volume Sidecar Protection)
# ========================================================================
VOLUME_GUARDRAIL_ENABLED = True
VOLUME_GUARDRAIL_EQUITY_DD_CUT_PCT = 10.0    # 10% DD kill (user wants loose)
VOLUME_GUARDRAIL_DAILY_PNL_MIN = 0.0         # No daily limit (user wants to continue)
VOLUME_GUARDRAIL_ROLLING_WINDOW = 30         # 30 trades window
VOLUME_GUARDRAIL_ROLLING_PNL_MIN = 0.0       # No rolling limit
VOLUME_GUARDRAIL_COOLDOWN_MINUTES = 60       # 60min cooldown

# Throttle seviyeleri (0=normal, 1=yarı, 2=durdur)
VOLUME_THROTTLE_LEVELS = {
    0: 1.0,    # Normal
    1: 0.5,    # Yarı boyut
    2: 0.0     # Durdur
}

# Mode-specific exposure budgets (Unlimited for Ghost Strategy)
PROFIT_CORE_MAX_EXPOSURE_PCT = 100.0   
VOLUME_SIDECAR_MAX_EXPOSURE_PCT = 100.0

# ============================================================================
# LOGGING & REPORTING
# ============================================================================

VERBOSE_LOGGING = True  # Detaylı log
ENABLE_CONSOLE_OUTPUT = True  # Console'da göster
SAVE_TRADES_TO_FILE = True
TRADE_LOG_FILE = f"trades_{BOT_ID}.csv"
BOT_LOG_FILE = f"bot_{BOT_ID}_log.txt"
WEBUI_LOG_FILE = f"webui_{BOT_ID}_log.txt"

# ============================================================================
# MEMORY MANAGEMENT
# ============================================================================

# Hafıza temizliği ayarları (Açık pozisyonlar asla silinmez!)
CLEANUP_INTERVAL_HOURS = 0.5        # Her 30 dakikada bir temizlik (0.5 saat)
KEEP_TRADE_HISTORY_COUNT = 50       # Bellekte son 50 işlem tutulur (100'den düşürüldü)
ARCHIVE_TRADES_TO_FILE = True       # Eski işlemler CSV'ye arşivlenir
TRADE_ARCHIVE_FILE = f"trades_{BOT_ID}_archive.csv"

# Bot Auto-Restart (Hafıza optimizasyonu için)
AUTO_RESTART_ENABLED = True         # Otomatik yeniden başlatma aktif
AUTO_RESTART_HOURS = 2.0            # Her 2 saatte bir yeniden başlat
# Pozisyonlar restart sırasında KAPATILMAZ - Yeniden başlatmada sync edilir

# ============================================================================
# LAYER-BASED STRATEGY PARAMETERS
# ============================================================================

# Katman bazlı strateji parametreleri
LAYER_PARAMS = {
    "QUALITY": {
        "rsi_oversold": 25,       
        "rsi_overbought": 72,     
        "use_ema_filter": True,   # ZORUNLU: Ana trend tersine işlem yasak
        "bb_tolerance": 0.10,     
        "min_bb_width": 0.80,     
        "tp_percent": 1.2,        
        "sl_percent": 2.0,        # ATR 3.5'e denk gelir
        "description": "Safe-Turbo: Yüksek volatilite, yüksek kalite"
    },
    "VOLUME": {
        "rsi_oversold": 30,       
        "rsi_overbought": 68,     
        "use_ema_filter": True,   # ZORUNLU
        "bb_tolerance": 0.15,     
        "min_bb_width": 0.60,     
        "tp_percent": 0.8,        
        "sl_percent": 1.8,        
        "description": "Safe-Turbo: Dengeli hacim",
        # Soft time-stop (scalp karakterini koru)
        "SOFT_TIME_STOP_MINUTES": 15,  # 15 dakika sonra scratch exit dene
        "SCRATCH_EXIT_MODE": True,      # BE+rebate ile pasif çıkmayı dene
        "SCRATCH_TIMEOUT_SEC": 30,      # Scratch dolmazsa 30 sn sonra market
    }

}

# ============================================================================
# TIERED QUALITY SYSTEM (New - Replaces layer logic)
# ============================================================================

TIER_PARAMS = {
    'tier1': {
        'name': 'GHOST Professional Trading',
        'symbols': ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'LINK/USDT', 'AVAX/USDT', 'SUI/USDT', 'AAVE/USDT', 'UNI/USDT', 'OP/USDT', 'ARB/USDT', 'TON/USDT', 'JUP/USDT'],
        
        # Universal parameters for all symbols
        'rsi_oversold': 30,
        'rsi_overbought': 70,
        'RSI_PERIOD': 14,
        
        'BB_PERIOD': 20,
        'BB_STD_DEV': 2.2,
        'min_bb_width': 0.70,
        'bb_tolerance': 0.12,
        
        'EMA_SHORT': 50,
        'EMA_LONG': 200,
        'ADX_PERIOD': 14,
        'ADX_THRESHOLD': 25,
        
        'max_spread_bps': 10,
        'MAX_ATR_PERCENT': 5.0,
        
        # GHOST Layering Strategy - Global (TP %1, SL %8)
        'tp_percent': 1.0,
        'sl_percent': 8.0,
        'TRAILING_STOP_ACTIVATION': 0.6,
        'TRAILING_STOP_CALLBACK': 0.3,
        
        # Risk yönetimi
        'max_exposure_percent': 100,
        'MAX_POSITIONS_PER_DIRECTION': 15,
        
        # Order intent
        'order_intent': 'TAKER',
        'MAKER_FILL_WAIT': 3.0,
        
        # Stratejiler
        'STRATEGIES': ['LiquiditySweepFade', 'MomentumBreakout'],
        
        # Filters
        'use_ema_filter': True,
        'MIN_MOMENTUM_THRESHOLD': None,
        'require_volume_spike': False,
        'description': 'GHOST Professional: Single-tier, global config. TP %1 SL %8 for 4-layer recovery'
    },
    
    'tier2': {
        'name': 'GHOST Professional Trading (Fallback)',
        'symbols': [],
        
        # Same as tier1
        'rsi_oversold': 25,
        'rsi_overbought': 70,
        'RSI_PERIOD': 14,
        
        'BB_PERIOD': 20,
        'BB_STD_DEV': 2.2,
        'min_bb_width': 0.70,
        'bb_tolerance': 0.12,
        
        'EMA_SHORT': 50,
        'EMA_LONG': 200,
        'ADX_PERIOD': 14,
        'ADX_THRESHOLD': 25,
        
        'max_spread_bps': 10,
        'MAX_ATR_PERCENT': 5.0,
        
        'tp_percent': 1.0,
        'sl_percent': 8.0,
        'TRAILING_STOP_ACTIVATION': 0.6,
        'TRAILING_STOP_CALLBACK': 0.3,
        
        'max_exposure_percent': 100,
        'MAX_POSITIONS_PER_DIRECTION': 15,
        
        'order_intent': 'TAKER',
        'MAKER_FILL_WAIT': 3.0,
        
        'STRATEGIES': ['LiquiditySweepFade', 'MomentumBreakout'],
        
        'use_ema_filter': True,
        'MIN_MOMENTUM_THRESHOLD': None,
        'require_volume_spike': False,
        'description': 'GHOST Professional: Identical to tier1'
    },
    
    'tier3': {
        'name': 'GHOST Professional Trading (Fallback)',
        'symbols': [],
        
        # Same as tier1
        'rsi_oversold': 25,
        'rsi_overbought': 70,
        'RSI_PERIOD': 14,
        
        'BB_PERIOD': 20,
        'BB_STD_DEV': 2.2,
        'min_bb_width': 0.70,
        'bb_tolerance': 0.12,
        
        'EMA_SHORT': 50,
        'EMA_LONG': 200,
        'ADX_PERIOD': 14,
        'ADX_THRESHOLD': 25,
        
        'max_spread_bps': 10,
        'MAX_ATR_PERCENT': 5.0,
        
        'tp_percent': 1.0,
        'sl_percent': 8.0,
        'TRAILING_STOP_ACTIVATION': 0.6,
        'TRAILING_STOP_CALLBACK': 0.3,
        
        'max_exposure_percent': 100,
        'MAX_POSITIONS_PER_DIRECTION': 15,
        
        'order_intent': 'TAKER',
        'MAKER_FILL_WAIT': 3.0,
        
        'STRATEGIES': ['LiquiditySweepFade', 'MomentumBreakout'],
        
        'use_ema_filter': True,
        'MIN_MOMENTUM_THRESHOLD': None,
        'require_volume_spike': False,
        'description': 'GHOST Professional: Identical to tier1'
    }
}

def get_tier_for_symbol(symbol: str) -> str:
    """Determine which tier a symbol belongs to"""
    for tier_name, tier_config in TIER_PARAMS.items():
        if symbol in tier_config['symbols']:
            return tier_name
    return 'tier2'  # Default fallback

def get_tier_params(tier: str) -> dict:
    """Get tier parameters"""
    return TIER_PARAMS.get(tier, TIER_PARAMS['tier2'])

def get_all_active_symbols() -> list:
    """Get all active symbols (from all tiers)"""
    all_symbols = []
    for tier_config in TIER_PARAMS.values():
        all_symbols.extend(tier_config['symbols'])
    return all_symbols

def validate_tier_config():
    """Validate TIER_PARAMS consistency"""
    all_symbols = []
    for tier_name, tier_config in TIER_PARAMS.items():
        # Check for symbol duplicates
        for symbol in tier_config['symbols']:
            if symbol in all_symbols:
                raise ValueError(f"Symbol {symbol} duplicate in {tier_name}")
            all_symbols.append(symbol)
        
        # Check required fields
        required_fields = [
            'rsi_oversold', 'rsi_overbought', 'tp_percent', 'sl_percent',
            'max_spread_bps', 'max_exposure_percent'
        ]
        for field in required_fields:
            if field not in tier_config:
                raise ValueError(f"Missing {field} in {tier_name}")
    
    print(f"OK: Tier config validated: {len(all_symbols)} symbols across {len(TIER_PARAMS)} tiers")

# Startup validation
try:
    validate_tier_config()
except Exception as e:
    print(f"FAIL: Tier config validation failed: {e}")
    raise

# All symbols (union of all tiers)
ALL_TIER_SYMBOLS = get_all_active_symbols()



def get_layer_for_symbol(symbol: str) -> str:
    """Symbol'ün hangi katmanda olduğunu döndür"""
    if symbol in QUALITY_SYMBOLS:
        return "QUALITY"
    elif symbol in VOLUME_SYMBOLS:
        return "VOLUME"
    return "QUALITY"  # Default: Quality (güvenli mod)

def get_layer_params(symbol: str) -> dict:
    """Symbol için layer parametrelerini al"""
    if not LAYER_MODE:
        # Eski mod: Herkes için aynı parametreler (backward compatibility)
        return {
            "rsi_oversold": RSI_OVERSOLD,
            "rsi_overbought": RSI_OVERBOUGHT,
            "use_ema_filter": not AGGRESSIVE_MODE,
            "bb_tolerance": BB_TOLERANCE_PERCENT / 100,
            "min_bb_width": MIN_BB_WIDTH_PERCENT,
        }
    
    layer = get_layer_for_symbol(symbol)
    return LAYER_PARAMS[layer]

# ============================================================================
# HOW TO SWITCH TO REAL MONEY MODE
# ============================================================================
# 1. Set PAPER_TRADING = False
# 2. Update API keys in EXCHANGE_CONFIGS
# 3. Set TESTNET = False for your exchange
# 4. Start with small MAX_TRADE_SIZE_USD
# 5. Monitor closely for the first few trades
# ============================================================================

