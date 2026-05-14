import logging
import requests

logger = logging.getLogger(__name__)

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# sport_key -> display name; only in-season sports will return games
SPORTS_MAP = {
    "basketball_nba": "NBA",
    "icehockey_nhl": "NHL",
    "baseball_mlb": "MLB",
    "americanfootball_nfl": "NFL",
    "mma_mixed_martial_arts": "UFC/MMA",
    "basketball_ncaab": "NCAAB",
    "americanfootball_ncaaf": "NCAAF",
}


class OddsClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.requests_remaining: str | None = None

    def get_sport_odds(self, sport_key: str) -> list[dict]:
        params = {
            "apiKey": self.api_key,
            "regions": "us",
            "markets": "h2h,spreads,totals",
            "oddsFormat": "american",
            "dateFormat": "iso",
        }
        try:
            resp = requests.get(
                f"{ODDS_API_BASE}/sports/{sport_key}/odds/",
                params=params,
                timeout=15,
            )
            if "x-requests-remaining" in resp.headers:
                self.requests_remaining = resp.headers["x-requests-remaining"]
            if resp.status_code == 404:
                return []  # sport not currently in season
            if resp.status_code == 401:
                logger.error("Odds API: invalid API key")
                return []
            if resp.status_code == 422:
                return []  # sport key not supported
            if resp.status_code == 429:
                logger.warning("Odds API: monthly quota exceeded")
                return []
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error(f"Odds API error ({sport_key}): {e}")
            return []

    def get_all_odds(self) -> dict[str, list[dict]]:
        """Returns {sport_display_name: [game_objects]}. Out-of-season sports are omitted."""
        result: dict[str, list[dict]] = {}
        for sport_key, sport_name in SPORTS_MAP.items():
            games = self.get_sport_odds(sport_key)
            if games:
                result[sport_name] = games
                logger.info(f"{sport_name}: {len(games)} game(s)")
        logger.info(f"Odds API requests remaining: {self.requests_remaining}")
        return result

    def search_team(self, team_name: str, all_odds: dict[str, list[dict]]) -> list[dict]:
        """Return games where team_name appears in home or away team field."""
        needle = team_name.lower()
        hits = []
        for sport, games in all_odds.items():
            for game in games:
                home = game.get("home_team", "").lower()
                away = game.get("away_team", "").lower()
                if needle in home or needle in away:
                    hits.append({"sport": sport, "game": game})
        return hits
