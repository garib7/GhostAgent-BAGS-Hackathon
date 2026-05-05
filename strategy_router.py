"""
Strategy Router: Routes symbols to their specific strategies and modes.
Fulfills SOLID: Open/Closed principle (new strategies can be added as classes).
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol
import pandas as pd
import indicators
import config

class StrategyMode(Enum):
    PROFIT_CORE = "PROFIT_CORE"
    VOLUME_SIDECAR = "VOLUME_SIDECAR"

class OrderIntent(Enum):
    MAKER = "MAKER"
    TAKER = "TAKER"

class TradeAction(Enum):
    BUY = "BUY"
    SELL = "SELL"
    NONE = "NONE"

@dataclass
class StrategyDecision:
    action: TradeAction
    symbol: str
    strategy_id: str
    mode: StrategyMode
    order_intent: OrderIntent
    tp_profile: Dict = field(default_factory=dict)
    sl_profile: Dict = field(default_factory=dict)
    reason: str = ""

class BaseSubStrategy(Protocol):
    def generate_decision(self, df: pd.DataFrame, symbol: str, trend_df: pd.DataFrame = None) -> StrategyDecision:
        ...

# --- PROFIT CORE STRATEGIES ---

class LiquiditySweepFade:
    """Reversion strategy focusing on liquidity sweeps of BB bands."""
    def generate_decision(self, df: pd.DataFrame, symbol: str, trend_df: pd.DataFrame = None) -> StrategyDecision:
        current = df.iloc[-1]
        
        # Get tier-specific params
        tier = config.get_tier_for_symbol(symbol)
        params = config.get_tier_params(tier)
        
        # Trend check
        trend_ok = True
        if trend_df is not None:
            trend_ema = indicators.calculate_ema(trend_df, 200).iloc[-1]
            trend_ok_long = trend_df.iloc[-1]['close'] > trend_ema
            trend_ok_short = trend_df.iloc[-1]['close'] < trend_ema
        else:
            trend_ok_long = trend_ok_short = True

        # ADX Filter: Only fade if trend is NOT extremely strong
        adx_ok = current.get('adx', 0) < 35

        # High-probability Reversion (LONG)
        if adx_ok and current['close'] <= current['bb_lower'] and current['rsi'] <= params.get('rsi_oversold', 25) and trend_ok_long:
            return StrategyDecision(
                action=TradeAction.BUY,
                symbol=symbol,
                strategy_id="SweepFade_Long",
                mode=StrategyMode.PROFIT_CORE,
                order_intent=OrderIntent.MAKER if params.get('order_intent') == 'MAKER' else OrderIntent.TAKER,
                tp_profile={'percent': params.get('tp_percent', 1.0)},
                sl_profile={'percent': params.get('sl_percent', 1.5)},
                reason="ADX_OK + BB_HIT + RSI_DIP + TREND_UP"
            )
            
        # High-probability Reversion (SHORT)
        if adx_ok and current['close'] >= current['bb_upper'] and current['rsi'] >= params.get('rsi_overbought', 72) and trend_ok_short:
            return StrategyDecision(
                action=TradeAction.SELL,
                symbol=symbol,
                strategy_id="SweepFade_Short",
                mode=StrategyMode.PROFIT_CORE,
                order_intent=OrderIntent.MAKER if params.get('order_intent') == 'MAKER' else OrderIntent.TAKER,
                tp_profile={'percent': params.get('tp_percent', 1.0)},
                sl_profile={'percent': params.get('sl_percent', 1.5)},
                reason="ADX_OK + BB_HIT + RSI_PEAK + TREND_DOWN"
            )
            
        return StrategyDecision(TradeAction.NONE, symbol, "SweepFade", StrategyMode.PROFIT_CORE, OrderIntent.TAKER)

class MomentumBreakout:
    """Momentum breakout strategy for trending markets."""
    def generate_decision(self, df: pd.DataFrame, symbol: str, trend_df: pd.DataFrame = None) -> StrategyDecision:
        current = df.iloc[-1]
        prev = df.iloc[-2]
        
        # Momentum condition (Fast EMA cross + ADX strength)
        if current['adx'] > 30:
            if current['close'] > current['ema'] and prev['close'] <= prev['ema']:
                return StrategyDecision(
                    action=TradeAction.BUY,
                    symbol=symbol,
                    strategy_id="Momentum_Long",
                    mode=StrategyMode.PROFIT_CORE,
                    order_intent=OrderIntent.TAKER,
                    tp_profile={'percent': 2.0}, # Wider TP for runners
                    sl_profile={'percent': 1.0},
                    reason="EMA_CROSS + HIGH_ADX"
                )
            elif current['close'] < current['ema'] and prev['close'] >= prev['ema']:
                return StrategyDecision(
                    action=TradeAction.SELL,
                    symbol=symbol,
                    strategy_id="Momentum_Short",
                    mode=StrategyMode.PROFIT_CORE,
                    order_intent=OrderIntent.TAKER,
                    tp_profile={'percent': 2.0},
                    sl_profile={'percent': 1.0},
                    reason="EMA_CROSS_DOWN + HIGH_ADX"
                )
        return StrategyDecision(TradeAction.NONE, symbol, "Momentum", StrategyMode.PROFIT_CORE, OrderIntent.TAKER)

# --- VOLUME SIDECAR STRATEGIES ---

class MicroSpreadScalperMaker:
    """Maker-focused high frequency scalper for Volume generation."""
    
    def __init__(self):
        self.strategy_id = "VolumeMaker"
        self.mode = StrategyMode.VOLUME_SIDECAR

    def generate_decision(self, df: pd.DataFrame, symbol: str, trend_df: pd.DataFrame = None) -> StrategyDecision:
        current = df.iloc[-1]
        rsi = current['rsi']
        bb_width = (current['bb_upper'] - current['bb_lower']) / current['bb_middle'] * 100  # Percentage width

        # Get tier-specific params
        tier = config.get_tier_for_symbol(symbol)
        params = config.get_tier_params(tier)

        # Tier-specific RSI thresholds
        rsi_oversold = params.get('rsi_oversold', 30)
        rsi_overbought = params.get('rsi_overbought', 68)
        tp_percent = params.get('tp_percent', 0.60)
        sl_percent = params.get('sl_percent', 2.0)

        # 🔧 TREND FILTER (NEW): Prevent counter-trend trades
        trend_ok_long = True
        trend_ok_short = True
        if trend_df is not None and len(trend_df) > 200:
            trend_ema = indicators.calculate_ema(trend_df, 200).iloc[-1]
            trend_price = trend_df.iloc[-1]['close']
            trend_ok_long = trend_price > trend_ema   # Only BUY if price > EMA200
            trend_ok_short = trend_price < trend_ema  # Only SELL if price < EMA200

        if rsi < rsi_oversold and trend_ok_long:
            return StrategyDecision(
                action=TradeAction.BUY,
                symbol=symbol,
                strategy_id=self.strategy_id,
                mode=self.mode,
                order_intent=OrderIntent.MAKER,
                tp_profile={'percent': tp_percent, 'maker_exit': True},
                sl_profile={'percent': sl_percent, 'atr_mult': 2.0},
                reason=f"VOL_MAKER_BUY: RSI={rsi:.1f} BB_width={bb_width:.2f}%"
            )
        elif rsi > rsi_overbought and trend_ok_short:
            return StrategyDecision(
                action=TradeAction.SELL,
                symbol=symbol,
                strategy_id=self.strategy_id,
                mode=self.mode,
                order_intent=OrderIntent.MAKER,
                tp_profile={'percent': tp_percent, 'maker_exit': True},
                sl_profile={'percent': sl_percent},
                reason=f"VOL_MAKER_SELL: RSI={rsi:.1f} BB_width={bb_width:.2f}%"
            )
        return StrategyDecision(TradeAction.NONE, symbol, self.strategy_id, self.mode, OrderIntent.MAKER)


class GhostReversionStrategy:
    """Ghost Strategy: Fade H4/H1 S/R levels with RSI extremes."""
    def __init__(self):
        self.strategy_id = "GhostStrategy"
        self.mode = StrategyMode.PROFIT_CORE

    def generate_decision(self, df: pd.DataFrame, symbol: str, trend_df: pd.DataFrame = None) -> StrategyDecision:
        if len(df) < 50:
            return StrategyDecision(TradeAction.NONE, symbol, self.strategy_id, self.mode, OrderIntent.TAKER)
            
        # Detection
        df_f = indicators.calculate_fractals(df, window=config.SR_WINDOW)
        sr_levels = indicators.extract_sr_levels(df_f, sensitivity=config.SR_SENSITIVITY)
        
        current = df.iloc[-1]
        price = current['close']
        rsi = current['rsi']
        
        # 🔧 TREND FILTER (EMA 200 on 15m)
        trend_ok_long = True
        trend_ok_short = True
        if config.USE_GHOST_TREND_FILTER and trend_df is not None and len(trend_df) >= 200:
            trend_ema = indicators.calculate_ema(trend_df, 200).iloc[-1]
            trend_price = trend_df.iloc[-1]['close']
            trend_ok_long = trend_price > trend_ema   # Only BUY if price > EMA200
            trend_ok_short = trend_price < trend_ema  # Only SELL if price < EMA200

        # Bullish: Price near Support + RSI Oversold
        for level in sr_levels:
            if abs(price - level) / level <= config.SR_SENSITIVITY:
                if rsi <= config.RSI_OVERSOLD and price > level and trend_ok_long: # Bounce from support
                    return StrategyDecision(
                        action=TradeAction.BUY,
                        symbol=symbol,
                        strategy_id=self.strategy_id,
                        mode=self.mode,
                        order_intent=OrderIntent.TAKER, # Ghost often enters at market for snipes
                        tp_profile={'percent': config.GHOST_TP_PCT},
                        sl_profile={'percent': config.GHOST_SL_PCT},
                        reason=f"GHOST_SUPPORT: Price={price:.4f} Level={level:.4f} RSI={rsi:.1f} (TREND_UP)"
                    )
                elif rsi >= config.RSI_OVERBOUGHT and price < level and trend_ok_short: # Reject from resistance
                    return StrategyDecision(
                        action=TradeAction.SELL,
                        symbol=symbol,
                        strategy_id=self.strategy_id,
                        mode=self.mode,
                        order_intent=OrderIntent.TAKER,
                        tp_profile={'percent': config.GHOST_TP_PCT},
                        sl_profile={'percent': config.GHOST_SL_PCT},
                        reason=f"GHOST_RESISTANCE: Price={price:.4f} Level={level:.4f} RSI={rsi:.1f} (TREND_DOWN)"
                    )
        
        return StrategyDecision(TradeAction.NONE, symbol, self.strategy_id, self.mode, OrderIntent.TAKER)


class StrategyRouter:
    def __init__(self):
        # Insert GhostStrategy as the primary selective strategy
        self.profit_strategies = [GhostReversionStrategy(), LiquiditySweepFade(), MomentumBreakout()]
        # DISABLED: MicroSpreadScalperMaker fallback removed for better selectivity
        # self.volume_strategy = MicroSpreadScalperMaker()
        
        # Cache for 5m RSI data (set externally by bot.py)
        self.mtf_rsi_cache = {}  # symbol -> 5m RSI value
    
    def set_mtf_rsi(self, symbol: str, rsi_5m: float):
        """Cache 5m RSI for MTF confirmation check"""
        self.mtf_rsi_cache[symbol] = rsi_5m
    
    def check_mtf_rsi(self, symbol: str, action: TradeAction, rsi_1m: float) -> bool:
        """
        MTF RSI Confirmation Check
        LONG: 1m RSI < 27 AND 5m RSI < 35
        SHORT: 1m RSI > 72 AND 5m RSI > 65
        
        Returns True if MTF check passes (or if disabled)
        """
        if not getattr(config, 'USE_MTF_RSI_CONFIRMATION', False):
            return True  # Disabled = always pass
        
        rsi_5m = self.mtf_rsi_cache.get(symbol)
        if rsi_5m is None:
            print(f"   ⚠️ [{symbol}] MTF RSI: 5m data not available, skipping MTF check")
            return True  # No 5m data = pass (fallback to old behavior)
        
        if action == TradeAction.BUY:
            # LONG: 1m RSI < 27 AND 5m RSI < 35
            threshold_1m = getattr(config, 'MTF_RSI_LONG_1M', 27)
            threshold_5m = getattr(config, 'MTF_RSI_LONG_5M', 35)
            passed = rsi_1m < threshold_1m and rsi_5m < threshold_5m
            if not passed:
                print(f"   🚫 [{symbol}] MTF RSI LONG REJECTED: 1m={rsi_1m:.1f} (need<{threshold_1m}) | 5m={rsi_5m:.1f} (need<{threshold_5m})")
            else:
                print(f"   ✅ [{symbol}] MTF RSI LONG OK: 1m={rsi_1m:.1f}<{threshold_1m} | 5m={rsi_5m:.1f}<{threshold_5m}")
            return passed
            
        elif action == TradeAction.SELL:
            # SHORT: 1m RSI > 72 AND 5m RSI > 65
            threshold_1m = getattr(config, 'MTF_RSI_SHORT_1M', 72)
            threshold_5m = getattr(config, 'MTF_RSI_SHORT_5M', 65)
            passed = rsi_1m > threshold_1m and rsi_5m > threshold_5m
            if not passed:
                print(f"   🚫 [{symbol}] MTF RSI SHORT REJECTED: 1m={rsi_1m:.1f} (need>{threshold_1m}) | 5m={rsi_5m:.1f} (need>{threshold_5m})")
            else:
                print(f"   ✅ [{symbol}] MTF RSI SHORT OK: 1m={rsi_1m:.1f}>{threshold_1m} | 5m={rsi_5m:.1f}>{threshold_5m}")
            return passed
        
        return True  # NONE action = pass
        
    def route(self, df: pd.DataFrame, symbol: str, trend_df: pd.DataFrame = None) -> StrategyDecision:
        # All tiers use same strategy set, params differ
        tier = config.get_tier_for_symbol(symbol)
        
        # Get 1m RSI for MTF check
        rsi_1m = df.iloc[-1].get('rsi', 50) if len(df) > 0 else 50
        
        # Try profit strategies in order (GhostReversion -> SweepFade -> Momentum)
        for strat in self.profit_strategies:
            decision = strat.generate_decision(df, symbol, trend_df)
            if decision.action != TradeAction.NONE:
                # 🔍 MTF RSI CONFIRMATION CHECK
                if self.check_mtf_rsi(symbol, decision.action, rsi_1m):
                    return decision
                else:
                    # MTF rejected - continue to next strategy or return NONE
                    continue
        
        # NO FALLBACK - Return NONE if no profit strategy signals
        # (MicroSpreadScalperMaker disabled for better selectivity)
        return StrategyDecision(TradeAction.NONE, symbol, "NoSignal", StrategyMode.PROFIT_CORE, OrderIntent.TAKER)

    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Mock version of adding indicators for Hackathon GhostAgent Demo."""
        import random
        # Create mock indicators directly in dataframe to simulate real strategy logic
        df['rsi'] = [random.uniform(20, 80) for _ in range(len(df))]
        df['bb_lower'] = df['close'] * 0.98
        df['bb_upper'] = df['close'] * 1.02
        df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['close']
        df['ema'] = df['close'] * random.uniform(0.99, 1.01)
        df['ema_21'] = df['close'] * random.uniform(0.98, 1.02)
        df['volume_sma'] = df['volume'] * random.uniform(0.8, 1.2)
        df['is_bullish'] = df['ema'] > df['ema_21']
        df['adx'] = [random.uniform(10, 50) for _ in range(len(df))]
        return df
