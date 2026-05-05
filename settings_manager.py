"""
Settings Manager - JSON dosyasından ayarları yükler ve kaydeder.
"""
import json
import os

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

DEFAULT_SETTINGS = {
    "api_key": "",
    "secret_key": "",
    "account_id": "",
    "paper_trading": True,
    "exchange": "grvt",
    "leverage": 25,
    "margin_per_trade": 5.0,
    "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    "aggressive_mode": False,
    "tp_profile": "BALANCED",
    "sl_profile": "BALANCED",
    "auto_scale": False,
    "initial_balance": 100.0
}

def load_settings() -> dict:
    """Ayarları settings.json dosyasından yükler."""
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
                # Eksik anahtarları varsayılan değerlerle doldur
                for key, value in DEFAULT_SETTINGS.items():
                    if key not in settings:
                        settings[key] = value
                return settings
    except Exception as e:
        print(f"[SettingsManager] Ayarlar yüklenirken hata: {e}")
    
    return DEFAULT_SETTINGS.copy()

def save_settings(settings: dict) -> bool:
    """Ayarları settings.json dosyasına kaydeder."""
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"[SettingsManager] Ayarlar kaydedilirken hata: {e}")
        return False
