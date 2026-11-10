"""Installation settings: Home Assistant address and token, entity IDs, switches.

Loaded once at import into ``os.environ`` from ``.env`` (git-ignored: local
values and secrets), then ``.env.defaults`` (committed: every key).  A
variable already set in the process environment wins over both files, and
``.env`` wins over ``.env.defaults``.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).parent
load_dotenv(_HERE / ".env")
load_dotenv(_HERE / ".env.defaults")


def env_flag(name: str) -> bool:
    """Boolean switch from the environment, read at call time."""
    return os.environ.get(name, "").strip().lower() in ("true", "1", "yes", "on")


HA_URL = os.environ["HA_URL"]
HA_TOKEN = os.environ.get("HA_TOKEN", "")

PRICE_TODAY_ENTITY = os.environ["PRICE_TODAY_ENTITY"]
PRICE_TOMORROW_ENTITY = os.environ["PRICE_TOMORROW_ENTITY"]

BATTERY_VOLTAGE_ENTITY = os.environ["BATTERY_VOLTAGE_ENTITY"]
INVERTER_OUTPUT_POWER_ENTITY = os.environ["INVERTER_OUTPUT_POWER_ENTITY"]
PV_ENERGY_TOTAL_ENTITY = os.environ["PV_ENERGY_TOTAL_ENTITY"]
PV_ENERGY_TODAY_ENTITY = os.environ["PV_ENERGY_TODAY_ENTITY"]
BMS_SOC_ENTITY = os.environ["BMS_SOC_ENTITY"]

SOLAR_FORECAST_TODAY_ENTITY = os.environ["SOLAR_FORECAST_TODAY_ENTITY"]
SOLAR_FORECAST_TOMORROW_ENTITY = os.environ["SOLAR_FORECAST_TOMORROW_ENTITY"]
SOLAR_FORECAST_D2_ENTITY = os.environ["SOLAR_FORECAST_D2_ENTITY"]

# Day offset (0 = today, 1 = tomorrow) → end-of-day SOC target helper.
SOC_TARGET_ENTITIES = {
    0: os.environ["SOC_TARGET_TODAY_ENTITY"],
    1: os.environ["SOC_TARGET_TOMORROW_ENTITY"],
}
