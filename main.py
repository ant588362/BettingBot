"""
Entry point — FastAPI server + APScheduler for daily picks at 10am ET.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import anthropic
import pytz
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from api import router, set_clients
from odds_client import OddsClient
from picks_generator import run_daily_picks


async def _scheduled_picks():
    await asyncio.to_thread(run_daily_picks)


@asynccontextmanager
async def lifespan(app: FastAPI):
    odds_key = os.getenv("ODDS_API_KEY", "")
    ai_key = os.getenv("ANTHROPIC_API_KEY", "")

    missing = [k for k, v in {"ODDS_API_KEY": odds_key, "ANTHROPIC_API_KEY": ai_key}.items() if not v]
    if missing:
        logger.error(f"Missing required env vars: {', '.join(missing)}")

    set_clients(
        OddsClient(odds_key),
        anthropic.Anthropic(api_key=ai_key),
    )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _scheduled_picks,
        CronTrigger(hour=10, minute=0, timezone=pytz.timezone("America/New_York")),
        id="daily_picks",
        name="Daily Picks 10am ET",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info("Scheduler started — daily picks fire at 10:00am ET")

    yield

    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


app = FastAPI(
    title="BetBot API",
    description="AI sports betting picks — backend for Whop app",
    version="1.0.0",
    lifespan=lifespan,
)

# Allow the Whop app frontend to call this API from the browser
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://whop.com", "https://*.whop.com"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(router)

# Serve the frontend — Whop iFrame points at /
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_frontend():
    return FileResponse("static/index.html")

@app.get("/privacy")
async def serve_privacy():
    return FileResponse("static/privacy.html")

@app.get("/terms")
async def serve_terms():
    return FileResponse("static/terms.html")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
