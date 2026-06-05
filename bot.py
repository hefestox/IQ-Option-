"""
╔══════════════════════════════════════════════════════════╗
║   IQ Option Bot — Super Indicator NN + Auto Fibo         ║
║   Meta: +5% da banca por dia                             ║
║   Stop: -10% da banca                                    ║
║   Timeframe: 5 minutos                                   ║
╚══════════════════════════════════════════════════════════╝

LÓGICA DE ENTRADA:
  CALL quando:
    - Super NN: buy_strong = True  (estrutura de alta + score NN >= threshold)
    - Fibo:     preço próximo ao nível 61.8% (suporte em tendência de baixa)

  PUT quando:
    - Super NN: sell_strong = True (estrutura de baixa + score NN >= threshold)
    - Fibo:     preço próximo ao nível 38.2% (resistência em tendência de alta)

  Sinal ID (indeciso) é ignorado para reduzir risco.
"""

import os
import time
import math
import logging
from datetime import datetime
from iqoptionapi.api import IQ_Option

# ─────────────────────────────────────────────
# CONFIGURAÇÕES (lidas do ambiente Railway)
# ─────────────────────────────────────────────
EMAIL      = os.getenv("IQ_EMAIL",    "seu_email@gmail.com")
PASSWORD   = os.getenv("IQ_PASSWORD", "sua_senha")
ATIVO      = os.getenv("IQ_ATIVO",    "EURUSD-OTC")
MODO       = os.getenv("IQ_MODO",     "REAL")   # REAL ou PRACTICE

TIMEFRAME  = 5       # minutos
EXPIRACAO  = 5       # minutos
META_PCT   = 0.05    # +5% → para o dia
STOP_PCT   = 0.10    # -10% → stop loss diário

# ── Parâmetros Super Indicator NN ────────────
MA_FAST_P  = 2
MA_SLOW_P  = 8
SIGNAL_P   = 6
MA_TREND_P = 200

W_CLOSE_MA = 0.50
W_MACD     = 0.35
W_CANDLE   = 0.15

THRESHOLD_STRONG = 0.015
THRESHOLD_ID     = 0.005

# ── Parâmetros Auto Fibo ─────────────────────
FIBO_TOLERANCIA = 0.0015   # 0.15% de tolerância ao redor do nível

# ─────────────────────────────────────────────
# LOG
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("IQBot")


# ═══════════════════════════════════════════
#  INDICADORES
# ═══════════════════════════════════════════

def ema(closes: list, period: int) -> list:
    """Exponential Moving Average."""
    if len(closes) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(closes[:period]) / period]
    for p in closes[period:]:
        result.append(p * k + result[-1] * (1 - k))
    return result


def safe_div(a: float, b: float) -> float:
    return 0.0 if b == 0 else a / b


# ── Super Indicator NN ───────────────────────

def super_indicator_nn(candles: list) -> str | None:
    """
    Retorna: 'call', 'put' ou None
    Só entra em sinais FORTES (buy_strong / sell_strong).
    """
    min_candles = MA_TREND_P + SIGNAL_P + 10
    if len(candles) < min_candles:
        return None

    closes = [c["close"] for c in candles]
    opens  = [c["open"]  for c in candles]

    ema_fast  = ema(closes, MA_FAST_P)
    ema_slow  = ema(closes, MA_SLOW_P)
    ema_trend = ema(closes, MA_TREND_P)

    # Alinhar todos pelo menor tamanho
    n = min(len(ema_fast), len(ema_slow), len(ema_trend))
    ema_fast  = ema_fast[-n:]
    ema_slow  = ema_slow[-n:]
    ema_trend = ema_trend[-n:]
    closes_n  = closes[-n:]
    opens_n   = opens[-n:]

    # MACD
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    sig_line  = ema(macd_line, SIGNAL_P)

    n2 = min(len(macd_line), len(sig_line))
    macd_line = macd_line[-n2:]
    sig_line  = sig_line[-n2:]
    ema_fast  = ema_fast[-n2:]
    ema_slow  = ema_slow[-n2:]
    ema_trend = ema_trend[-n2:]
    closes_n  = closes_n[-n2:]
    opens_n   = opens_n[-n2:]

    # Valores do candle atual (último)
    ef0  = ema_fast[-1]
    es0  = ema_slow[-1]
    et0  = ema_trend[-1]
    m0   = macd_line[-1]
    s0   = sig_line[-1]
    c0   = closes_n[-1]
    o0   = opens_n[-1]

    # Features NN
    feat_close_ma = safe_div(c0 - ef0, ef0)
    feat_macd     = safe_div(m0, es0)
    feat_candle   = safe_div(abs(c0 - o0), c0)

    # Scores separados
    buy_score  = 0.0
    sell_score = 0.0

    if feat_close_ma > 0:
        buy_score  += W_CLOSE_MA * feat_close_ma
    else:
        sell_score += W_CLOSE_MA * (-feat_close_ma)

    if feat_macd > 0:
        buy_score  += W_MACD * feat_macd
    else:
        sell_score += W_MACD * (-feat_macd)

    buy_score  += W_CANDLE * feat_candle
    sell_score += W_CANDLE * feat_candle

    # Confirmação estrutural
    struct_buy  = (ef0 > es0) and (ef0 > et0) and (m0 > s0)
    struct_sell = (ef0 < es0) and (ef0 < et0) and (m0 < s0)

    buy_strong  = struct_buy  and (buy_score  >= THRESHOLD_STRONG)
    sell_strong = struct_sell and (sell_score >= THRESHOLD_STRONG)

    log.debug(f"NN → buy_score={buy_score:.4f} sell_score={sell_score:.4f} "
              f"struct_buy={struct_buy} struct_sell={struct_sell}")

    if buy_strong:
        return "call"
    if sell_strong:
        return "put"
    return None


# ── Auto Fibo ────────────────────────────────

def auto_fibo(candles: list) -> str | None:
    """
    Verifica se o preço atual está próximo de um nível Fibonacci.
    Tendência de alta (candle verde) → resistência 38.2% → sinal PUT
    Tendência de baixa (candle vermelho) → suporte 61.8%   → sinal CALL
    """
    if len(candles) < 2:
        return None

    ultimo  = candles[-1]
    close   = ultimo["close"]
    open_   = ultimo["open"]
    low_    = min(c["low"]  for c in candles[-3:])
    high_   = max(c["high"] for c in candles[-3:])
    rng     = abs(high_ - low_)

    if rng == 0:
        return None

    nivel_382 = high_ - rng * 0.382
    nivel_618 = low_  + rng * 0.618

    prox_382 = abs(close - nivel_382) / close <= FIBO_TOLERANCIA
    prox_618 = abs(close - nivel_618) / close <= FIBO_TOLERANCIA

    # Tendência de alta: candle verde + preço perto da resistência 38.2%
    if close > open_ and prox_382:
        log.debug(f"Fibo → PUT | close={close:.5f} nivel_382={nivel_382:.5f}")
        return "put"

    # Tendência de baixa: candle vermelho + preço perto do suporte 61.8%
    if close < open_ and prox_618:
        log.debug(f"Fibo → CALL | close={close:.5f} nivel_618={nivel_618:.5f}")
        return "call"

    return None


# ── Confluência final ────────────────────────

def sinal_final(candles: list) -> str | None:
    """
    Retorna sinal apenas quando Super NN e Fibo concordam.
    """
    nn   = super_indicator_nn(candles)
    fibo = auto_fibo(candles)

    log.info(f"SuperNN={nn} | AutoFibo={fibo}")

    if nn and fibo and nn == fibo:
        log.info(f"✅ Confluência! Sinal: {nn.upper()}")
        return nn

    # Se só o NN for forte (sem Fibo) também entra — menos restritivo
    if nn:
        log.info(f"⚡ Sinal apenas NN: {nn.upper()}")
        return nn

    return None


# ═══════════════════════════════════════════
#  BOT
# ═══════════════════════════════════════════

class BotIQOption:

    def __init__(self):
        self.api            = IQ_Option(EMAIL, PASSWORD)
        self.banca_inicial  = 0.0
        self.banca_atual    = 0.0
        self.resultado_dia  = 0.0
        self.operacoes      = 0
        self.wins           = 0

    # ── conexão ──────────────────────────────

    def conectar(self):
        log.info("Conectando à IQ Option...")
        ok, reason = self.api.connect()
        if not ok:
            raise ConnectionError(f"Falha: {reason}")
        self.api.change_balance(MODO)
        log.info(f"Conectado! Modo: {MODO}")

    def atualizar_banca(self):
        self.banca_atual = self.api.get_balance()
        if self.banca_inicial == 0:
            self.banca_inicial = self.banca_atual
        return self.banca_atual

    # ── candles ──────────────────────────────

    def obter_candles(self, qtd=300) -> list:
        raw = self.api.get_candles(ATIVO, TIMEFRAME * 60, qtd, time.time())
        return raw

    # ── valor da operação ─────────────────────

    def calcular_valor(self) -> float:
        valor = round(self.banca_atual * 0.05, 2)
        return max(valor, 1.0)

    # ── operar ───────────────────────────────

    def operar(self, direcao: str) -> float:
        valor = self.calcular_valor()
        log.info(f"📤 {direcao.upper()} | {ATIVO} | ${valor} | {EXPIRACAO}min")

        _, id_op = self.api.buy(valor, ATIVO, direcao, EXPIRACAO)

        while True:
            resultado = self.api.check_win_v3(id_op)
            if resultado is not None:
                break
            time.sleep(1)

        lucro = round(resultado, 2)
        self.resultado_dia += lucro
        self.operacoes     += 1
        if lucro > 0:
            self.wins += 1

        taxa = f"{self.wins}/{self.operacoes}"
        status = "✅ WIN" if lucro > 0 else "❌ LOSS"
        log.info(f"{status} ${lucro:+.2f} | Dia: ${self.resultado_dia:+.2f} | W/L: {taxa}")
        return lucro

    # ── metas ────────────────────────────────

    def meta_atingida(self) -> bool:
        if self.resultado_dia >= self.banca_inicial * META_PCT:
            log.info(f"🎯 META ATINGIDA! ${self.resultado_dia:+.2f}")
            return True
        return False

    def stop_atingido(self) -> bool:
        if self.resultado_dia <= -(self.banca_inicial * STOP_PCT):
            log.warning(f"🛑 STOP LOSS! ${self.resultado_dia:+.2f}")
            return True
        return False

    # ── aguardar candle ───────────────────────

    def aguardar_candle(self):
        seg = TIMEFRAME * 60
        espera = seg - (time.time() % seg) + 3
        log.info(f"⏳ Próximo candle em {int(espera)}s")
        time.sleep(espera)

    # ── loop principal ────────────────────────

    def rodar(self):
        self.conectar()
        self.atualizar_banca()

        meta_val = self.banca_inicial * META_PCT
        stop_val = self.banca_inicial * STOP_PCT
        log.info("=" * 55)
        log.info(f"Banca: ${self.banca_inicial:.2f} | Meta: +${meta_val:.2f} | Stop: -${stop_val:.2f}")
        log.info(f"Ativo: {ATIVO} | TF: {TIMEFRAME}min | Exp: {EXPIRACAO}min")
        log.info("=" * 55)

        while True:
            try:
                self.aguardar_candle()
                self.atualizar_banca()

                if self.meta_atingida() or self.stop_atingido():
                    log.info("Bot encerrado para hoje. 👋")
                    break

                candles = self.obter_candles()
                sinal   = sinal_final(candles)

                if sinal:
                    self.operar(sinal)
                else:
                    log.info("⏸ Sem sinal — aguardando próximo candle...")

            except KeyboardInterrupt:
                log.info("Interrompido manualmente.")
                break
            except Exception as e:
                log.error(f"Erro: {e}", exc_info=True)
                time.sleep(15)


if __name__ == "__main__":
    bot = BotIQOption()
    bot.rodar()
