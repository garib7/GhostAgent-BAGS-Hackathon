"""
Indicators Module: Centralized Technical Analysis calculations.
SOLID compliant: Logic is separated from execution and strategy routing.
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional

def calculate_bollinger_bands(df: pd.DataFrame, period: int = 20, std_dev_mult: float = 2.0) -> Dict[str, pd.Series]:
    """Calculate Bollinger Bands using completed candles (shifted)."""
    middle_band = df['close'].rolling(window=period).mean()
    std_dev = df['close'].rolling(window=period).std()
    upper_band = middle_band + (std_dev * std_dev_mult)
    lower_band = middle_band - (std_dev * std_dev_mult)
    
    return {
        'upper': upper_band,
        'middle': middle_band,
        'lower': lower_band
    }

def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Relative Strength Index (RSI)."""
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    
    is_flat = (avg_gain < 1e-10) & (avg_loss < 1e-10)
    safe_avg_loss = avg_loss.replace(0, 1e-10)
    rs = avg_gain / safe_avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi[is_flat] = 50.0
    
    return rsi.fillna(50)

def calculate_ema(df: pd.DataFrame, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return df['close'].ewm(span=period, adjust=False).mean()

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high = df['high']
    low = df['low']
    close = df['close']
    
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index."""
    high, low, close = df['high'], df['low'], df['close']
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    
    # DM smoothing
    plus_dm_s = plus_dm.ewm(alpha=1/period, adjust=False).mean()
    minus_dm_s = minus_dm.ewm(alpha=1/period, adjust=False).mean()
    
    atr = calculate_atr(df, period)
    plus_di = 100 * (plus_dm_s / atr.replace(0, 1e-10))
    minus_di = 100 * (minus_dm_s / atr.replace(0, 1e-10))
    
    dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, 1e-10))
    return dx.ewm(alpha=1/period, adjust=False).mean()

def calculate_fractals(df: pd.DataFrame, window: int = 2) -> pd.DataFrame:
    """Detect local swing highs/lows (fractals)."""
    df = df.copy()
    df['is_high'] = False
    df['is_low'] = False
    
    for i in range(window, len(df) - window):
        # High fractal
        is_h = True
        for j in range(1, window + 1):
            if df['high'].iloc[i] <= df['high'].iloc[i-j] or df['high'].iloc[i] < df['high'].iloc[i+j]:
                is_h = False
                break
        if is_h:
            df.at[df.index[i], 'is_high'] = True
            
        # Low fractal
        is_l = True
        for j in range(1, window + 1):
            if df['low'].iloc[i] >= df['low'].iloc[i-j] or df['low'].iloc[i] > df['low'].iloc[i+j]:
                is_l = False
                break
        if is_l:
            df.at[df.index[i], 'is_low'] = True
            
    return df

def extract_sr_levels(df: pd.DataFrame, sensitivity: float = 0.005) -> list:
    """Group fractal points into significant S/R levels."""
    highs = df[df['is_high']]['high'].tolist()
    lows = df[df['is_low']]['low'].tolist()
    raw_levels = sorted(highs + lows)
    
    if not raw_levels:
        return []
        
    unique_levels = []
    if raw_levels:
        current_cluster = [raw_levels[0]]
        for i in range(1, len(raw_levels)):
            if raw_levels[i] <= current_cluster[0] * (1 + sensitivity):
                current_cluster.append(raw_levels[i])
            else:
                unique_levels.append(sum(current_cluster) / len(current_cluster))
                current_cluster = [raw_levels[i]]
        unique_levels.append(sum(current_cluster) / len(current_cluster))
        
    return unique_levels

def add_all_indicators(df: pd.DataFrame, config_obj) -> pd.DataFrame:
    """Helper to attach all common indicators to a DataFrame."""
    # BB
    bb = calculate_bollinger_bands(df, config_obj.BB_PERIOD, config_obj.BB_STD)
    df['bb_upper'], df['bb_middle'], df['bb_lower'] = bb['upper'], bb['middle'], bb['lower']
    
    # RSI & EMA
    df['rsi'] = calculate_rsi(df, config_obj.RSI_PERIOD)
    df['ema'] = calculate_ema(df, config_obj.EMA_PERIOD)
    
    # ATR & ADX
    df['atr'] = calculate_atr(df, getattr(config_obj, 'ATR_PERIOD', 14))
    df['adx'] = calculate_adx(df, getattr(config_obj, 'ADX_PERIOD', 14))
    
    return df
