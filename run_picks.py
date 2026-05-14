"""
Standalone picks runner — use this for Railway Cron Service or manual testing.

  python run_picks.py
"""

import logging
import os
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from picks_generator import run_daily_picks

if __name__ == "__main__":
    run_daily_picks()
