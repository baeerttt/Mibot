"""
fees.py — modelo de comisiones de Polymarket (fuente unica de verdad).

Polymarket cambio su esquema el 2026-03-23: dejo el viejo 2% sobre ganancias y
paso a un FEE POR TRADE, por categoria, que paga el TAKER (el que cruza el spread).
Mibot siempre es taker (compra al ask) -> paga el fee completo, sin rebate de maker.

Formula oficial del taker fee (USDC) por trade:

    fee = shares * p * feeRate * (p * (1 - p)) ** exponent

donde p = precio del contrato (0..1). Para CRIPTO: feeRate=0.072, exponent=1.

Propiedad clave: el termino p*(1-p) hace que el fee como % del notional sea
MAXIMO en p=0.50 (1.80%) y caiga a ~0 en los extremos. Operar cerca de la moneda
al aire (tipico de los mercados 5m/15m) es lo MAS caro:

    effective_rate(0.50) = 1.80%   effective_rate(0.70)=1.51%   effective_rate(0.90)=0.65%

Esto da una palanca real de rentabilidad: exigir mas edge cuanto mas cerca de 0.50
esta el precio, porque ahi el fee se come la ventaja.

Referencia: Polymarket fee schedule, marzo 2026 (categoria cripto = la mas cara).
"""
import config

# Tasa y exponente por categoria. Mibot opera SOLO cripto, la categoria mas cara.
RATE_CRYPTO = getattr(config, "POLYMARKET_FEE_RATE", 0.072)
EXP_CRYPTO = getattr(config, "POLYMARKET_FEE_EXPONENT", 1.0)


def effective_rate(price: float, rate: float = RATE_CRYPTO, exp: float = EXP_CRYPTO) -> float:
    """Fee como FRACCION del notional invertido (USDC gastado), a precio `price`.
    Ej: effective_rate(0.5) -> 0.018 (1.80%)."""
    p = max(0.0, min(1.0, price))
    return rate * (p * (1.0 - p)) ** exp


def fee_per_share(price: float, rate: float = RATE_CRYPTO, exp: float = EXP_CRYPTO) -> float:
    """Costo del fee EN UNIDADES DE PRECIO por share comprada.
    Sirve para sumarlo al ask en el filtro de edge / sizing (mismas unidades que el
    edge). fee_per_share(p) = p * effective_rate(p)."""
    p = max(0.0, min(1.0, price))
    return p * effective_rate(p, rate, exp)


def taker_fee(shares: float, price: float,
              rate: float = RATE_CRYPTO, exp: float = EXP_CRYPTO) -> float:
    """Fee total en USDC de un trade de `shares` shares a precio `price`."""
    return shares * fee_per_share(price, rate, exp)


if __name__ == "__main__":
    # tabla de referencia rapida
    print("precio  fee%notional  fee$/share")
    for p in (0.10, 0.30, 0.50, 0.70, 0.90):
        print(f"  {p:.2f}     {effective_rate(p)*100:5.2f}%      {fee_per_share(p):.5f}")
    print(f"\nEjemplo oficial: 100 shares @ 0.50 -> fee ${taker_fee(100, 0.50):.2f} (debe dar 0.90)")
