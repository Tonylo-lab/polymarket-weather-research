"""Pure fee reservation arithmetic only; the live gateway is excluded."""
from decimal import Decimal,ROUND_CEILING
UNIT=1_000_000


def fee_cap(quantity_raw,rate,exponent):
    q=Decimal(quantity_raw)/UNIT
    return int((q*rate*Decimal('.25')**exponent*UNIT).to_integral_value(rounding=ROUND_CEILING))+1000
