"""
Risk Manager: Handles balance tracking, fee calculation, and position sizing
"""

import config
import time
import csv
import os
from datetime import datetime
from typing import Dict, Optional


class RiskManager:
    """
    Manages trading risk including:
    - Paper trading balance simulation
    - Position size calculation
    - Fee profitability checks
    - Stop-loss validation
    """
    
    def __init__(self):
        self.price_update_count = 0
        self.duplicate_ids_to_cancel = [] # IDs to be cleaned up by the bot
        # Capital & Balance Tracking
        self.paper_balance_usd = getattr(config, 'INITIAL_BALANCE_USD', 100.0) 
        self.daily_start_balance = self.paper_balance_usd  # Tracking for circuit breaker
        self.daily_equity_start = self.paper_balance_usd   # Tracking for % DD breaker
        self.positions = []  # List of open positions (Dicts)
        self.position_layers = {} # symbol -> list of Layer Dicts
        self.trade_history = []  # Historical trades for analysis
        self.cooldowns = {} # symbol -> last_close_time
        self.last_cleanup_time = time.time()  # Track last cleanup
        self.operation_locks = set() # Set of symbols currently being traded (FIX BUG #Q04)
        self.exchange_adapter = None # Will be linked by the bot
        
        # --- PENDING ORDERS SYSTEM (GHOST PROFESSIONAL) ---
        # symbol -> { 'orders': [{'level': price, 'amount_usd': amt, 'type': 'ADD'|'TRIM', 'placed': bool, 'order_id': str}] }
        self.pending_orders = {}
        
        # --- CIRCUIT BREAKER & GUARDRAILS ---
        self.daily_start_time = 0.0
        self.is_circuit_breaker_active = False
        
        # Volume Guardrail State (ULTRATHINK)
        self.daily_equity_start_time = time.time()
        self.volume_sidecar_enabled = True
        self.volume_throttle_level = 1.0  # FIX: Multiplier (1.0=normal, 0.5=throttled, 0.0=killed)
        self.volume_kill_time = None
        self.volume_daily_trades = []
        self.volume_rolling_trades = []
        
        # Load history from CSV to ensure persistence (ULTRATHINK)
        self._load_history_from_csv()

    def check_circuit_breaker(self) -> bool:
        """Kayıp limiti kontrolü (Ghost Stratejisi için devre dışı)"""
        return False

    def check_volume_guardrails(self) -> Dict:
        """Check Volume Sidecar guardrails and update state"""
        if not config.VOLUME_GUARDRAIL_ENABLED:
            return {'enabled': True, 'throttle_level': 0, 'reason': 'Disabled'}
        
        current_equity = self.get_balance()
        # FIX: Correct DD calculation (loss = current is LESS than start)
        equity_dd_pct = ((self.daily_equity_start - current_equity) / 
                         self.daily_equity_start * 100) if self.daily_equity_start > 0 else 0
        
        # Only trigger if POSITIVE drawdown (current < start)
        if equity_dd_pct > 0 and equity_dd_pct >= config.VOLUME_GUARDRAIL_EQUITY_DD_CUT_PCT:
            self.volume_sidecar_enabled = False
            self.volume_kill_time = time.time()
            self.volume_throttle_level = 2
            print(f"\n🚨 [VOLUME GUARDRAIL] KILL-SWITCH! DD: {equity_dd_pct:.2f}%")
            return {
                'enabled': False,
                'throttle_level': 2,
                'reason': f'KILL: DD {equity_dd_pct:.2f}%',
                'equity_dd_pct': equity_dd_pct,
                'daily_pnl': 0,
                'rolling_pnl': 0
            }
        
        daily_pnl = sum(t.get('net_pnl', 0) for t in self.volume_daily_trades)
        rolling_trades = self.volume_rolling_trades[-config.VOLUME_GUARDRAIL_ROLLING_WINDOW:]
        rolling_pnl = sum(t.get('net_pnl', 0) for t in rolling_trades)

    def get_statistics(self) -> Dict:
        """Dashboard için istatistikleri topla"""
        try:
            trades = self.trade_history
            win_rate = 0
            
            # 1. Realized P&L (from closed trades)
            total_realized_pnl = 0
            daily_realized_pnl = 0
            today_str = datetime.now().strftime('%Y-%m-%d')
            
            if trades:
                wins = len([t for t in trades if float(t.get('profit_usd', 0) or 0) > 0 or t.get('net_pnl', 0) > 0])
                win_rate = (wins / len(trades)) * 100
                total_realized_pnl = sum(float(t.get('profit_usd', 0) or 0) for t in trades)
                
                # Daily realized calculation
                for t in trades:
                    ts = t.get('timestamp', '')
                    if today_str in (ts if ts else ''):
                        daily_realized_pnl += float(t.get('profit_usd', 0) or 0)

            # 2. Unrealized P&L (from open positions)
            total_unrealized_pnl_usd = 0
            for p in self.positions:
                u_pnl_pct = p.get('unrealized_pnl', 0)
                pos_value = p.get('position_value_usd', 0) or (p.get('entry_price', 0) * p.get('position_size', 0))
                if pos_value > 0:
                    total_unrealized_pnl_usd += (pos_value * (u_pnl_pct / 100))

            # Session metrics
            current_equity = self.get_balance()
            session_pnl = current_equity - self.daily_equity_start
            
            return {
                "win_rate": round(win_rate, 1),
                "total_trades": len(trades),
                "total_realized_pnl": round(total_realized_pnl, 2),
                "daily_realized_pnl": round(daily_realized_pnl, 2),
                "unrealized_pnl_usd": round(total_unrealized_pnl_usd, 2),
                "session_pnl": round(session_pnl, 2),
                "total_committed_margin": round(self.get_total_committed_margin(), 2),
                "is_circuit_breaker": self.is_circuit_breaker_active,
                "volume_throttle": self.volume_throttle_level,
                "active_strategy_mode": getattr(config, 'ACTIVE_STRATEGY_MODE', 'ECONOMIC')
            }
        except Exception as e:
            print(f"[WARNING] get_statistics error: {e}")
            return {"error": str(e)}

    def get_total_committed_margin(self) -> float:
        """Tüm açık pozisyonlara bağlanan toplam marjin (USD)"""
        total = 0.0
        for pos in self.positions:
            pos_value = pos.get('position_size', 0) * pos.get('entry_price', 0)
            total += pos_value / config.LEVERAGE if config.LEVERAGE > 0 else 0
        return total

    def get_liquidation_distance(self, symbol: str, current_price: float) -> float:
        """Likidasyon mesafesini hesapla (%)"""
        pos = next((p for p in self.positions if p['symbol'] == symbol), None)
        if not pos: return 100.0
        
        leverage = config.LEVERAGE
        entry = pos['entry_price']
        direction = pos['direction']
        
        if direction == 'BUY':
            liq_price = entry * (1 - 1/leverage)
            dist = (current_price - liq_price) / current_price * 100 if current_price > 0 else 100
        else:
            liq_price = entry * (1 + 1/leverage)
            dist = (liq_price - current_price) / current_price * 100 if current_price > 0 else 100
            
        return max(0, dist)
        
        if self.volume_kill_time:
            elapsed_min = (time.time() - self.volume_kill_time) / 60
            if elapsed_min < config.VOLUME_GUARDRAIL_COOLDOWN_MINUTES:
                return {
                    'enabled': False,
                    'throttle_level': 2,
                    'reason': f'Cooldown: {elapsed_min:.1f}min',
                    'equity_dd_pct': equity_dd_pct,
                    'daily_pnl': daily_pnl,
                    'rolling_pnl': rolling_pnl
                }
            else:
                self.volume_sidecar_enabled = True
                self.volume_kill_time = None
                self.volume_throttle_level = 0
        
        return {
            'enabled': self.volume_sidecar_enabled,
            'throttle_level': self.volume_throttle_level,
            'reason': 'OK',
            'equity_dd_pct': equity_dd_pct,
            'daily_pnl': daily_pnl,
            'rolling_pnl': rolling_pnl
        }

    def reset_daily_metrics(self):
        """Reset daily metrics at midnight or bot restart"""
        self.daily_equity_start = self.get_balance()
        self.daily_equity_start_time = time.time()
        self.volume_daily_trades = []
        self.volume_throttle_level = 0
        print(f"📅 Daily reset. Equity: ${self.daily_equity_start:.2f}")

    def get_balance(self) -> float:
        """Get current total balance (API first, then simulated fallback)"""
        # API check prioritized regardless of mode (User wants to see real balance)
        if self.exchange_adapter:
            try:
                real_balance = self.exchange_adapter.get_balance() 
                if real_balance is not None and real_balance > 0:
                    # Sync simulation with real balance if we're in paper mode
                    if config.PAPER_TRADING:
                        self.paper_balance_usd = real_balance
                    return real_balance
            except Exception:
                pass
                
        # SIMULATED FALLBACK
        if config.PAPER_TRADING:
            return self.paper_balance_usd
            
        # SAFETY FALLBACK: Use INITIAL_BALANCE_USD from config
        return getattr(config, 'INITIAL_BALANCE_USD', 100.0)
    
    def get_free_balance(self) -> float:
        """Get balance not allocated to open positions (FIX BUG #25)
        
        This calculates how much margin is actually available for new trades.
        With leverage, each position uses: position_value / leverage as margin.
        
        Returns:
            Free balance available for new positions
        """
        total_balance = self.get_balance()
        
        # Calculate margin currently in use
        margin_in_use = 0.0
        for pos in self.positions:
            # Each position uses margin = position_value / leverage
            position_value = pos['entry_price'] * pos['position_size']
            margin_used = position_value / config.LEVERAGE if hasattr(config, 'LEVERAGE') else position_value
            margin_in_use += margin_used
        
        free_balance = total_balance - margin_in_use
        return max(0.0, free_balance)  # Never return negative
    
    def calculate_position_size(self, current_price: float, direction: str = "BUY", symbol: str = None, decision=None) -> float:
        """Dinamik Pozisyon Büyüklüğü (Mode-aware + Guardrails + Spread Gate)"""
        # 1. Circuit Breaker Kontrolü
        if self.check_circuit_breaker():
            return 0.0

        # Mode detection
        mode = decision.mode.value if decision and hasattr(decision, 'mode') else "PROFIT_CORE"
        
        # 2. Pozisyon var mı bak (Var ise bu bir kademe eklemesidir)
        existing_pos = next((p for p in self.positions if p['symbol'] == symbol), None)
        is_layering = existing_pos is not None

        # 2. SPREAD GATE (HARD RULE)
        if symbol and self.exchange_adapter and hasattr(self.exchange_adapter, 'get_spread'):
            spread_data = self.exchange_adapter.get_spread(symbol)
            spread_bps = spread_data.get('spread_bps', 999999)
            
            # Tier-based standard thresholds
            threshold = getattr(config, 'MAX_SPREAD_BPS_QUALITY', 5.0) if mode == "PROFIT_CORE" else getattr(config, 'MAX_SPREAD_BPS_VOLUME', 10.0)
            min_liq = getattr(config, 'MIN_LIQUIDITY_USD', 1000.0)

            # [GHOST RECOVERY] Loosen spread/liquidity gates for layering
            recovery_spread_threshold = 30.0 # Allow up to 30bps for recovery
            recovery_liq_threshold = 250.0   # Lower liq floor for recovery
            
            eff_threshold = recovery_spread_threshold if is_layering else threshold
            eff_min_liq = recovery_liq_threshold if is_layering else min_liq

            if spread_bps > eff_threshold:
                # Only log every 10th iteration to avoid spam
                if getattr(self, 'price_update_count', 0) % 10 == 0:
                    print(f"   ⛔ [{symbol}] SPREAD TOO WIDE: {spread_bps:.1f}bps > {eff_threshold:.0f}bps threshold {'(RECOVERY)' if is_layering else ''}")
                return 0.0
        
        # 3. LIQUIDITY GATE (ANTI-SLIPPAGE)
        if symbol and self.exchange_adapter and hasattr(self.exchange_adapter, 'get_liquidity'):
            depth_pct = getattr(config, 'LIQUIDITY_DEPTH_PCT', 0.5)
            # eff_min_liq calculated above
            
            liquidity = self.exchange_adapter.get_liquidity(symbol, depth_pct)
            # Check the side we are interested in (BUY -> ask liquidity, SELL -> bid liquidity)
            # Actually, for a taker order, we consume liquidity on the opposite side.
            side_liq = liquidity['ask_liquidity_usd'] if direction == "BUY" else liquidity['bid_liquidity_usd']
            
            if side_liq < eff_min_liq:
                if getattr(self, 'price_update_count', 0) % 10 == 0:
                    print(f"   ⛔ [{symbol}] INSUFFICIENT LIQUIDITY: ${side_liq:.1f} < ${eff_min_liq:.0f} {'(RECOVERY)' if is_layering else ''}")
                return 0.0

        # 4. Korelasyon ve Limit Kontrolü
        if not self.can_open_new_position(symbol, direction, mode, is_layering=is_layering):
            return 0.0

        available_balance = self.get_free_balance()
        total_balance = self.get_balance()
        
        # --- GHOST STRATEGY SIZING (FIXED USD MODE) ---
        
        # [VIP STRATEGY] TON/USDT ÖZEL TARİFE (Kullanıcı İsteği: 15+8+8+15)
        if symbol == "TON/USDT" or symbol == "TON_USDT_Perp":
            if not existing_pos:
                margin_amount_usd = 15.0
                tag = "INITIAL (TON_VIP)"
            else:
                layers = self.position_layers.get(symbol, [])
                added_count = max(0, len(layers) - 1)
                
                if added_count == 0: 
                    margin_amount_usd = 8.0  # Tier 1
                elif added_count == 1: 
                    margin_amount_usd = 8.0  # Tier 2
                else: 
                    margin_amount_usd = 15.0 # Tier 3+
                
                tag = f"LAYER_{added_count+1} (TON_VIP)"
                
        else:
            # DİĞER SEMBOLLER İÇİN MODA GÖRE BELİRLENEN KATMANLAR (MARTINGALE / ECONOMIC)
            strategy_layers = getattr(config, 'CURRENT_STRATEGY_LAYERS', [10.0, 5.0, 5.0, 10.0])
            
            if not existing_pos:
                margin_amount_usd = strategy_layers[0]
                tag = f"INITIAL ({getattr(config, 'ACTIVE_STRATEGY_MODE', 'ECON')})"
            else:
                layers = self.position_layers.get(symbol, [])
                layer_index = len(layers) # Bir sonraki kademenin indeksi
                
                # Eğer strateji listesinden fazla kademe gelirse, son elemanı kullan
                if layer_index < len(strategy_layers):
                    margin_amount_usd = strategy_layers[layer_index]
                else:
                    margin_amount_usd = strategy_layers[-1]
                
                tag = f"LAYER_{layer_index} ({getattr(config, 'ACTIVE_STRATEGY_MODE', 'ECON')})"
            
        # --- [DYNAMIC SCALING] ---
        if getattr(config, 'GHOST_AUTO_SCALE_ENABLED', False):
            base_balance = getattr(config, 'INITIAL_BALANCE_USD', 100.0)
            if base_balance > 0:
                scaling_multiplier = total_balance / base_balance
                # Apply multiplier to margin
                original_margin = margin_amount_usd
                margin_amount_usd = margin_amount_usd * scaling_multiplier
                if getattr(config, 'VERBOSE_LOGGING', True):
                    print(f"   [SCALE] [SCALE] Applied {scaling_multiplier:.2f}x multiplier: ${original_margin:.1f} -> ${margin_amount_usd:.1f}")

        # Borsa alt limit koruması
        min_allowed = getattr(config, 'MIN_TRADE_SIZE_USD', 5.0)
        if margin_amount_usd < min_allowed:
            margin_amount_usd = min_allowed 
            
        # Hard Safety Cap (İnsan hatasına karşı son koruma)
        hard_cap = getattr(config, 'HARD_SAFETY_USD_MARGIN_MAX', 50.0)
        margin_amount_usd = min(margin_amount_usd, hard_cap)
        
        if margin_amount_usd > available_balance:
            print(f"   [WARNING] [{symbol}] Yetersiz bakiye (${margin_amount_usd:.2f}). Mevcut: {available_balance:.2f}")
            # En azından kalanı kullanmaya çalış (Ghost bazen kasanın dibine kadar girer)
            margin_amount_usd = available_balance * 0.95 
            if margin_amount_usd < min_allowed: return 0.0

        # Final position value calculation
        position_value_usd = margin_amount_usd * config.LEVERAGE
        position_size = position_value_usd / current_price
        
        # risk_pct'yi hesapla (logging için)
        risk_pct = margin_amount_usd / total_balance if total_balance > 0 else 0
        
        if getattr(config, 'VERBOSE_LOGGING', True):
            print(f"   [GHOST_SIZING] {symbol} | {tag} | Balance: ${total_balance:.2f} | Entry: %{risk_pct*100:.1f} | Margin: ${margin_amount_usd:.2f}")
            print(f"   [GHOST_SIZING] {symbol} | Leverage: {config.LEVERAGE}x | Total Value: ${position_value_usd:.2f} | Quantity: {position_size:.4f}")
        
        min_coin_size = getattr(config, 'MIN_TRADE_SIZE_COIN', 0.001)
        if position_size < min_coin_size:
            return 0.0
            
        return position_size
    
    def calculate_fees(self, entry_price: float, exit_price: float, 
                      position_size: float,
                      entry_intent: str = "TAKER",
                      exit_intent: str = "TAKER") -> Dict[str, float]:
        """
        Calculate trading fees based on order intent (maker/taker)
        
        Args:
            entry_price: Entry price
            exit_price: Exit price
            position_size: Size of position in base currency
            entry_intent: "MAKER" or "TAKER"
            exit_intent: "MAKER" or "TAKER"
            
        Returns:
            Dictionary with entry_fee, exit_fee, total_fee, fee percentages
        """
        # Fee percentages based on intent
        entry_fee_pct = (config.MAKER_FEE_PERCENT if entry_intent == "MAKER" 
                         else config.TAKER_FEE_PERCENT)
        exit_fee_pct = (config.MAKER_FEE_PERCENT if exit_intent == "MAKER" 
                        else config.TAKER_FEE_PERCENT)
        
        # Calculate fee amounts
        entry_value = entry_price * position_size
        exit_value = exit_price * position_size
        
        entry_fee = (entry_value * entry_fee_pct) / 100
        exit_fee = (exit_value * exit_fee_pct) / 100
        total_fee = entry_fee + exit_fee
        
        return {
            'entry_fee': entry_fee,
            'exit_fee': exit_fee,
            'total_fee': total_fee,
            'entry_fee_pct': entry_fee_pct,
            'exit_fee_pct': exit_fee_pct
        }

    
    def is_profitable_after_fees(self, entry_price: float, exit_price: float, 
                                 position_size: float, direction: str = "BUY",
                                 entry_intent: str = "TAKER",
                                 exit_intent: str = "TAKER") -> bool:
        """Check if trade is profitable after fees with intent awareness"""
        fees = self.calculate_fees(entry_price, exit_price, position_size,
                                   entry_intent, exit_intent)
        
        if direction.upper() == "BUY":
            entry_cost = entry_price * position_size + fees["entry_fee"]
            exit_revenue = exit_price * position_size - fees["exit_fee"]
            profit = exit_revenue - entry_cost
            base_val = entry_cost
        else:
            entry_revenue = entry_price * position_size - fees["entry_fee"]
            exit_cost = exit_price * position_size + fees["exit_fee"]
            profit = entry_revenue - exit_cost
            base_val = entry_revenue
            
        profit_percent = (profit / base_val) * 100 if base_val > 0 else 0
        
        if base_val <= 0:
            print(f"[WARNING] ERROR: Invalid profit calculation - base_val={base_val:.2f}")
            return False
        
        return profit_percent >= config.MIN_PROFIT_PERCENT
    
    def calculate_stop_loss_price(self, entry_price: float, direction: str, symbol: str = None, atr: float = None, sl_profile: Dict = None) -> float:
        """Dinamik stop-loss (Profile aware + Minimum Mesafe Korumalı)"""
        if sl_profile and 'percent' in sl_profile:
            stop_distance = entry_price * (sl_profile['percent'] / 100)
        else:
            # Minimum stop payı (%0.6 - Gürültüyü ve fee'leri kurtarmak için)
            min_sl_dist = entry_price * (getattr(config, 'MIN_STOP_LOSS_FLOOR', 0.6) / 100)
            
            if atr and atr > 0:
                atr_dist = atr * getattr(config, 'ATR_SL_MULTIPLIER', 2.5)
                # En az %0.6 veya ATR stop (Hangisi daha güvenliyse)
                stop_distance = max(atr_dist, min_sl_dist)
            else:
                stop_distance = min_sl_dist

        if direction.upper() == "BUY":
            sl_price = entry_price - stop_distance
        else:
            sl_price = entry_price + stop_distance
            
        # SANITY CHECK: Avoid negative or absurd values
        if sl_price < 0: sl_price = 0
        
        # Hard cap for crazy values (e.g. ID confusion leading to billions)
        if sl_price > 1_000_000:
            print(f"   [WARNING] [RISK_GUARD] Absurd SL Price detected (${sl_price:.2f}). Clamping to Entry.")
            sl_price = entry_price # Fail safe
            
        return sl_price

    def calculate_take_profit_price(self, entry_price: float, direction: str, symbol: str = None, atr: float = None, tp_profile: Dict = None) -> float:
        """Dinamik take-profit (Profile aware + Minimum Kar Korumalı)"""
        if tp_profile and 'percent' in tp_profile:
            profit_distance = entry_price * (tp_profile['percent'] / 100)
        elif tp_profile and 'roi_percent' in tp_profile:
            # ROI % to Price % conversion: price_pct = roi_pct / leverage
            roi_raw = tp_profile['roi_percent']  # e.g. 0.08 for 8%
            price_pct = roi_raw / config.LEVERAGE if hasattr(config, 'LEVERAGE') else roi_raw
            profit_distance = entry_price * price_pct
        else:
            # Minimum kar hedefi (%0.8 - Net kâr bırakması için)
            min_tp_dist = entry_price * (getattr(config, 'MIN_PROFIT_TARGET_FLOOR', 0.8) / 100)
            
            if atr and atr > 0:
                atr_dist = atr * getattr(config, 'ATR_TP_MULTIPLIER', 3.5)
                profit_distance = max(atr_dist, min_tp_dist)
            else:
                profit_distance = min_tp_dist

        if direction.upper() == "BUY":
            tp_price = entry_price + profit_distance
        else:
            tp_price = entry_price - profit_distance

        # SANITY CHECK: Avoid negative or absurd values
        if tp_price < 0: tp_price = 0
        
        # Hard cap for crazy values
        if tp_price > 1_000_000:
            print(f"   [WARNING] [RISK_GUARD] Absurd TP Price detected (${tp_price:.2f}). Clamping to realistic target.")
            # Fallback: Entry +/- 50%
            tp_price = entry_price * 1.5 if direction=="BUY" else entry_price * 0.5
            
        return tp_price
    
    def sync_positions(self, exchange_positions: list, exchange_orders: list = None):
        """
        Sync local positions with exchange positions on startup
        """
        if not exchange_positions:
            return
            
        print(f"🔄 Syncing {len(exchange_positions)} positions from exchange...")
        for p in exchange_positions:
            symbol = p['symbol']
            
            # Find existing position in memory
            existing_pos = next((pos for pos in self.positions if pos['symbol'] == symbol), None)
                
            # Attempt to find existing TP and SL orders
            tp_order_id = None
            sl_order_id = None
            
            if exchange_orders:
                # For a BUY position, TP/SL are SELL orders
                tp_matches = []
                sl_matches = []
                
                for o in exchange_orders:
                    o_sym_raw = o.get('symbol', '')
                    o_sym = o_sym_raw.replace('_USDT_Perp', '').replace('/USDT', '').split('_')[0]
                    p_sym = p['symbol'].replace('/USDT', '').split('_')[0]
                    target_side = 'SELL' if p['direction'] == 'BUY' else 'BUY'
                    
                    if o_sym == p_sym and o.get('side', '').upper() == target_side:
                        # Heuristic: Identify SL and TP
                        o_type = o.get('type', '').upper()
                        if any(t in o_type for t in ['STOP', 'TRIGGER', 'CONDITIONAL', 'STOP_LOSS']):
                            sl_matches.append(o)
                        elif any(t in o_type for t in ['LIMIT', 'TAKE_PROFIT', 'TRIGGER']):
                            # LIMIT/TAKE_PROFIT/etc are TP
                            tp_matches.append(o)
                
                # Link TP (Consistent with bot.py: Pick highest ID)
                if tp_matches:
                    tp_matches.sort(key=lambda x: str(x.get('id', '') or x.get('order_id', '')), reverse=True)
                    tp_order_id = tp_matches[0].get('id') or tp_matches[0].get('order_id')
                    
                    # SAFETY: If existing ID is placeholder, ADOPT it and don't clean it up
                    if existing_pos and (not existing_pos.get('tp_order_id') or existing_pos.get('tp_order_id') == '0x00'):
                        existing_pos['tp_order_id'] = tp_order_id
                        print(f"🎯 [{symbol}] Adopted real TP ID: {tp_order_id}")
                        # Don't add to cleanup if we just adopted it
                        if len(tp_matches) > 1:
                            self.duplicate_ids_to_cancel.extend([(m.get('id') or m.get('order_id'), symbol) for m in tp_matches[1:]])
                    elif len(tp_matches) > 1:
                        self.duplicate_ids_to_cancel.extend([(m.get('id') or m.get('order_id'), symbol) for m in tp_matches[1:]])
                        print(f"   [CLEANUP] Found {len(tp_matches)-1} duplicate TP orders for {symbol}.")

                # Link SL (Consistent sorting)
                if sl_matches:
                    sl_matches.sort(key=lambda x: str(x.get('id', '') or x.get('order_id', '')), reverse=True)
                    sl_order_id = sl_matches[0].get('id') or sl_matches[0].get('order_id')
                    
                    # SAFETY: Same for SL
                    if existing_pos and (not existing_pos.get('sl_order_id') or existing_pos.get('sl_order_id') == '0x00'):
                        existing_pos['sl_order_id'] = sl_order_id
                        print(f"🛑 [{symbol}] Adopted real SL ID: {sl_order_id}")
                        if len(sl_matches) > 1:
                            self.duplicate_ids_to_cancel.extend([(m.get('id') or m.get('order_id'), symbol) for m in sl_matches[1:]])
                    elif len(sl_matches) > 1:
                        self.duplicate_ids_to_cancel.extend([(m.get('id') or m.get('order_id'), symbol) for m in sl_matches[1:]])
                        print(f"   [CLEANUP] Found {len(sl_matches)-1} duplicate SL orders for {symbol}.")

                # HMR FIX: Ensure existing position has HMR fields
                if existing_pos:
                    if 'peak_price' not in existing_pos:
                        existing_pos['peak_price'] = p['entry_price']
                    if 'trailing_sl' not in existing_pos:
                        existing_pos['trailing_sl'] = existing_pos.get('stop_loss', p['entry_price'])
                    
                    # [GHOST FIX] Ensure position_layers is ALWAYS initialized for existing positions too!
                    if symbol not in self.position_layers or len(self.position_layers[symbol]) == 0:
                        self.position_layers[symbol] = [{
                            'price': existing_pos['entry_price'],
                            'size': existing_pos['position_size'],
                            'time': existing_pos.get('opened_at') or time.time(),
                            'first_entry_price': existing_pos.get('first_entry_price', existing_pos['entry_price'])
                        }]
                        print(f"   🔧 [{symbol}] Position layers RESTORED: Baseline=${existing_pos['entry_price']:.4f}")
                    
                    return True # Existing position synced

            if not existing_pos:
                # CRITICAL SAFETY: Validate Position Size before Adoption
                # INCREASED for Ghost Strategy (10% entry on $1k balance = $2k+ position value)
                position_value_usd = p['entry_price'] * p['position_size']
                max_safety_val = 5000.0 # Hard global safety cap
                if position_value_usd > max_safety_val:
                    print(f"🚨 [RISK_GATE] REJECTED Adoption of {symbol}: Value ${position_value_usd:.2f} > Safety Cap ${max_safety_val:.2f}")
                    return None
                
                # Create new memory entry
                new_pos = {
                    "symbol": symbol,
                    "direction": p['direction'],
                    "entry_price": p['entry_price'],
                    "position_size": p['position_size'],
                    "entry_value_usd": position_value_usd,
                    "first_entry_price": p['entry_price'], # Init first entry as current (fallback)
                    "stop_loss": self.calculate_stop_loss_price(p['entry_price'], p['direction']),
                    "trailing_sl": self.calculate_stop_loss_price(p['entry_price'], p['direction']), 
                    "peak_price": p['entry_price'], 
                    "synced": True,
                    "tp_order_id": tp_order_id,
                    "sl_order_id": sl_order_id,
                    "opened_at": p.get('timestamp') or time.time()
                }
                self.positions.append(new_pos)
                
                # --- [ULTRATHINK] SMART LAYER SYNC ---
                # Estimate which DCA layer we are currently at based on total margin used
                if symbol not in self.position_layers:
                    leverage = float(getattr(config, 'LEVERAGE', 10.0))
                    total_margin = position_value_usd / leverage
                    
                    # Get base margin from config or settings.json
                    initial_margin = float(getattr(config, 'GHOST_INITIAL_MARGIN_USD', 10.0))
                    
                    # Logic: Current Value / (Margin * Leverage) gives rough layer count
                    # We use a 0.5x buffer to avoid over-counting due to small price swings
                    layers_to_add = max(1, int((total_margin + (initial_margin / 2)) / initial_margin))
                    
                    # Cap by max allowed layers
                    max_allowed = int(getattr(config, 'MAX_TIER_COUNT', 3)) + 1 # +1 for initial
                    layers_to_add = min(layers_to_add, max_allowed)
                    
                    # Initialize with placeholders
                    self.position_layers[symbol] = []
                    for i in range(layers_to_add):
                        self.position_layers[symbol].append({
                            'price': p['entry_price'],
                            'size': p['position_size'] / layers_to_add,
                            'time': p.get('timestamp') or time.time(),
                            'first_entry_price': p['entry_price']
                        })
                    
                    if layers_to_add > 1:
                        print(f"   💡 [SMART_SYNC] Detected {layers_to_add} layers for {symbol} based on ${total_margin:.2f} margin.")

                print(f"   + Synced position: {symbol} ({p['direction']}) | Value: ${position_value_usd:.2f} | TP:{'found' if tp_order_id else 'None'} SL:{'found' if sl_order_id else 'None'}")
                return True # New position synced

    def open_position(self, symbol: str, direction: str, entry_price: float, size: float, stop_loss: float, 
                     tp_order_id: str = None, sl_order_id: str = None,
                     entry_intent: str = "TAKER", exit_intent: str = "TAKER") -> Dict:
        # CRITICAL SAFETY: Final Size Check
        # INCREASED for Ghost Strategy (10% entry = larger position values)
        pos_value_usd = entry_price * size
        max_safety_val = 5000.0 # Hard global safety cap
        
        if pos_value_usd > max_safety_val:
            print(f"🚨 [RISK_GATE] REJECTED Opening {symbol}: Value ${pos_value_usd:.2f} > Safety Cap ${max_safety_val:.2f}")
            return None

        """Open position with order intent tracking"""
        position = {
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "position_size": size,
            "stop_loss": stop_loss,
            "trailing_sl": stop_loss, # Initial trailing SL is the hard SL
            "peak_price": entry_price, # Track highest/lowest price for trailing
            "entry_value_usd": entry_price * size,
            "first_entry_price": entry_price,
            "tp_order_id": tp_order_id,
            "sl_order_id": sl_order_id,
            "entry_intent": entry_intent,  # NEW: Track order intent
            "exit_intent": exit_intent,    # NEW: Track expected exit intent
            "opened_at": time.time()
        }
        
        if config.PAPER_TRADING:
            self.paper_balance_usd -= position["entry_value_usd"]
            print(f"[STATS] [PAPER] Entry {direction} at ${entry_price:.2f} | Intent: {entry_intent} | SL: ${stop_loss:.2f}")
        else:
            print(f"[ENTRY] [REAL] Entry {direction} at ${entry_price:.2f} | Intent: {entry_intent} | SL: ${stop_loss:.2f}")
        
        # CRITICAL: Set entry cooldown to prevent double-entry on slow exchange sync
        # Even if position list isn't updated, can_open_new_position will block for 60s
        self.cooldowns[symbol] = time.time()
        
        self.positions.append(position)
        
        # Initialize layers (preserve first entry price for SL logic)
        self.position_layers[symbol] = [{
            'price': entry_price,
            'size': size,
            'time': time.time(),
            'first_entry_price': entry_price
        }]
        
        return position

    def get_first_entry_price(self, symbol: str) -> float:
        """Return the preserved first entry price for a symbol.

        Falls back to the current position entry_price if no layer info exists.
        """
        # Prefer the value stored on position_layers (set at open or when first layer added)
        if symbol in self.position_layers and len(self.position_layers[symbol]) > 0:
            fe = self.position_layers[symbol][0].get('first_entry_price')
            if fe is not None:
                return fe

        # Fallback to the position dict
        pos = next((p for p in self.positions if p['symbol'] == symbol), None)
        if pos:
            return pos.get('first_entry_price', pos.get('entry_price', 0.0))

        return 0.0

    def add_layer(self, symbol: str, price: float, size: float):
        """Add a new layer (kademe) and recalculate average entry."""
        pos = next((p for p in self.positions if p['symbol'] == symbol), None)
        if not pos: return
        
        # Add to layers tracking
        if symbol not in self.position_layers:
            first_entry_price = pos['entry_price']  # İlk giriş fiyatını kaydet
            self.position_layers[symbol] = [{
                'price': pos['entry_price'], 
                'size': pos['position_size'], 
                'time': pos['opened_at'],
                'first_entry_price': first_entry_price  # ← ANAHTAR: İlk fiyat korunacak
            }]
            
        # Yeni kademe ekle
        first_entry = self.position_layers[symbol][0].get('first_entry_price', self.position_layers[symbol][0]['price'])
        self.position_layers[symbol].append({
            'price': price,
            'size': size,
            'time': time.time(),
            'first_entry_price': first_entry  # ← İlk fiyat tüm katmanlar için aynı
        })
        
        # Recalculate weighted average
        total_size = sum(l['size'] for l in self.position_layers[symbol])
        weighted_sum = sum(l['price'] * l['size'] for l in self.position_layers[symbol])
        new_avg = weighted_sum / total_size
        
        pos['entry_price'] = new_avg
        pos['position_size'] = total_size
        pos['entry_value_usd'] = new_avg * total_size
        
        print(f"   [LAYER_ADD] [{symbol}] TIER EKLENDİ! Yeni Ortalama: ${new_avg:.4f} | Toplam Büyüklük: {total_size:.4f}")

    def get_trim_info(self, symbol: str) -> Optional[Dict]:
        """Get info for 'Trim at Cost' - size and price to close the last layer."""
        if symbol not in self.position_layers or len(self.position_layers[symbol]) <= 1:
            return None
            
        last_layer = self.position_layers[symbol][-1]
        pos = next((p for p in self.positions if p['symbol'] == symbol), None)
        
        return {
            'size_to_trim': last_layer['size'],
            'maliyet': pos['entry_price']
        }

    def pop_layer(self, symbol: str):
        """Remove the last layer (after successful trim)."""
        if symbol in self.position_layers and len(self.position_layers[symbol]) > 1:
            last = self.position_layers[symbol].pop()
            pos = next((p for p in self.positions if p['symbol'] == symbol), None)
            if pos:
                pos['position_size'] -= last['size']
                # Recompute entry value
                pos['entry_value_usd'] = pos.get('entry_price', 0.0) * pos['position_size']

                # If we've popped back to only the first layer, reset entry_price to that first entry
                if len(self.position_layers[symbol]) == 1:
                    # When we return to a single remaining layer, the position's current entry_price
                    # (which was the weighted average at trim) becomes the new baseline.
                    new_baseline = pos.get('entry_price')
                    if new_baseline is not None:
                        pos['first_entry_price'] = new_baseline
                        # Also update the single remaining layer to reflect this "new home"
                        self.position_layers[symbol][0]['price'] = new_baseline
                        self.position_layers[symbol][0]['first_entry_price'] = new_baseline
                        print(f"   [RESET] [{symbol}] TRIM SONRASI BAŞA DÖNÜLDÜ. Yeni Baseline: {new_baseline:.4f}")

                        # Recalculate SL based on the new first-entry (and recreate SL order on exchange)
                        try:
                            new_sl = self.calculate_stop_loss_price(new_baseline, pos['direction'], symbol=symbol, sl_profile=pos.get('sl_profile'))
                            pos['stop_loss'] = new_sl
                            pos['trailing_sl'] = new_sl
                            # Cancel old SL order if exists
                            if self.exchange_adapter and pos.get('sl_order_id'):
                                try:
                                    self.exchange_adapter.cancel_order(pos.get('sl_order_id'), symbol)
                                except Exception:
                                    pass
                            # Create new SL order to match updated hard SL
                            if self.exchange_adapter:
                                try:
                                    oid = self.exchange_adapter.create_sl_order(symbol, new_sl, pos['position_size'], pos['direction'])
                                    if oid:
                                        pos['sl_order_id'] = oid
                                        print(f"   [OK] [{symbol}] SL güncellendi ve yeniden oluşturuldu: ${new_sl:.4f} (OID={oid})")
                                except Exception as e:
                                    print(f"   [WARNING] [{symbol}] SL yeniden oluşturma hatası: {e}")
                        except Exception as e:
                            print(f"   [WARNING] [{symbol}] Yeni SL hesaplama hatası: {e}")

    def update_trailing_stops(self, current_price: float, symbol: str):
        """Trailing stop seviyelerini güncelle (Kârı izle + Koruma)"""
        if not getattr(config, 'TRAILING_STOP_ENABLED', False):
            return

        for pos in self.positions:
            if pos['symbol'] == symbol:
                # Kâr Oranı Hesapla (% Net)
                profit_info = self.get_profit_info(current_price, pos)
                net_profit = profit_info['profit_percent']
                
                # --- [ULTRATHINK] BREAK-EVEN PROTECTION ---
                # Eğer kâr %0.6'yı geçerse, stopu giriş fiyatına (fee dahil) çek.
                # Bu sayede kârlı bir işlem asla zarara dönmez.
                if net_profit >= 0.6 and not pos.get('breakeven_triggered', False):
                    fees = self.calculate_fees(pos['entry_price'], current_price, pos['position_size'])
                    round_trip_fee_dist = fees['total_fee'] / pos['position_size']
                    
                    if pos['direction'] == "BUY":
                        pos['trailing_sl'] = pos['entry_price'] + round_trip_fee_dist
                    else:
                        pos['trailing_sl'] = pos['entry_price'] - round_trip_fee_dist
                        
                    pos['breakeven_triggered'] = True
                    print(f"🛡️ [{symbol}] BREAK-EVEN ACTIVE: Stop girişe çekildi.")

                # --- [ULTRATHINK] DELAYED TRAILING STOP ---
                # Sadece kâr %0.5'i geçince takip etmeye başla.
                # Daha düşük kârlarda fiyatın rahat "nefes almasına" izin ver.
                if net_profit < 0.5:
                    continue

                callback = getattr(config, 'TRAILING_STOP_CALLBACK', 0.5) / 100
                
                if pos['direction'] == "BUY":
                    if current_price > pos['peak_price']:
                        pos['peak_price'] = current_price
                        new_tsl = current_price * (1 - callback)
                        # Sadece stop yukarı gidiyorsa güncelle
                        if new_tsl > pos['trailing_sl']:
                            pos['trailing_sl'] = new_tsl
                else: # SELL (Short)
                    if current_price < pos['peak_price']:
                        pos['peak_price'] = current_price
                        new_tsl = current_price * (1 + callback)
                        # Sadece stop aşağı gidiyorsa güncelle
                        if new_tsl < pos['trailing_sl']:
                            pos['trailing_sl'] = new_tsl
    
    def close_position(self, position: Dict, exit_price: float, reason: str = "UNKNOWN", actual_exit_intent: str = None):
        """Close an open position and update balance"""
        # DUPLICATE GUARD: Check if this position was already closed
        if position not in self.positions:
            print(f"   [WARNING] [{position.get('symbol', 'UNKNOWN')}] Position already closed, skipping duplicate close")
            return
        
        # Create unique trade ID for duplicate detection
        trade_id = f"{position['symbol']}_{position.get('opened_at', 0):.0f}"
        if not hasattr(self, '_closed_trade_ids'):
            self._closed_trade_ids = set()
        
        if trade_id in self._closed_trade_ids:
            print(f"   [WARNING] [{position['symbol']}] Trade already recorded (ID: {trade_id}), skipping duplicate")
            # CRITICAL FIX: Ensure it is removed from active memory to prevent infinite loop
            if position in self.positions:
                self.positions.remove(position)
                print(f"   [CLEANUP] Removed duplicate position from memory: {position['symbol']}")
            return
        
        self._closed_trade_ids.add(trade_id)
        
        # Clean old IDs (keep last 100 to prevent memory leak)
        if len(self._closed_trade_ids) > 100:
            self._closed_trade_ids = set(list(self._closed_trade_ids)[-100:])
        
        symbol = position['symbol']
        
        # Intent-aware fee calculation
        entry_intent = position.get('entry_intent', 'TAKER')
        exit_intent = actual_exit_intent if actual_exit_intent else position.get('exit_intent', 'TAKER')
        
        # --- [ULTRATHINK] TAKER-FEE KILL SWITCH ---
        # If we intended MAKER but got TAKER, trigger a longer cooldown
        if position.get('exit_intent') == 'MAKER' and exit_intent == 'TAKER':
            print(f"   🚨 [{symbol}] TAKER-FEE DETECTED! (Intended MAKER). Cooldown applied.")
            self.cooldowns[symbol] = time.time() + 300 # 5 minute penalty
        else:
            self.cooldowns[symbol] = time.time() # Normal cooldown
        
        fees = self.calculate_fees(
            position["entry_price"], 
            exit_price, 
            position["position_size"],
            entry_intent=entry_intent,
            exit_intent=exit_intent
        )
        
        print(f"   💰 [{symbol}] Fee calculated with entry={entry_intent}, exit={exit_intent}, total_fee=${fees['total_fee']:.4f}")

        
        direction = position.get("direction", "BUY")
        if direction == "BUY":
            revenue = exit_price * position["position_size"]
            cost = position["entry_price"] * position["position_size"]
            profit = revenue - cost - fees["total_fee"]
        else:  # SELL (short)
            revenue = position["entry_price"] * position["position_size"]
            cost = exit_price * position["position_size"]
            profit = revenue - cost - fees["total_fee"]
        
        profit_percent = (profit / (position["entry_price"] * position["position_size"])) * 100
        
        # Historical trade record
        trade_record = {
            "symbol": symbol,
            "direction": direction,
            "entry_price": position["entry_price"],
            "exit_price": exit_price,
            "position_size": position["position_size"],
            "profit_usd": profit,
            "profit_percent": profit_percent,
            "fees_paid": fees["total_fee"],
            "reason": reason,
            "opened_at": position.get("opened_at", 0),
            "closed_at": time.time()
        }
        
        self.trade_history.append(trade_record)
        
        # --- [ULTRATHINK] VOLUME GUARDRAIL TRACKING ---
        if position.get('mode') == "VOLUME_SIDECAR":
            volume_trade = {'net_pnl': profit, 'time': time.time(), 'symbol': symbol}
            self.volume_daily_trades.append(volume_trade)
            self.volume_rolling_trades.append(volume_trade)
            guardrail_status = self.check_volume_guardrails()
            if not guardrail_status['enabled']:
                print(f"[WARNING]  VOLUME GUARDRAIL: {guardrail_status['reason']}")
        if config.SAVE_TRADES_TO_FILE:
            self._save_trade_to_csv(trade_record)
        
        # FIX BUG #21: Remove position from list to prevent memory leak
        if position in self.positions:
            self.positions.remove(position)
        
        # Update balance (for paper trading or local tracking)
        if config.PAPER_TRADING:
            # Return capital + profit/loss
            self.paper_balance_usd += position["entry_value_usd"] + profit          
            print(f"🔴 [PAPER TRADING] Closed {position['direction']} at ${exit_price:.2f} | Reason: {reason}")
            print(f"   Profit/Loss: ${profit:.2f} ({profit_percent:.2f}%)")
            print(f"   Fees Paid: ${fees['total_fee']:.2f}")
            print(f"   New Balance: ${self.paper_balance_usd:.2f}")
        else:
            # Real trading - balance tracking
            # For real trading, balance is updated by exchange, we just log
            print(f"[ENTRY] [REAL TRADING] Closed {position['direction']} at ${exit_price:.2f} | Reason: {reason}")
            print(f"   Profit/Loss: ${profit:.2f} ({profit_percent:.2f}%)")
            print(f"   Fees Paid: ${fees['total_fee']:.2f}")
    
    def _save_trade_to_csv(self, trade_record: Dict):
        """Save a single trade record to CSV file"""
        try:
            file_exists = os.path.exists(config.TRADE_LOG_FILE)
            
            with open(config.TRADE_LOG_FILE, 'a', newline='', encoding='utf-8') as f:
                fieldnames = [
                    'timestamp', 'symbol', 'direction', 'entry_price', 'exit_price', 
                    'position_size', 'profit_usd', 'profit_percent', 'fees_paid', 
                    'reason', 'hold_time_seconds'
                ]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                
                # Write header only if file is new
                if not file_exists:
                    writer.writeheader()
                
                # Calculate hold time
                hold_time = trade_record['closed_at'] - trade_record['opened_at']
                
                # Format data for CSV
                csv_row = {
                    'timestamp': datetime.fromtimestamp(trade_record['closed_at']).strftime('%Y-%m-%d %H:%M:%S'),
                    'symbol': trade_record['symbol'],
                    'direction': trade_record['direction'],
                    'entry_price': f"{trade_record['entry_price']:.4f}",
                    'exit_price': f"{trade_record['exit_price']:.4f}",
                    'position_size': f"{trade_record['position_size']:.6f}",
                    'profit_usd': f"{trade_record['profit_usd']:.2f}",
                    'profit_percent': f"{trade_record['profit_percent']:.2f}",
                    'fees_paid': f"{trade_record['fees_paid']:.2f}",
                    'reason': trade_record['reason'],
                    'hold_time_seconds': f"{hold_time:.0f}"
                }
                
                writer.writerow(csv_row)
                
        except Exception as e:
            print(f"[WARNING] CSV Log Error: {e}")

    def _load_history_from_csv(self):
        """Load trade history from CSV for persistence across restarts"""
        if not os.path.exists(config.TRADE_LOG_FILE):
            return
            
        try:
            with open(config.TRADE_LOG_FILE, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                loaded_trades = []
                for row in reader:
                    try:
                        loaded_trades.append({
                            'timestamp': row.get('timestamp'),
                            'symbol': row.get('symbol'),
                            'direction': row.get('direction'),
                            'entry_price': float(row.get('entry_price', 0) or 0),
                            'exit_price': float(row.get('exit_price', 0) or 0),
                            'position_size': float(row.get('position_size', 0) or 0),
                            'profit_usd': float(row.get('profit_usd', row.get('profit_loss', 0)) or 0),
                            'profit_percent': float(row.get('profit_percent', 0) or 0),
                            'reason': row.get('reason', 'N/A')
                        })
                    except Exception:
                        continue
                
                if loaded_trades:
                    self.trade_history = loaded_trades
                    print(f"[INFO] Loaded {len(loaded_trades)} trades from {config.TRADE_LOG_FILE}")
        except Exception as e:
            print(f"[WARNING] Could not load history from CSV: {e}")
    
    def get_profit_info(self, current_price: float, position: Dict) -> Dict:
        """Calculate P&L with correct maker/taker fees from position metadata"""
        entry_price = position['entry_price']
        size = position['position_size']
        side = position['direction']
        
        # Intent from position metadata (fallback to defaults)
        entry_intent = position.get('entry_intent', config.DEFAULT_ORDER_INTENT_PROFIT)
        exit_intent = position.get('exit_intent', config.DEFAULT_ORDER_INTENT_PROFIT)
        
        # Fees with intent
        fees = self.calculate_fees(entry_price, current_price, size, 
                                   entry_intent, exit_intent)
        
        # Gross P&L
        if side == "BUY":
            gross_pnl = (current_price - entry_price) * size
        else:
            gross_pnl = (entry_price - current_price) * size
        
        # Net P&L
        net_pnl = gross_pnl - fees['total_fee']
        net_pnl_pct = (net_pnl / (entry_price * size)) * 100 if size > 0 else 0
        
        return {
            'profit_percent': net_pnl_pct,
            'gross_profit_percent': (gross_pnl / (entry_price * size)) * 100,
            'net_pnl': net_pnl,
            'fees': fees,
            'is_target_hit': net_pnl_pct >= config.TARGET_PROFIT_PERCENT,
            'is_stop_loss_hit': net_pnl_pct <= -config.STOP_LOSS_PERCENT
        }

    def check_take_profit(self, current_price: float, position: Dict) -> bool:
        """
        Check if a position has reached the target profit percentage
        Include fees in the calculation.
        """
        fees = self.calculate_fees(position["entry_price"], current_price, position["position_size"])
        
        entry_cost = position["entry_price"] * position["position_size"] + fees["entry_fee"]
        exit_revenue = current_price * position["position_size"] - fees["exit_fee"]
        
        profit = exit_revenue - entry_cost
        profit_percent = (profit / entry_cost) * 100
        
        # Check against TARGET_PROFIT_PERCENT in config
        return profit_percent >= config.TARGET_PROFIT_PERCENT

    def check_stop_loss(self, current_price: float, symbol: str) -> Optional[Dict]:
        """Stop-loss (Hard & Trailing) kontrolü"""
        for position in self.positions:
            if position["symbol"] == symbol:
                # 1. Trailing Stop-Loss Kontrolü
                tsl = position.get("trailing_sl", position["stop_loss"])
                if position["direction"] == "BUY":
                    if current_price <= tsl:
                        position['close_reason'] = "TRAILING_STOP" if tsl > position["stop_loss"] else "HARD_STOP"
                        return position
                else: # SELL (Short)
                    if current_price >= tsl:
                        position['close_reason'] = "TRAILING_STOP" if tsl < position["stop_loss"] else "HARD_STOP"
                        return position
        return None
    
    def has_open_positions(self) -> bool:
        """Check if there are any open positions"""
        return len(self.positions) > 0
    
    def _apply_volume_guardrails(self):
        """Volume Sidecar Specific Kill-Switch & Throttling"""
        total_balance = self.get_balance()
        
        # 1. Equity Drawdown Check (10%)
        equity_dd_limit = total_balance * 0.10
        if self.volume_daily_pnl <= -equity_dd_limit:
            self.volume_throttle_level = 0.0
            print(f"🚨 [VOLUME GUARDRAIL] Equity DD reached 10%. Sidecar STOPPED.")
            return

        # 2. Rolling Loss Throttling
        if len(self.volume_realized_pnl_rolling) >= 5:
            losses = [p for p in self.volume_realized_pnl_rolling if p < 0]
            if len(losses) > len(self.volume_realized_pnl_rolling) * 0.7:
                self.volume_throttle_level = 0.5
                print(f"[WARNING] [VOLUME GUARDRAIL] 70% of last trades are losses. Throttling to 50%.")
            else:
                self.volume_throttle_level = 1.0

    def can_open_new_position(self, symbol: str = None, direction: str = "BUY", mode=None, is_layering: bool = False) -> bool:
        """Check budget and limits with dual-mode support."""
        total_balance = self.get_balance()
        if symbol and symbol in self.operation_locks:
            return False
            
        # Logic check: No ProfitCore trade if same symbol exists, unless layering
        if not is_layering:
            for pos in self.positions:
                if pos['symbol'] == symbol:
                    return False

        # 1. Overall Exposure Check (Disabled for Ghost Strategy)
        # We still keep a very high fallback to prevent total liquidations
        margin_in_use = sum((p['entry_price'] * p['position_size']) / config.LEVERAGE for p in self.positions)
        if margin_in_use >= self.get_balance() * 0.95: # 95% limit for extreme safety
            return False

        # 2. [ULTRATHINK] Mode-Specific Budget Check
        if mode == "PROFIT_CORE":
            pc_margin = sum((p['entry_price'] * p['position_size']) / config.LEVERAGE for p in self.positions if p.get('mode') == "PROFIT_CORE")
            limit = getattr(config, 'PROFIT_CORE_MAX_EXPOSURE_PCT', 20.0) / 100
            if pc_margin >= total_balance * limit:
                return False
        elif mode == "VOLUME_SIDECAR":
            vs_margin = sum((p['entry_price'] * p['position_size']) / config.LEVERAGE for p in self.positions if p.get('mode') == "VOLUME_SIDECAR")
            limit = getattr(config, 'VOLUME_SIDECAR_MAX_EXPOSURE_PCT', 15.0) / 100
            if vs_margin >= total_balance * limit or self.volume_throttle_level == 0.0:
                return False

        # 3. Correlation Guard (Bypassed for layering to ensure recovery)
        same_dir_count = sum(1 for pos in self.positions if pos['direction'] == direction)
        if not is_layering and same_dir_count >= getattr(config, 'MAX_SAME_DIRECTION_POSITIONS', 5):
            return False
        
        return True
        
    def lock_symbol(self, symbol: str):
        """Lock a symbol to prevent concurrent operations (FIX BUG #Q04)"""
        self.operation_locks.add(symbol)
        
    def unlock_symbol(self, symbol: str):
        """Unlock a symbol after operation is complete"""
        if symbol in self.operation_locks:
            self.operation_locks.remove(symbol)
            
    def is_locked(self, symbol: str) -> bool:
        """Check if symbol is currently locked"""
        return symbol in self.operation_locks
    
    def cleanup_old_data(self, keep_last_n_trades: int = None):
        """
        Clean up old trade history to free memory.
        
        CRITICAL: This NEVER touches active positions or balance!
        Only removes old completed trades from memory.
        
        Args:
            keep_last_n_trades: How many recent trades to keep (None = use config)
        """
        if keep_last_n_trades is None:
            keep_last_n_trades = config.KEEP_TRADE_HISTORY_COUNT
        
        # Nothing to clean if we're under the limit
        if len(self.trade_history) <= keep_last_n_trades:
            return
        
        # Calculate how many trades to archive
        trades_to_archive = len(self.trade_history) - keep_last_n_trades
        old_trades = self.trade_history[:trades_to_archive]
        
        # Archive to CSV if enabled
        if config.ARCHIVE_TRADES_TO_FILE and old_trades:
            try:
                import csv
                import os
                
                file_exists = os.path.exists(config.TRADE_ARCHIVE_FILE)
                
                # Use consistent fieldnames (same as trades.csv)
                archive_fieldnames = [
                    'timestamp', 'symbol', 'direction', 'entry_price', 'exit_price', 
                    'position_size', 'profit_usd', 'profit_percent', 'reason'
                ]
                
                with open(config.TRADE_ARCHIVE_FILE, 'a', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=archive_fieldnames, extrasaction='ignore')
                    
                    # Write header only if file is new
                    if not file_exists:
                        writer.writeheader()
                    
                    # Format each trade for archive
                    for trade in old_trades:
                        archive_row = {
                            'timestamp': trade.get('timestamp', datetime.fromtimestamp(trade.get('closed_at', time.time())).strftime('%Y-%m-%d %H:%M:%S')),
                            'symbol': trade.get('symbol', ''),
                            'direction': trade.get('direction', ''),
                            'entry_price': f"{trade.get('entry_price', 0):.4f}",
                            'exit_price': f"{trade.get('exit_price', 0):.4f}",
                            'position_size': f"{trade.get('position_size', 0):.6f}",
                            'profit_usd': f"{trade.get('profit_usd', 0):.2f}",
                            'profit_percent': f"{trade.get('profit_percent', 0):.2f}",
                            'reason': trade.get('reason', 'UNKNOWN')
                        }
                        writer.writerow(archive_row)
                
                print(f"[ARCHIVE]  {trades_to_archive} eski işlem arşivlendi: {config.TRADE_ARCHIVE_FILE}")
            except Exception as e:
                print(f"[WARNING]  Arşivleme hatası: {e}")
        
        # Keep only recent trades in memory
        self.trade_history = self.trade_history[trades_to_archive:]
        
        # Update cleanup time
        self.last_cleanup_time = time.time()
        
        print(f"[CLEANUP] Hafıza temizlendi: {trades_to_archive} eski işlem kaldırıldı")
        print(f"   Bellekte kalan: {len(self.trade_history)} işlem")
        print(f"   Aktif pozisyonlar: {len(self.positions)} (korunuyor [OK])")
    
    def should_run_cleanup(self) -> bool:
        """Check if it's time to run cleanup based on interval OR trade count
        
        Uses dual triggers:
        1. Time-based: Every CLEANUP_INTERVAL_HOURS
        2. Count-based: When trade history reaches KEEP_TRADE_HISTORY_COUNT
        
        This ensures cleanup runs even if bot restarts frequently.
        """
        elapsed_hours = (time.time() - self.last_cleanup_time) / 3600
        time_trigger = elapsed_hours >= config.CLEANUP_INTERVAL_HOURS
        
        # Count trigger: If we have too many trades in memory
        count_trigger = len(self.trade_history) >= config.KEEP_TRADE_HISTORY_COUNT
        
        return time_trigger or count_trigger
    
    def print_summary(self):
        """Print trading summary"""
        print("\n" + "="*60)
        print("[INFO] TRADING SUMMARY")
        print("="*60)
        print(f"Mode: {'PAPER TRADING (SAFE)' if config.PAPER_TRADING else 'REAL MONEY (LIVE)'}")
        print(f"Current Balance: ${self.get_balance():.2f}")
        print(f"Open Positions: {len(self.positions)}")
        print(f"Total Trades: {len(self.trade_history)}")
        
        if self.trade_history:
            # FIX: Use profit_usd instead of profit_loss
            total_profit = sum(t.get("profit_usd", t.get("profit_loss", 0)) for t in self.trade_history)
            winning_trades = sum(1 for t in self.trade_history if t.get("profit_usd", t.get("profit_loss", 0)) > 0)
            win_rate = (winning_trades / len(self.trade_history)) * 100
            
            print(f"Total Profit/Loss: ${total_profit:.2f}")
            print(f"Win Rate: {win_rate:.1f}% ({winning_trades}/{len(self.trade_history)})")
        
        print("="*60 + "\n")

    # ========== PENDING ORDERS MANAGEMENT (GHOST PROFESSIONAL) ==========
    
    def create_pending_layer_order(self, symbol: str, level: float, amount_usd: float, order_type: str = "ADD"):
        """
        Fiyat belirli seviyeye düşerse/yükselirse otomatik emir gir (Pending State'te bekle)
        
        Args:
            symbol: Trading pair (e.g., 'BTC/USDT')
            level: Fiyat seviyesi (emir bu seviyeye ulaştığında aktif olacak)
            amount_usd: Marjin miktarı (USDT)
            order_type: 'ADD' (kademeli ekleme) veya 'TRIM' (kısmi çıkış)
        """
        if symbol not in self.pending_orders:
            self.pending_orders[symbol] = {'orders': []}
        
        pending_order = {
            'level': level,
            'amount_usd': amount_usd,
            'type': order_type,
            'placed': False,
            'order_id': None,
            'created_at': time.time()
        }
        
        self.pending_orders[symbol]['orders'].append(pending_order)
        print(f"   ⏳ [PENDING] {symbol}: {order_type} ${amount_usd:.2f} @ {level:.4f}")
    
    def get_pending_orders(self, symbol: str) -> list:
        """Sembol için beklemede olan emirleri döndür"""
        return self.pending_orders.get(symbol, {}).get('orders', [])
    
    def clear_pending_order(self, symbol: str, index: int):
        """Pending emirden kaldır (order filled veya cancelled)"""
        if symbol in self.pending_orders and index < len(self.pending_orders[symbol]['orders']):
            self.pending_orders[symbol]['orders'].pop(index)
    
    def clear_all_pending_orders(self, symbol: str):
        """Sembolün tüm pending emirlerini iptal et"""
        if symbol in self.pending_orders:
            self.pending_orders[symbol]['orders'].clear()
            print(f"   [OK] [{symbol}] Tüm pending emirler iptal edildi")

    # ========== DYNAMIC TP CALCULATION (GHOST PROFESSIONAL) ==========
    
    def cancel_pending_orders_on_tp_hit(self, symbol: str):
        """
        TP aktif olunca (işlem kapandı) tüm pending orderları iptal et.
        Senaryo: Fiyat TP'ye ulaştı → işlem kapandı → pending emirler artık geçersiz
        """
        self.clear_all_pending_orders(symbol)
        print(f"   🛑 [{symbol}] TP aktif - Tüm pending emirler iptal edildi")
    
    def calculate_average_entry_price(self, symbol: str) -> float:
        """
        Sembol için ortalama giriş fiyatını hesapla (tüm kademeler birlikte)
        
        Örnek:
        - Tier 1: 100 USDT @ $100 → Toplam: $10,000
        - Tier 2: 5 USDT @ $98 → Toplam: $490
        - Toplam Büyüklük: 10 + 5 = 15 USDT
        - Ortalama Giriş: $10,490 / 15 = $699.33
        """
        layers = self.position_layers.get(symbol, [])
        if not layers:
            return 0.0
        
        total_value_usd = 0.0
        total_size_usd = 0.0
        
        for layer in layers:
            price = layer.get('price', 0)
            size_usd = layer.get('size_usd', 0)
            
            # Eğer size_usd yoksa, position_size olarak depolanmışsa
            if not size_usd and 'size' in layer:
                # Coin cinsinden size varsa, fiyatla çarp
                size_usd = layer['size'] * price
            
            total_value_usd += price * size_usd
            total_size_usd += size_usd
        
        if total_size_usd == 0:
            return 0.0
        
        avg_price = total_value_usd / total_size_usd
        return avg_price
    
    def calculate_dynamic_tp(self, symbol: str, direction: str = "BUY", tp_percent: float = 0.01) -> float:
        """
        Ortalama giriş fiyatına göre dinamik TP hesapla
        
        Args:
            symbol: Trading pair
            direction: 'BUY' (long) veya 'SELL' (short)
            tp_percent: Kâr yüzdesi (0.01 = %1.0)
        
        Döner: TP seviyesi
        """
        avg_entry = self.calculate_average_entry_price(symbol)
        if avg_entry == 0:
            return 0.0
        
        if direction == "BUY":
            tp = avg_entry * (1 + tp_percent)
        else:  # SELL
            tp = avg_entry * (1 - tp_percent)
        
        return tp
    
    def get_active_position(self, symbol: str) -> Optional[Dict]:
        """Sembol için aktif pozisyonu bul"""
        return next((p for p in self.positions if p['symbol'] == symbol), None)

    # ========== PARTIAL CLOSE / TRIM SYSTEM ==========
    
    def should_trim_at_cost(self, symbol: str, current_price: float, direction: str = "BUY") -> bool:
        """
        Ortalama giriş fiyatına döndük mi kontrol et.
        Döndüysek, eklediğimiz kademeleri çıkartmalıyız.
        
        Senaryo:
        - Girdik: 100 @ 10 USDT
        - 2. Tier: 98 @ 5 USDT
        - 3. Tier: 97 @ 5 USDT
        - Ortalama: 99.33 USDT
        
        Fiyat 99.33'e döndüğünde:
        - 3. Tier (97 @ 5 USDT) ve 2. Tier (98 @ 5 USDT) çıkart
        - İlk Tier (100 @ 10 USDT) TP için kalsın
        """
        layers = self.position_layers.get(symbol, [])
        if len(layers) <= 1:
            return False  # Sadece ilk giriş varsa trim yapma
        
        avg_entry = self.calculate_average_entry_price(symbol)
        
        tolerance = avg_entry * 0.001  # %0.1 tolerance
        
        if direction == "BUY":
            # Long: fiyat ortalama fiyata yaklaştı mı?
            return current_price >= (avg_entry - tolerance) and current_price <= (avg_entry + tolerance)
        else:  # SELL
            # Short: fiyat ortalama fiyata yaklaştı mı?
            return current_price <= (avg_entry + tolerance) and current_price >= (avg_entry - tolerance)
    
    def trim_excess_layers(self, symbol: str) -> bool:
        """
        Fiyat ortalamaya döndüğünde, eklediğimiz son kademeleri çıkart
        (İlk giriş hariç)
        
        ÖNEMLİ: Trim sonrası kalan 10 USDT (ilk giriş) için TP yeniden hesaplanmalı!
        
        Senaryo:
        - Girdik: 100 @ 10 USDT
        - Ekleme: 98 @ 5 USDT (Ortalama: 99.33)
        - Trim @99.33'de: Son 5 USDT çıkartıldı
        - Kalan: 10 USDT @ 99.33'de
        - Yeni TP: 99.33 * 1.01 = 100.33
        """
        layers = self.position_layers.get(symbol, [])
        if len(layers) <= 1:
            return False
        
        # Son eklenen kademeleri çıkart (ters sıradan başla)
        trimmed_count = len(layers) - 1  # İlk giriş hariç hepsini çıkart
        
        # TODO: Gerçek close emrini gönder
        # self.exchange_adapter.close_positions(symbol, position_size=trimmed_size)
        
        # Artık sadece ilk giriş kalsın, ama ortalama giriş fiyatı güncellenmiş olabilir
        avg_at_trim = self.calculate_average_entry_price(symbol)
        
        # [GHOST FIX] After trim, the ONLY remaining layer is the first one, 
        # but its baseline (first_entry_price) MUST BE RESET to the price where we started after trim.
        # This allows recursive layering based on the "New Home" price.
        self.position_layers[symbol] = [{
            'price': avg_at_trim,  
            'size': layers[0]['size'],
            'time': layers[0]['time'],
            'first_entry_price': avg_at_trim  # ← NEW BASELINE RESET
        }]
        
        # Pozisyon yapısını da güncelle
        pos = next((p for p in self.positions if p['symbol'] == symbol), None)
        if pos:
            pos['entry_price'] = avg_at_trim
            pos['position_size'] = layers[0]['size']
            pos['first_entry_price'] = avg_at_trim # ← NEW BASELINE RESET on position too
        
        print(f"   [TRIM] [TRIM] {symbol}: {trimmed_count} kademe(s) çıkartıldı. Yeni Başlangıç (Baseline): {avg_at_trim:.4f}'de devam ediyor.")
        return True

    # ========== RECURSIVE LAYER MANAGEMENT ==========
    
    def get_next_layer_level(self, symbol: str, current_price: float, direction: str = "BUY", 
                            layer_threshold_pct: float = 0.02) -> Optional[float]:
        """
        Fiyat belirli mesafeye düşerse/yükselirse sonraki kademe seviyesini belirle
        
        ÖNEMLİ: Temel SANINIZ İLK GİRİŞ FİYATI, ORTALAMA DEĞİL!
        
        Senaryo:
        - İlk giriş: 100 USDT @ $100
        - Trim @99.33'de (2. ve 3. kademe çıkartıldı)
        - Fiyat 98'e düştü
        - Yeni kademe: 100 * (1 - 0.02) = 98 USDT ← İlk fiyata göre -2%
        
        Args:
            symbol: Trading pair
            current_price: Şu anki fiyat
            direction: 'BUY' (long) veya 'SELL' (short)
            layer_threshold_pct: Tier seviyesi (0.02 = %2)
        
        Döner: Sonraki kademe seviyesi
        """
        layers = self.position_layers.get(symbol, [])
        if not layers:
            return None
        
        # TEMELİ İLK GİRİŞ FİYATINA SABİT
        first_entry_price = layers[0].get('first_entry_price', layers[0]['price'])
        
        if direction == "BUY":
            # Long: fiyat kaç katman aşağıda?
            distance_pct = (first_entry_price - current_price) / first_entry_price
            
            if distance_pct >= layer_threshold_pct * 2:  # 4. Tier seviyesi (-4%)
                next_level = first_entry_price * (1 - layer_threshold_pct * 2)
            elif distance_pct >= layer_threshold_pct:  # 2-3. Tier seviyesi (-2%)
                next_level = first_entry_price * (1 - layer_threshold_pct)
            else:
                next_level = None
        else:  # SELL
            # Short: fiyat kaç katman yukarıda?
            distance_pct = (current_price - first_entry_price) / first_entry_price
            
            if distance_pct >= layer_threshold_pct * 2:
                next_level = first_entry_price * (1 + layer_threshold_pct * 2)
            elif distance_pct >= layer_threshold_pct:
                next_level = first_entry_price * (1 + layer_threshold_pct)
            else:
                next_level = None
        
        return next_level
    
    def schedule_recursive_layer(self, symbol: str, current_price: float, direction: str = "BUY"):
        """
        Sonraki kademe seviyesine otomatik emir koy (pending state'te bekle)
        """
        next_level = self.get_next_layer_level(symbol, current_price, direction)
        
        if next_level is None:
            return
        
        # Hangi kademe miktarı?
        layers = self.position_layers.get(symbol, [])
        layer_count = len(layers)
        
        if layer_count == 1:
            amount = 5.0  # 2. Tier
        elif layer_count == 2:
            amount = 5.0  # 3. Tier
        else:
            amount = 10.0  # 4+ Tier
        
        self.create_pending_layer_order(symbol, next_level, amount, order_type="ADD")






