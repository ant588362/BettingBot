"""
Discord bot — slash commands + DM handler for member Q&A.
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

import anthropic
import discord
from discord import app_commands
from discord.ext import commands

from claude_client import analyze_matchup, analyze_parlay, format_odds_for_prompt
from history import get_all_stats, get_weekly_stats
from odds_client import OddsClient
from picks_generator import run_daily_picks

logger = logging.getLogger(__name__)

# ── Rate limiter: 5 AI requests per user per 10 minutes ──────────────────────

_RATE_MAX = 5
_RATE_WINDOW = 600  # seconds


class _RateLimiter:
    def __init__(self):
        self._log: dict[int, list[float]] = defaultdict(list)

    def check(self, user_id: int) -> tuple[bool, int]:
        """Returns (allowed, seconds_until_reset)."""
        now = datetime.now(timezone.utc).timestamp()
        self._log[user_id] = [t for t in self._log[user_id] if now - t < _RATE_WINDOW]
        if len(self._log[user_id]) >= _RATE_MAX:
            oldest = min(self._log[user_id])
            return False, int(_RATE_WINDOW - (now - oldest))
        self._log[user_id].append(now)
        return True, 0


_limiter = _RateLimiter()

# Per-server cooldown for /picks — prevents members from spamming full generation
# Key: guild_id (or user_id for DMs). Value: timestamp of last generation.
_picks_cooldowns: dict[int, float] = {}
_PICKS_COOLDOWN_SECS = 3600  # 1 hour between on-demand generations


def _rl_message(wait: int) -> str:
    m, s = wait // 60, wait % 60
    return f"You've hit the rate limit. Please wait {m}m {s}s before trying again."


# ── Bot class ─────────────────────────────────────────────────────────────────

class BetBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # privileged — enable in Discord Developer Portal
        intents.dm_messages = True
        super().__init__(command_prefix="!", intents=intents)

        self.odds_client: OddsClient | None = None
        self.claude_client: anthropic.Anthropic | None = None
        self._odds_cache: dict = {}
        self._odds_cache_ts: float = 0.0
        self._cache_ttl = 1800  # 30 min

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("Global slash commands synced")

    async def on_ready(self):
        logger.info(f"Bot online: {self.user} (id={self.user.id})")
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="the lines 👀")
        )

    # ── Odds cache (non-blocking) ─────────────────────────────────────────────

    async def fetch_odds(self) -> dict:
        now = datetime.now(timezone.utc).timestamp()
        if now - self._odds_cache_ts > self._cache_ttl:
            logger.info("Refreshing odds cache…")
            self._odds_cache = await asyncio.to_thread(self.odds_client.get_all_odds)
            self._odds_cache_ts = now
        return self._odds_cache

    # ── DM handler ────────────────────────────────────────────────────────────

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if isinstance(message.channel, discord.DMChannel):
            await self._handle_dm(message)
            return
        await self.process_commands(message)

    async def _handle_dm(self, message: discord.Message):
        allowed, wait = _limiter.check(message.author.id)
        if not allowed:
            await message.channel.send(_rl_message(wait))
            return

        async with message.channel.typing():
            try:
                all_odds = await self.fetch_odds()
                ctx = format_odds_for_prompt(all_odds, max_chars=3000)
                reply = await asyncio.to_thread(
                    analyze_matchup, self.claude_client, message.content, ctx
                )
                await message.channel.send(reply)
            except Exception as e:
                logger.error(f"DM handler error: {e}")
                await message.channel.send("Ran into an issue. Please try again in a moment.")


# ── Bot factory + slash commands ──────────────────────────────────────────────

def create_bot() -> BetBot:
    bot = BetBot()

    # /picks ──────────────────────────────────────────────────────────────────
    @bot.tree.command(name="picks", description="Generate today's AI picks on demand")
    async def cmd_picks(interaction: discord.Interaction):
        scope_id = interaction.guild_id or interaction.user.id
        now = datetime.now(timezone.utc).timestamp()
        last = _picks_cooldowns.get(scope_id, 0.0)
        remaining = int(_PICKS_COOLDOWN_SECS - (now - last))

        if remaining > 0:
            await interaction.response.send_message(
                f"Picks were already generated recently. Next refresh in {remaining // 60}m {remaining % 60}s.\n"
                "The daily picks post at 10am ET automatically — use this sparingly!",
                ephemeral=True,
            )
            return

        _picks_cooldowns[scope_id] = now
        await interaction.response.defer(thinking=True)
        try:
            await asyncio.to_thread(run_daily_picks)
            await interaction.followup.send(
                "✅ Fresh picks generated and posted to the picks channel + Whop feed!",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"/picks error: {e}")
            _picks_cooldowns.pop(scope_id, None)  # release cooldown on failure so they can retry
            await interaction.followup.send(
                "Picks generation failed. Please try again.", ephemeral=True
            )

    # /analyze ────────────────────────────────────────────────────────────────
    @bot.tree.command(name="analyze", description="AI analysis on any team or matchup today")
    @app_commands.describe(matchup="Team name or matchup (e.g. 'Lakers' or 'Chiefs vs Eagles')")
    async def cmd_analyze(interaction: discord.Interaction, matchup: str):
        allowed, wait = _limiter.check(interaction.user.id)
        if not allowed:
            await interaction.response.send_message(_rl_message(wait), ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        try:
            all_odds = await bot.fetch_odds()
            ctx = format_odds_for_prompt(all_odds, max_chars=3000)
            result = await asyncio.to_thread(analyze_matchup, bot.claude_client, matchup, ctx)
            embed = discord.Embed(
                title=f"🔍 Analysis: {matchup}",
                description=result,
                color=0x4A90E2,
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"/analyze error: {e}")
            await interaction.followup.send("Analysis failed. Try again in a moment.", ephemeral=True)

    # /odds ───────────────────────────────────────────────────────────────────
    @bot.tree.command(name="odds", description="Current odds for a team or fighter")
    @app_commands.describe(team="Team or fighter name (e.g. 'Lakers', 'Poirier')")
    async def cmd_odds(interaction: discord.Interaction, team: str):
        await interaction.response.defer(thinking=True)
        try:
            all_odds = await bot.fetch_odds()
            lines: list[str] = []

            for sport, games in all_odds.items():
                for game in games:
                    home = game.get("home_team", "")
                    away = game.get("away_team", "")
                    needle = team.lower()
                    if needle not in home.lower() and needle not in away.lower():
                        continue

                    tip = game.get("commence_time", "")[:16].replace("T", " ")
                    lines.append(f"**{away} @ {home}**  |  {sport}  |  {tip} UTC")

                    for bk in game.get("bookmakers", [])[:1]:
                        for mkt in bk.get("markets", []):
                            mkey = mkt["key"]
                            label = {"h2h": "ML", "spreads": "Spread", "totals": "Total"}.get(mkey, mkey)
                            parts: list[str] = []
                            for o in mkt.get("outcomes", []):
                                try:
                                    price = int(o["price"])
                                except (KeyError, ValueError):
                                    continue
                                sign = "+" if price > 0 else ""
                                pt = o.get("point")
                                pt_s = f" {'+' if (pt is not None and pt > 0) else ''}{pt}" if pt is not None else ""
                                parts.append(f"{o.get('name', '?')}{pt_s}: {sign}{price}")
                            if parts:
                                lines.append(f"  _{label}:_ {' | '.join(parts)}")
                    lines.append("")

            if not lines:
                await interaction.followup.send(f"No games found for **{team}** today.", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"📊 Odds: {team}",
                description="\n".join(lines)[:4000],
                color=0x27AE60,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text="Verify odds at your sportsbook before betting.")
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"/odds error: {e}")
            await interaction.followup.send("Failed to fetch odds. Try again.", ephemeral=True)

    # /parlay ─────────────────────────────────────────────────────────────────
    @bot.tree.command(name="parlay", description="Analyze a parlay combination")
    @app_commands.describe(picks="Your picks (e.g. 'Lakers ML + Chiefs -3 + Over 47.5')")
    async def cmd_parlay(interaction: discord.Interaction, picks: str):
        allowed, wait = _limiter.check(interaction.user.id)
        if not allowed:
            await interaction.response.send_message(_rl_message(wait), ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        try:
            all_odds = await bot.fetch_odds()
            ctx = format_odds_for_prompt(all_odds, max_chars=2000)
            result = await asyncio.to_thread(analyze_parlay, bot.claude_client, picks, ctx)
            embed = discord.Embed(
                title="🎰 Parlay Analysis",
                description=f"**Your picks:** {picks}\n\n{result}",
                color=0x9B59B6,
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"/parlay error: {e}")
            await interaction.followup.send("Parlay analysis failed. Try again.", ephemeral=True)

    # /record ─────────────────────────────────────────────────────────────────
    @bot.tree.command(name="record", description="Show current pick record and ROI")
    async def cmd_record(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            stats, weekly = await asyncio.gather(
                asyncio.to_thread(get_all_stats),
                asyncio.to_thread(get_weekly_stats),
            )

            def _fmt(d: dict) -> str:
                pl = d["unit_pl"]
                pushes_str = f" ({d['pushes']}P)" if d["pushes"] else ""
                return (
                    f"**{d['wins']}-{d['losses']}{pushes_str}**\n"
                    f"Win Rate: {d['win_rate']:.1f}%\n"
                    f"P/L: **{'+' if pl >= 0 else ''}{pl:.1f}u**"
                )

            embed = discord.Embed(
                title="📈 Pick Record & ROI",
                color=0xF5A623,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="All-Time", value=_fmt(stats), inline=True)
            embed.add_field(name="Last 7 Days", value=_fmt(weekly), inline=True)
            embed.set_footer(text="Results sourced from picks_history.csv")
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"/record error: {e}")
            await interaction.followup.send("Failed to load record. Try again.", ephemeral=True)

    return bot
