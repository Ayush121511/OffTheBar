"""
Current football facts from the football-data.org API: league tables, recent
and coming matches, squads, managers and top scorers, for the competitions its
free plan covers. Domestic cups and the Europa League aren't among them.

`python football_data.py` fetches everything and saves one gzipped JSON
(FOOTBALL_DATA_STORE: "gs://bucket/path" in production, a local path otherwise),
about 5 minutes at the free plan's 10 calls a minute. A scheduled job runs it.
context_for() reads that file and gives an answer the facts on the clubs and
competitions a question names, so tables, fixtures and squads come from data
instead of from old articles or the model's memory.

Terms: the site must show "Football data provided by the Football-Data.org API".
"""
import os
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from kb_store import read_json_gz, write_json_gz

API_URL = "https://api.football-data.org/v4"
DEFAULT_STORE = Path(__file__).resolve().parent / ".cache" / "football_data.json.gz"
COMPETITIONS = {
    "PL": "Premier League",
    "PD": "La Liga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "CL": "Champions League",
    "ELC": "Championship",
    "DED": "Eredivisie",
    "PPL": "Primeira Liga",
    "BSA": "Brasileirao",
}
COMPETITION_ALIASES = {
    "PL": ["premier league", "epl"],
    "PD": ["la liga", "laliga", "spanish league"],
    "SA": ["serie a", "italian league"],
    "BL1": ["bundesliga", "german league"],
    "FL1": ["ligue 1", "french league"],
    "CL": ["champions league", "ucl"],
    "ELC": ["championship"],
    "DED": ["eredivisie", "dutch league"],
    "PPL": ["primeira liga", "portuguese league"],
    "BSA": ["brasileirao", "brazilian league"],
}
TEAM_ALIASES = {
    "Man City": ["manchester city", "man city"],
    "Man United": ["manchester united", "man united", "man utd"],
    "Tottenham": ["tottenham", "spurs"],
    "Wolverhampton": ["wolves"],
    "Barça": ["barcelona", "barca"],
    "Atleti": ["atletico madrid", "atletico", "atleti"],
    "Inter": ["inter milan", "inter"],
    "Bayern": ["bayern munich", "bayern"],
    "PSG": ["paris saint-germain", "psg"],
}
FETCH_WINDOW_DAYS = 21
SECONDS_BETWEEN_CALLS = 6.5
RATE_LIMIT_WAIT_SECONDS = 65
RELOAD_SECONDS = 30 * 60
RETRY_SECONDS = 60
LAST_RESULTS = 5
NEXT_MATCHES = 3
TABLE_SCORERS = 10
MAX_CLUBS = 3
MIN_SURNAME_LENGTH = 4
POSITION_ORDER = ("Goalkeeper", "Defence", "Midfield", "Offence")


def store_location() -> str:
    return os.getenv("FOOTBALL_DATA_STORE") or str(DEFAULT_STORE)


def _folded(text):
    """Lowercase, without accents, so "Barça" and "barca" match."""
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def _fetch(path, token):
    """One API call at the free plan's pace, or None if it fails."""
    time.sleep(SECONDS_BETWEEN_CALLS)
    for _ in range(2):
        response = requests.get(f"{API_URL}{path}", headers={"X-Auth-Token": token}, timeout=30)
        if response.status_code != 429:
            break
        time.sleep(RATE_LIMIT_WAIT_SECONDS)
    if response.status_code != 200:
        print(f"[OffTheBar data] {path} returned {response.status_code}: {response.text[:160]}")
        return None
    return response.json()


def _table_rows(standings):
    rows = []
    for group in standings.get("standings", []):
        if group.get("type") != "TOTAL":
            continue
        for row in group.get("table", []):
            rows.append({
                "position": row["position"],
                "team_id": row["team"]["id"],
                "team": row["team"].get("shortName") or row["team"].get("name"),
                "played": row["playedGames"],
                "won": row["won"],
                "drawn": row["draw"],
                "lost": row["lost"],
                "goal_difference": row["goalDifference"],
                "points": row["points"],
            })
    return rows


def _team_record(team, code):
    return {
        "id": team["id"],
        "name": team.get("name"),
        "short": team.get("shortName") or team.get("name"),
        "coach": (team.get("coach") or {}).get("name"),
        "competitions": [code],
        "squad": [{"name": p.get("name"), "position": p.get("position")} for p in team.get("squad") or []],
    }


def _match_record(match, code):
    full_time = (match.get("score") or {}).get("fullTime") or {}
    return {
        "competition": code,
        "date": match.get("utcDate"),
        "status": match.get("status"),
        "matchday": match.get("matchday"),
        "home_id": match["homeTeam"].get("id"),
        "home": match["homeTeam"].get("shortName") or match["homeTeam"].get("name"),
        "away_id": match["awayTeam"].get("id"),
        "away": match["awayTeam"].get("shortName") or match["awayTeam"].get("name"),
        "home_goals": full_time.get("home"),
        "away_goals": full_time.get("away"),
    }


def refresh():
    """Fetch every competition and save the store. A competition whose calls
    fail keeps what the last run saved."""
    token = os.environ["FOOTBALL_DATA_API_KEY"]
    store = read_json_gz(store_location()) or {}
    competitions = store.get("competitions", {})
    today = datetime.now(timezone.utc).date()
    window = f"dateFrom={today - timedelta(days=FETCH_WINDOW_DAYS)}&dateTo={today + timedelta(days=FETCH_WINDOW_DAYS)}"
    fetched = 0
    for code, name in COMPETITIONS.items():
        standings = _fetch(f"/competitions/{code}/standings", token)
        teams = _fetch(f"/competitions/{code}/teams", token)
        matches = _fetch(f"/competitions/{code}/matches?{window}", token)
        scorers = _fetch(f"/competitions/{code}/scorers?limit={TABLE_SCORERS}", token)
        if not standings or not teams or matches is None:
            print(f"[OffTheBar data] {name}: incomplete, keeping the last run's data")
            continue
        competitions[code] = {
            "name": name,
            "matchday": (standings.get("season") or {}).get("currentMatchday"),
            "table": _table_rows(standings),
            "teams": [_team_record(team, code) for team in teams.get("teams", [])],
            "matches": [_match_record(match, code) for match in matches.get("matches", [])],
            "scorers": [
                {"player": row["player"].get("name"), "team": row["team"].get("shortName"),
                 "goals": row.get("goals"), "assists": row.get("assists")}
                for row in (scorers or {}).get("scorers", [])
            ],
        }
        fetched += 1
        print(f"[OffTheBar data] {name}: {len(competitions[code]['table'])} table rows, "
              f"{len(competitions[code]['teams'])} teams, {len(competitions[code]['matches'])} matches")
    if not fetched:
        raise RuntimeError("no competition could be fetched")
    write_json_gz(store_location(), {"competitions": competitions})
    print(f"[OffTheBar data] saved {fetched} of {len(COMPETITIONS)} competitions to {store_location()}")


class _Facts:
    """The store, indexed by the names people use."""

    def __init__(self, store):
        self.updated_at = store.get("updated_at", "")
        self.competitions = store.get("competitions", {})
        self.teams = {}
        self.team_names = {}
        for code, competition in self.competitions.items():
            for team in competition.get("teams", []):
                known = self.teams.get(team["id"])
                if known:
                    known["competitions"].extend(c for c in team["competitions"] if c not in known["competitions"])
                    continue
                self.teams[team["id"]] = dict(team, competitions=list(team["competitions"]))
                names = [team.get("short"), re.sub(r"\b(FC|CF|AFC|SC|SSC|AC|1\.)\b\.?", "", team.get("name") or "")]
                names += TEAM_ALIASES.get(team.get("short"), [])
                for name in names:
                    folded = " ".join(_folded(name).split())
                    if len(folded) > 2:
                        self.team_names[folded] = team["id"]
        self.full_names = {}
        self.surnames = {}
        for team in self.teams.values():
            for player in team.get("squad", []):
                full = _folded(player.get("name"))
                self.full_names.setdefault(full, []).append((player["name"], team))
                surname = full.split()[-1] if full.split() else ""
                if len(surname) >= MIN_SURNAME_LENGTH and surname != full:
                    self.surnames.setdefault(surname, []).append((player["name"], team))

    def clubs_named(self, text):
        found = []
        for name, team_id in sorted(self.team_names.items(), key=lambda item: -len(item[0])):
            if re.search(rf"\b{re.escape(name)}\b", text):
                text = re.sub(rf"\b{re.escape(name)}\b", " ", text)
                if team_id not in found:
                    found.append(team_id)
        return found[:MAX_CLUBS]

    def competitions_named(self, text):
        return [code for code, aliases in COMPETITION_ALIASES.items()
                if code in self.competitions and any(re.search(rf"\b{re.escape(a)}\b", text) for a in aliases)]

    def players_named(self, text):
        """Players whose full name `text` holds, or whose surname it holds with
        a capital, mid-sentence: lowercase "white" and "young" are just words."""
        found = {}
        words = re.findall(r"[^\W\d_][\w'\u2019-]*", text)
        folded = [_folded(word) for word in words]
        for pair in zip(folded, folded[1:]):
            for name, team in self.full_names.get(" ".join(pair), []):
                found.setdefault(name, team)
        for i, word in enumerate(words):
            if i and word[0].isupper():
                for name, team in self.surnames.get(folded[i], []):
                    found.setdefault(name, team)
        return found


def _table_line(row, competition):
    return (f"{row['position']}. {row['team']} {row['points']} pts, played {row['played']} "
            f"(W{row['won']} D{row['drawn']} L{row['lost']}, GD {row['goal_difference']:+d})")


def _match_line(match, team_id, competitions):
    day = datetime.fromisoformat(match["date"].replace("Z", "+00:00")).strftime("%A %Y-%m-%d")
    name = competitions.get(match["competition"], {}).get("name", match["competition"])
    if match["home_goals"] is None:
        opponent = f"{match['away']} (home)" if match["home_id"] == team_id else f"{match['home']} (away)"
        return f"{day}: {opponent}, {name}"
    return f"{day}: {match['home']} {match['home_goals']}-{match['away_goals']} {match['away']}, {name}"


def _club_block(facts, team_id, now):
    team = facts.teams[team_id]
    lines = [f"{team['short']} - manager: {team.get('coach') or 'not listed'}"]
    matches = []
    for code in team["competitions"]:
        competition = facts.competitions[code]
        for row in competition.get("table", []):
            if row["team_id"] == team_id:
                lines.append(f"{competition['name']} table: {_table_line(row, competition)}")
        matches += [m for m in competition.get("matches", []) if team_id in (m["home_id"], m["away_id"])]
    matches.sort(key=lambda m: m["date"])
    played = [m for m in matches if m["status"] == "FINISHED"]
    coming = [m for m in matches if m["status"] in ("SCHEDULED", "TIMED") and m["date"] >= now.strftime("%Y-%m-%dT%H:%M")]
    if played:
        lines.append("Last results: " + "; ".join(_match_line(m, team_id, facts.competitions) for m in played[-LAST_RESULTS:]))
    if coming:
        lines.append("Next league and Champions League matches: " + "; ".join(_match_line(m, team_id, facts.competitions) for m in coming[:NEXT_MATCHES]))
    if team.get("squad"):
        groups = {position: [] for position in POSITION_ORDER}
        for player in team["squad"]:
            groups.setdefault(player.get("position") or "Other", []).append(player["name"])
        lines.append("Squad: " + "; ".join(f"{position}: {', '.join(names)}" for position, names in groups.items() if names))
    return "\n".join(lines)


def _competition_block(competition):
    lines = [f"{competition['name']} table, matchday {competition.get('matchday')}: "
             + "; ".join(_table_line(row, competition) for row in competition.get("table", []))]
    if competition.get("scorers"):
        lines.append(f"{competition['name']} top scorers: " + "; ".join(
            f"{row['player']} ({row['team']}) {row['goals']}" for row in competition["scorers"]))
    return "\n".join(lines)


_facts = None
_loaded_at = float("-inf")
_load_lock = threading.Lock()


def _current_facts():
    """The store, re-read every RELOAD_SECONDS. None if it can't be read."""
    global _facts, _loaded_at
    with _load_lock:
        if time.monotonic() - _loaded_at >= (RELOAD_SECONDS if _facts else RETRY_SECONDS):
            _loaded_at = time.monotonic()
            try:
                store = read_json_gz(store_location())
                if store:
                    _facts = _Facts(store)
            except Exception as exc:
                print(f"[OffTheBar data] couldn't read {store_location()}: {exc}")
        return _facts


def context_for(text):
    """The facts on the clubs, competitions and players `text` names, as a
    labeled block to go first in an answer's context, or ""."""
    facts = _current_facts()
    if not facts:
        return ""
    folded = _folded(text)
    now = datetime.now(timezone.utc)
    clubs = facts.clubs_named(folded)
    blocks = [_club_block(facts, team_id, now) for team_id in clubs]
    if not clubs:
        blocks += [_competition_block(facts.competitions[code]) for code in facts.competitions_named(folded)]
    players = facts.players_named(text)
    if players:
        blocks.append("Players of that name in these leagues, by current club: "
                      + "; ".join(f"{name}: {team['short']}" for name, team in list(players.items())[:MAX_CLUBS * 2]))
    if not blocks:
        return ""
    return (
        f"[FOOTBALL DATA - as of {facts.updated_at[:16].replace('T', ' ')} UTC. Current figures from a football database: "
        "trust them over every article and over your memory for tables, results, dates, fixtures, managers and squads. "
        "A player not in a club's squad here isn't at that club now. Scores can lag a few hours, and domestic cups and the "
        "Europa League aren't included, so a club's real next match may be a cup tie.]\n"
        + "\n\n".join(blocks)
    )


if __name__ == "__main__":
    if os.path.exists(".env"):
        for line in Path(".env").read_text().splitlines():
            if line.startswith("FOOTBALL_DATA_API_KEY="):
                os.environ.setdefault("FOOTBALL_DATA_API_KEY", line.split("=", 1)[1].strip().strip("\"'"))
    refresh()
    sys.exit(0)
