"""Shared Market metadata and objective HKO observation types."""

from dataclasses import dataclass, field

from typing import Optional

@dataclass(frozen=True)
class OutcomeMetadata:
    raw_name: str
    normalized_name: str
    bucket_type: str
    temperature_value: Optional[float] = None
    threshold: Optional[float] = None

@dataclass(frozen=True)
class Outcome:
    name: str
    current_price: float
    target_payout: float
    current_payout_held: float
    role: str
    death_line_c: Optional[float] = None
    outcome_temp_c: Optional[float] = None
    avg_entry_price: Optional[float] = None
    price_source: str = ""
    price_warning: str = ""
    market_id: str = ""
    condition_id: str = ""

@dataclass(frozen=True)
class HKOWeather:
    update_time: str
    current_temperature: Optional[float]
    today_recorded_high: Optional[float]
    forecast_high: Optional[float]
    weather_description: str
    rainfall_or_warning_info: str = ""
    raw_saved_path: str = ""
    recorded_high_csv_timestamp: Optional[str] = None
    recorded_high_station: Optional[str] = None
    recorded_high_raw_value: Optional[str] = None
    recorded_high_csv_error: str = ""
    special_weather_tips: str = ""
    warning_info_text: str = ""
    active_warning_codes: list[str] = field(default_factory=list)
