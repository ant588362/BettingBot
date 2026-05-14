"""
Whop API client — posts picks to your Whop community feed / direct messages.

Required env vars:
  WHOP_API_KEY       — Bearer token from Whop dashboard → Developer → API Keys
  WHOP_COMPANY_ID    — Your company ID (e.g. "biz_abc123")
  WHOP_EXPERIENCE_ID — (optional) experience/product ID to scope the post
  WHOP_FORUM_POST    — set to "1" to create a forum post; default is a feed post

Whop API docs: https://dev.whop.com/reference
"""

import logging
import os
import requests

logger = logging.getLogger(__name__)

WHOP_BASE = "https://api.whop.com/api/v2"


class WhopClient:
    def __init__(self, api_key: str, company_id: str, experience_id: str | None = None):
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.company_id = company_id
        self.experience_id = experience_id

    # ── Feed posts ────────────────────────────────────────────────────────────

    def post_to_feed(self, title: str, body: str) -> dict | None:
        """Create a post in the company's community feed (visible to all members)."""
        payload: dict = {"title": title, "body": body}
        if self.experience_id:
            payload["experience_id"] = self.experience_id

        url = f"{WHOP_BASE}/companies/{self.company_id}/posts"
        try:
            resp = requests.post(url, json=payload, headers=self.headers, timeout=15)
            if resp.status_code == 401:
                logger.error("Whop API: unauthorised — check WHOP_API_KEY")
                return None
            if resp.status_code == 404:
                logger.error("Whop API: company/experience not found — check WHOP_COMPANY_ID")
                return None
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"Whop: posted feed post id={data.get('id')}")
            return data
        except requests.RequestException as e:
            logger.error(f"Whop API error (post_to_feed): {e}")
            return None

    # ── Forum posts (structured, with sections) ───────────────────────────────

    def post_to_forum(self, title: str, body: str, forum_id: str | None = None) -> dict | None:
        """Create a forum post if your Whop product has a forum section."""
        endpoint_id = forum_id or self.experience_id
        if not endpoint_id:
            logger.warning("Whop: no forum_id or experience_id provided, falling back to feed post")
            return self.post_to_feed(title, body)

        url = f"{WHOP_BASE}/forums/{endpoint_id}/posts"
        payload = {"title": title, "body": body}
        try:
            resp = requests.post(url, json=payload, headers=self.headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"Whop: posted forum post id={data.get('id')}")
            return data
        except requests.RequestException as e:
            logger.error(f"Whop API error (post_to_forum): {e}")
            return None

    # ── Broadcast messages (DM blast to all members) ──────────────────────────

    def broadcast_message(self, message: str) -> dict | None:
        """Send a direct message to all active members via Whop inbox."""
        url = f"{WHOP_BASE}/companies/{self.company_id}/messages/broadcast"
        payload = {"message": message}
        if self.experience_id:
            payload["experience_id"] = self.experience_id
        try:
            resp = requests.post(url, json=payload, headers=self.headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            logger.info("Whop: broadcast message sent")
            return data
        except requests.RequestException as e:
            logger.error(f"Whop API error (broadcast_message): {e}")
            return None


# ── Text formatters (plain-text for Whop, which renders Markdown) ─────────────

def _stars(n: int) -> str:
    return "★" * max(1, min(5, n)) + "☆" * (5 - max(1, min(5, n)))


def _pick_block(pick: dict, emoji: str = "▶") -> str:
    return (
        f"{emoji} **{pick['teams']}** | {pick['sport']}\n"
        f"Pick: **{pick['pick']}** @ {pick['odds']}\n"
        f"Confidence: {_stars(pick['confidence'])} | Units: {pick['units']}u\n"
        f"_{pick['analysis']}_\n"
    )


def format_picks_for_whop(picks_data: dict, yesterday: dict, weekly: dict) -> tuple[str, str]:
    """Returns (title, body) suitable for a Whop post."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    title = f"🏆 AI Picks — {today}"

    sections: list[str] = []

    # Track record
    record_lines: list[str] = []
    if yesterday["wins"] + yesterday["losses"] > 0:
        pl = yesterday["unit_pl"]
        record_lines.append(
            f"Yesterday: **{yesterday['wins']}-{yesterday['losses']}** | "
            f"P/L: **{'+' if pl >= 0 else ''}{pl:.1f}u**"
        )
    wpl = weekly["unit_pl"]
    record_lines.append(
        f"7-Day: **{weekly['wins']}-{weekly['losses']}** "
        f"({weekly['win_rate']:.0f}% ATS) | ROI: **{'+' if wpl >= 0 else ''}{wpl:.1f}u**"
    )
    sections.append("📊 **TRACK RECORD**\n" + "\n".join(record_lines))
    sections.append("---")

    # Lock
    lock = picks_data.get("lock_of_the_day")
    if lock:
        sections.append("🔒 **LOCK OF THE DAY**\n" + _pick_block(lock, "🔒"))
        sections.append("---")

    # Top plays
    tops = picks_data.get("top_picks", [])
    if tops:
        blocks = "\n".join(_pick_block(p) for p in tops)
        sections.append(f"⭐ **TOP PLAYS** ({len(tops)} picks)\n\n{blocks}")
        sections.append("---")

    # Longshots
    shots = picks_data.get("longshots", [])
    if shots:
        blocks = "\n".join(_pick_block(p, "🎯") for p in shots)
        sections.append(f"🎲 **LONGSHOTS**\n\n{blocks}")
        sections.append("---")

    sections.append("_AI analysis only. Bet responsibly. Past performance ≠ future results._")

    body = "\n\n".join(sections)
    return title, body


def post_picks_to_whop(picks_data: dict, yesterday: dict, weekly: dict) -> bool:
    """Top-level helper called by picks_generator. Returns True on success."""
    api_key = os.getenv("WHOP_API_KEY", "")
    company_id = os.getenv("WHOP_COMPANY_ID", "")
    experience_id = os.getenv("WHOP_EXPERIENCE_ID") or None
    use_forum = os.getenv("WHOP_FORUM_POST", "0") == "1"

    if not api_key or not company_id:
        logger.warning("Whop: WHOP_API_KEY or WHOP_COMPANY_ID not set — skipping Whop post")
        return False

    client = WhopClient(api_key, company_id, experience_id)
    title, body = format_picks_for_whop(picks_data, yesterday, weekly)

    result = client.post_to_forum(title, body) if use_forum else client.post_to_feed(title, body)
    return result is not None
