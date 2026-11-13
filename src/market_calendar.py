"""Pure event naming; calendar discovery and time-based runners excluded."""
from datetime import date
MONTH_SLUGS={1:'january',2:'february',3:'march',4:'april',5:'may',6:'june',7:'july',8:'august',9:'september',10:'october',11:'november',12:'december'}


def event_slug_for_date(target: date) -> str:
    month = MONTH_SLUGS[target.month]
    return f"highest-temperature-in-hong-kong-on-{month}-{target.day}-{target.year}"
