import os
import re
import requests
import streamlit as st
from bs4 import BeautifulSoup
from openai import OpenAI
from datetime import datetime

# =========================
# Constants / configuration
# =========================
TEAM_LISTS_TOPIC_URL = "https://www.nrl.com/news/topic/team-lists/"
DRAW_BASE_URL_2026 = "https://www.nrl.com/draw/nrl-premiership/2026/"

NRL_TEAMS = [
    "Brisbane Broncos",
    "Canterbury-Bankstown Bulldogs",
    "North Queensland Cowboys",
    "Dolphins",
    "St George Illawarra Dragons",
    "Parramatta Eels",
    "Newcastle Knights",
    "Penrith Panthers",
    "South Sydney Rabbitohs",
    "Canberra Raiders",
    "Sydney Roosters",
    "Manly Warringah Sea Eagles",
    "Cronulla-Sutherland Sharks",
    "Melbourne Storm",
    "Gold Coast Titans",
    "New Zealand Warriors",
    "Wests Tigers",
]

TEAM_SHORT = {
    "Brisbane Broncos": "Broncos",
    "Canterbury-Bankstown Bulldogs": "Bulldogs",
    "North Queensland Cowboys": "Cowboys",
    "Dolphins": "Dolphins",
    "St George Illawarra Dragons": "Dragons",
    "Parramatta Eels": "Eels",
    "Newcastle Knights": "Knights",
    "Penrith Panthers": "Panthers",
    "South Sydney Rabbitohs": "Rabbitohs",
    "Canberra Raiders": "Raiders",
    "Sydney Roosters": "Roosters",
    "Manly Warringah Sea Eagles": "Sea Eagles",
    "Cronulla-Sutherland Sharks": "Sharks",
    "Melbourne Storm": "Storm",
    "Gold Coast Titans": "Titans",
    "New Zealand Warriors": "Warriors",
    "Wests Tigers": "Wests Tigers",
}
TEAM_SLUG = {
    "Brisbane Broncos": "broncos",
    "Canterbury-Bankstown Bulldogs": "bulldogs",
    "North Queensland Cowboys": "cowboys",
    "Dolphins": "dolphins",
    "St George Illawarra Dragons": "dragons",
    "Parramatta Eels": "eels",
    "Newcastle Knights": "knights",
    "Penrith Panthers": "panthers",
    "South Sydney Rabbitohs": "rabbitohs",
    "Canberra Raiders": "raiders",
    "Sydney Roosters": "roosters",
    "Manly Warringah Sea Eagles": "sea-eagles",
    "Cronulla-Sutherland Sharks": "sharks",
    "Melbourne Storm": "storm",
    "Gold Coast Titans": "titans",
    "New Zealand Warriors": "warriors",
    "Wests Tigers": "wests-tigers",
}
POSITIONS = [
    "Fullback",
    "Winger",
    "Centre",
    "Five-eighth",
    "Halfback",
    "Prop",
    "Hooker",
    "Second Row",
    "Lock",
    "Interchange",
    "Utility",
]
PLAYER_STATS_IDS = [
    "33",  # tries
    "35",  # goals
    "45",  # tackles made
    "47",  # run metres
    "52",  # line breaks
]
SYSTEM_PROMPT = """
You are an NRL tactical profiler.

Rules:
- Do not invent stats, injuries, or confirmed selections.
- If something is unknown, say Unknown.
- Provide exactly 3 tactical options.
- Rate each option (Effectiveness 1-10, Risk Low/Med/High, Confidence 0-100).
- Keep response under 650 words.
""".strip()


# =========================
# Network helpers
# =========================
def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NRLProfilerMVP/1.0; +https://localhost)"
    }
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    return r.text


def _unique_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        clean = " ".join((item or "").split())
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _team_aliases(team_full: str) -> list[str]:
    aliases = {team_full, TEAM_SHORT.get(team_full, team_full)}
    for token in ["St", "St.", "Warringah", "-", "Bankstown"]:
        aliases.update({a.replace(token, "").strip() for a in list(aliases)})
    aliases = {a for a in aliases if a}
    return sorted(aliases, key=len, reverse=True)


def _row_matches_team(row_text: str, aliases: list[str]) -> bool:
    row_text_lower = row_text.lower()
    return any(alias.lower() in row_text_lower for alias in aliases)


@st.cache_data(ttl=86400)
def get_players_from_stats(team_full: str, season: int) -> list[str]:
    team_aliases = _team_aliases(team_full)
    names = set()

    for stat_id in PLAYER_STATS_IDS:
        url = f"https://www.nrl.com/stats/players/?competition=111&season={season}&stat={stat_id}"
        try:
            html = fetch_html(url)
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            continue

        for row in soup.select("table tbody tr"):
            cells = [td.get_text(" ", strip=True) for td in row.select("td")]
            if not cells:
                continue

            row_text = " | ".join(cells)
            if not _row_matches_team(row_text, team_aliases):
                continue

            name = None
            for a in row.select("a[href]"):
                href = a.get("href", "")
                candidate = " ".join(a.get_text(" ", strip=True).split())
                if "/players/" in href and " " in candidate:
                    name = candidate
                    break

            if not name:
                for cell in cells:
                    cleaned = " ".join(cell.split())
                    if len(cleaned) < 4 or cleaned.isdigit():
                        continue
                    if _row_matches_team(cleaned, team_aliases):
                        continue
                    if not re.search(r"[A-Za-z].*\s+[A-Za-z]", cleaned):
                        continue
                    name = cleaned
                    break

            if name:
                names.add(name)

    return sorted(names)


# =========================
# Draw / next fixture (best-effort scraping)
# =========================
import io
import pdfplumber
from datetime import date
from dateutil import parser as dateparser  # pip install python-dateutil

DRAW_PDF_URL = "https://www.nrl.com/globalassets/nrl-draw-2026---final.pdf"

def _download_pdf_bytes(url: str) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NRLProfilerMVP/1.0)"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.content

def try_get_next_fixture(team_full: str) -> dict:
    """
    Reliable MVP:
    - Download the official 2026 draw PDF
    - Extract text
    - Find the next match line that includes the selected team
    """
    team_short = TEAM_SHORT.get(team_full, team_full)

    try:
        pdf_bytes = _download_pdf_bytes(DRAW_PDF_URL)

        text = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t.strip():
                    text.append(t)

        all_text = "\n".join(text)

        # Very simple heuristic: find occurrences of "Team vs. Team" near dates.
        # The PDF includes entries like: "Sea Eagles vs. Raiders 4 Pines Park ... Sunday, Mar 8 ..."
        # We find the first future match containing the team short name.
        # NOTE: Dates in PDF are local/AEST; good enough for MVP.

        # Split into lines for scanning
        lines = [ln.strip() for ln in all_text.splitlines() if ln.strip()]

        today = date.today()

        # Regex for match line fragments
        match_re = re.compile(r"^(?P<home>.+?)\svs\.\s(?P<away>.+?)\s+(?P<venue>.+?)\s+\(", re.IGNORECASE)
        date_re = re.compile(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s([A-Za-z]{3})\s(\d{1,2})", re.IGNORECASE)

        best = None  # (match_date, data)

        for i, ln in enumerate(lines):
            if team_short.lower() not in ln.lower() and team_full.lower() not in ln.lower():
                continue

            m = match_re.search(ln)
            if not m:
                continue

            home = m.group("home").strip()
            away = m.group("away").strip()
            venue = m.group("venue").strip()

            # Look ahead a few lines for the date
            match_date = None
            for j in range(i, min(i + 6, len(lines))):
                dm = date_re.search(lines[j])
                if dm:
                    # Build a 2026 date from "Mar 8"
                    month_abbr = dm.group(2)
                    day_num = dm.group(3)
                    match_date = dateparser.parse(f"2026 {month_abbr} {day_num}").date()
                    break

            if not match_date:
                continue

            if match_date < today:
                continue

            # Pick the earliest future match
            if best is None or match_date < best[0]:
                best = (match_date, {"home_team": home, "away_team": away, "venue": venue})

        if not best:
            return {"success": False, "note": "Could not find a future fixture for this team in the 2026 draw PDF."}

        match_date, md = best
        home_full = _resolve_from_short(md["home_team"])
        away_full = _resolve_from_short(md["away_team"])

        opponent = away_full if team_full == home_full else home_full

        return {
            "success": True,
            "round_label": f"Next match on {match_date.isoformat()}",
            "opponent": opponent,
            "home_team": home_full,
            "away_team": away_full,
            "venue": md["venue"],
            "match_url": "Unknown",  # optional later
            "note": "Derived from official 2026 draw PDF.",
        }

    except Exception as e:
        return {"success": False, "note": f"PDF draw parse failed: {e}"}

def _resolve_from_short(name: str) -> str:
    # Map short club names from PDF to full names in NRL_TEAMS
    n = name.strip()
    for full, short in TEAM_SHORT.items():
        if n.lower() == short.lower():
            return full
    # If already close to full name, try partial match
    for full in NRL_TEAMS:
        if n.lower() in full.lower() or full.lower() in n.lower():
            return full
    return n

# =========================
# Team Lists scraping (from Team Lists articles)
# =========================
def find_latest_team_lists_article_url() -> str:
    html = fetch_html(TEAM_LISTS_TOPIC_URL)
    soup = BeautifulSoup(html, "lxml")

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        text = (a.get_text() or "").strip().lower()
        if "/news/" in href and "team lists" in text:
            return "https://www.nrl.com" + href if href.startswith("/") else href

    raise RuntimeError("Could not find a Team Lists article link on the topic page.")


def extract_match_team_list(article_url: str, home_short: str, away_short: str) -> dict:
    html = fetch_html(article_url)
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n")

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    target = f"{home_short} v {away_short}".lower()

    start_idx = None
    for i, ln in enumerate(lines):
        if target in ln.lower():
            start_idx = i
            break

    if start_idx is None:
        return {
            "match_heading": None,
            "players": [],
            "article_url": article_url,
            "note": "Match not found in this Team Lists article (lists may not be published yet, or naming differs).",
        }

    block = []
    for ln in lines[start_idx:start_idx + 800]:
        block.append(ln)

    player_re = re.compile(
        r"^(?P<pos>.+?) for (?P<team>.+?) is number (?P<num>\d+)\s+(?P<name>.+)$",
        re.IGNORECASE
    )

    players = []
    match_heading = lines[start_idx]

    for ln in block:
        m = player_re.match(ln)
        if not m:
            continue

        team_text = m.group("team").strip()
        num = int(m.group("num"))
        pos = m.group("pos").strip()
        name = " ".join(m.group("name").split())

        # Assign side based on short name containment
        side = "home"
        if away_short.lower() in team_text.lower() and home_short.lower() not in team_text.lower():
            side = "away"
        elif home_short.lower() in team_text.lower() and away_short.lower() not in team_text.lower():
            side = "home"
        else:
            # fallback: ambiguous -> choose away if it contains away short
            side = "away" if away_short.lower() in team_text.lower() else "home"

        players.append({"side": side, "team": team_text, "number": num, "position": pos, "name": name})

    return {"match_heading": match_heading, "players": players, "article_url": article_url}


# =========================
# Player stats scraping (best-effort: NRL site search -> player page text extract)
# =========================
def slugify_player_name(name: str) -> str:
    # "Nathan Cleary" -> "nathan-cleary"
    s = name.strip().lower()
    s = re.sub(r"[^a-z\s\-']", "", s)     # keep letters/spaces/hyphen/apostrophe
    s = s.replace("'", "")               # drop apostrophes (safe for URLs)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    return s

def try_fetch_player_stats(player_name: str, team_full: str) -> dict:
    """
    Reliable MVP approach:
    - Construct the NRL player profile URL from team + player slug
    - Parse the Player Bio fields and the latest year in the Career By Season table
    """
    try:
        team_slug = TEAM_SLUG.get(team_full)
        if not team_slug:
            return {"success": False, "note": f"No TEAM_SLUG mapping for {team_full}."}

        player_slug = slugify_player_name(player_name)
        url = f"https://www.nrl.com/players/nrl-premiership/{team_slug}/{player_slug}/"

        html = fetch_html(url)
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n")

        # Basic sanity check: page contains player name header
        if player_name.lower() not in text.lower():
            return {
                "success": False,
                "note": "Constructed player URL did not appear to match the player name (slug mismatch).",
                "tried_url": url,
            }

        # Extract Player Bio fields from the plain text (the page contains lines like 'Height:' then '182 cm')
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

        def pick_value_after(label: str) -> str:
            for i, ln in enumerate(lines):
                if ln.lower() == label.lower():
                    if i + 1 < len(lines):
                        return lines[i + 1]
            return "Unknown"

        # On NRL player pages, bio uses label lines like "Height:" followed by value on next line. :contentReference[oaicite:2]{index=2}
        height = pick_value_after("Height:")
        weight = pick_value_after("Weight:")
        dob = pick_value_after("Date of Birth:")
        age = pick_value_after("Age:")
        birthplace = pick_value_after("Birthplace:")

        # Extract latest season row from the Career By Season section.
        # The HTML text includes rows like:
        # "Panthers  2025  22  13  8  1  59%  6  79  -  1  184  12,726 ...  17  ...  Tackles Made ... Total Running Metres ..." :contentReference[oaicite:3]{index=3}
        team_short = TEAM_SHORT.get(team_full, team_full)

        season_rows = []
        for ln in lines:
            if team_short.lower() in ln.lower():
                m = re.search(r"\b(20\d{2})\b", ln)
                if m:
                    year = int(m.group(1))
                    # crude filter: row likely contains "Win %" or lots of numbers
                    digit_count = sum(ch.isdigit() for ch in ln)
                    if digit_count > 20:
                        season_rows.append((year, ln))

        latest_year = None
        latest_line = None
        if season_rows:
            latest_year, latest_line = sorted(season_rows, key=lambda x: x[0])[-1]

        # Very lightweight extraction from the latest line:
        # We'll just keep the whole line for now (MVP) plus a few common fields if we can.
        # (This avoids breaking if NRL adds/removes columns.)
        season_summary = "Unknown"
        if latest_year and latest_line:
            season_summary = f"Latest season row (from Career By Season): {latest_line}"

        summary = (
            f"NRL player page: {url}\n"
            f"Height: {height}\n"
            f"Weight: {weight}\n"
            f"DOB: {dob}\n"
            f"Age: {age}\n"
            f"Birthplace: {birthplace}\n"
            f"{season_summary}"
        )

        return {
            "success": True,
            "player_url": url,
            "summary_text": summary,
            "bio": {
                "height": height,
                "weight": weight,
                "dob": dob,
                "age": age,
                "birthplace": birthplace,
            },
            "latest_season_year": latest_year,
        }

    except requests.HTTPError as e:
        return {"success": False, "note": f"HTTP error fetching player page. Possibly wrong slug. {e}"}
    except Exception as e:
        return {"success": False, "note": f"Player stats scrape failed: {e}"}

# =========================
# LLM helper
# =========================
def generate_brief(model: str, payload: dict) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set.")

    client = OpenAI(api_key=api_key)

    user_prompt = f"""
PLAYER: {payload.get('player')}
TEAM: {payload.get('team')}
POSITION: {payload.get('position')}
OPPONENT: {payload.get('opponent')}

MATCH CONTEXT:
- Round: {payload.get('round_label')}
- Home: {payload.get('home_team')}
- Away: {payload.get('away_team')}
- Venue: {payload.get('venue')}
- Match URL (if known): {payload.get('match_url', 'Unknown')}

STRATEGY SETTINGS:
- Risk appetite (0-100): {payload.get('risk')}
- Style preference: {payload.get('style_pref')}

Instructions:
- If risk <= 30: prioritise completion, field position, conservative defensive reads, low-error tactics.
- If risk 31-70: balanced plan, selective pressure.
- If risk > 70: prioritise line-speed pressure, contestable kicks, aggressive edge reads/press, higher variance tactics.
- Always keep exactly 3 tactical options, but tune their aggressiveness to the risk level.

PLAYER STATS (if available):
{payload.get('player_stats', 'Unknown')}

OPPONENT NOTES (team list + any tendencies/stats you have):
{payload.get('opponent_notes')}

Deliver:
1) Opponent style snapshot
2) What player should expect (first 20 mins)
3) Exactly 3 tactical options with triggers + step-by-step + trade-offs
4) Ratings table
5) Outperform levers (top 5)
""".strip()

    resp = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    return resp.output_text


# =========================
# Streamlit UI
# =========================
st.set_page_config(page_title="NRL Profiler", layout="wide")
st.title("🏉 NRL Tactical Profiler MVP")

with st.sidebar:
    st.subheader("Model")
    model = st.selectbox(
        "Choose model",
        ["gpt-5-mini", "gpt-5.2", "gpt-5.1-chat-latest", "gpt-5-nano"],
        index=0,
    )
    st.caption("Tip: if a model errors for your account, switch it here.")
with st.sidebar:
    st.subheader("Strategy")
    risk = st.slider("Risk appetite", 0, 100, 50, help="0=conservative / low-variance, 100=aggressive / high-variance")
    style_pref = st.selectbox("Playing Style Preference", ["Balanced", "Middle-control", "Edge-shift", "Kick-pressure"], index=0)
# Session state defaults
if "fixture" not in st.session_state:
    st.session_state["fixture"] = {"success": False, "note": "No fixture loaded yet."}
if "scraped_opponent_list" not in st.session_state:
    st.session_state["scraped_opponent_list"] = ""
if "player_stats_text" not in st.session_state:
    st.session_state["player_stats_text"] = ""
if "team_list_players_by_team" not in st.session_state:
    st.session_state["team_list_players_by_team"] = {}
if "selected_player_by_team" not in st.session_state:
    st.session_state["selected_player_by_team"] = {}
if "manual_player_override_by_team" not in st.session_state:
    st.session_state["manual_player_override_by_team"] = {}
if "include_broader_list" not in st.session_state:
    st.session_state["include_broader_list"] = True

# ---- Inputs ----
st.subheader("1) Select team + player")

colA, colB, colC = st.columns([1.2, 1.2, 1.0], gap="large")

with colA:
    team = st.selectbox("Your team", NRL_TEAMS, index=NRL_TEAMS.index("Manly Warringah Sea Eagles"))
with colB:
    include_broader_list = st.toggle("Include broader list (best effort)", value=st.session_state["include_broader_list"])
    st.session_state["include_broader_list"] = include_broader_list

    current_season = datetime.now().year
    team_list_players = st.session_state["team_list_players_by_team"].get(team, [])
    broader_players = get_players_from_stats(team, current_season) if include_broader_list else []
    player_options = _unique_preserve_order(team_list_players + broader_players)
    if not player_options:
        player_options = ["Josh Feledy"]

    selected_player_by_team = st.session_state["selected_player_by_team"]
    if team not in selected_player_by_team or selected_player_by_team[team] not in player_options:
        selected_player_by_team[team] = player_options[0]

    selected_player = st.selectbox(
        "Player",
        player_options,
        index=player_options.index(selected_player_by_team[team]),
    )
    selected_player_by_team[team] = selected_player

    manual_default = st.session_state["manual_player_override_by_team"].get(team, "")
    manual_player_override = st.text_input("Or type player name", value=manual_default)
    st.session_state["manual_player_override_by_team"][team] = manual_player_override

with colC:
    position = st.selectbox("Position", POSITIONS, index=POSITIONS.index("Centre"))

player = manual_player_override.strip() or selected_player

# ---- Fixture: auto next opponent ----
st.subheader("2) Next fixture (auto)")

# Make fixture refresh obvious
fx_col1, fx_col2 = st.columns([1, 2], gap="large")
with fx_col1:
    if st.button("🔄 Refresh next fixture for selected team"):
        st.session_state["fixture"] = try_get_next_fixture(team)

fixture = st.session_state["fixture"]

# If fixture not found, allow fallback opponent selection (so you're never blocked)
fallback_opponent = None
if not fixture.get("success"):
    st.warning(fixture.get("note", "Could not determine next fixture automatically."))
    fallback_opponent = st.selectbox(
        "Fallback: choose opponent manually",
        [t for t in NRL_TEAMS if t != team],
        index=0
    )

# Derive match context
round_label = fixture.get("round_label", "Unknown") if fixture.get("success") else "Unknown"
home_team = fixture.get("home_team", "Unknown") if fixture.get("success") else "Unknown"
away_team = fixture.get("away_team", "Unknown") if fixture.get("success") else "Unknown"
venue = fixture.get("venue", "Unknown") if fixture.get("success") else "Unknown"
match_url = fixture.get("match_url", "Unknown") if fixture.get("success") else "Unknown"

# Determine opponent + home/away display
if fixture.get("success"):
    opponent = fixture.get("opponent", "Unknown")
else:
    opponent = fallback_opponent
    # For fallback mode we can’t know home/away; show Unknown
    home_team, away_team = "Unknown", "Unknown"
    venue, match_url = "Unknown", "Unknown"

with fx_col2:
    st.markdown(
        f"""
**Opponent:** {opponent}  
**Round:** {round_label}  
**Home:** {home_team}  
**Away:** {away_team}  
**Venue:** {venue}  
""".strip()
    )
    if match_url != "Unknown":
        st.markdown(f"Match link: {match_url}")

# ---- Player stats button ----
st.subheader("3) Player stats (optional, free scrape)")

ps_col1, ps_col2 = st.columns([1, 2], gap="large")
with ps_col1:
    if st.button("📊 Fetch player stats (best-effort)"):
        res = try_fetch_player_stats(player, team)
        if res.get("success"):
            st.session_state["player_stats_text"] = res.get("summary_text", "")
            st.success("Fetched player stats summary.")
        else:
            st.session_state["player_stats_text"] = ""
            st.warning(res.get("note", "Could not fetch player stats."))

with ps_col2:
    if st.session_state["player_stats_text"]:
        st.text(st.session_state["player_stats_text"])
    else:
        st.caption("No player stats loaded (fine for MVP).")

# ---- Opponent notes ----
st.subheader("4) Opponent notes (paste or auto-load team list)")
opponent_notes = st.text_area(
    "Opponent notes",
    "Strong middle rotation, likely ruck-focused and field-position driven.",
    height=120,
)

# ---- Fetch Team Lists section (shaded) ----
st.markdown(
    """
<div style="background:#f2f6ff; border:1px solid #d6e4ff; padding:14px; border-radius:10px;">
  <h3 style="margin:0 0 8px 0;">5) 🔎 Fetch Team Lists (recommended)</h3>
  <div style="margin-bottom:8px;">
    This pulls the latest <b>NRL Team Lists</b> article and tries to extract the matchup for your next fixture.
    If lists aren’t published yet, it will tell you.
  </div>
</div>
""",
    unsafe_allow_html=True,
)

# Determine home/away short names for team lists extraction
# If home/away unknown (fallback mode), we still attempt using "team v opponent" guess.
home_short = TEAM_SHORT.get(home_team, TEAM_SHORT.get(team, team)) if home_team != "Unknown" else TEAM_SHORT.get(team, team)
away_short = TEAM_SHORT.get(away_team, TEAM_SHORT.get(opponent, opponent)) if away_team != "Unknown" else TEAM_SHORT.get(opponent, opponent)

# If home/away unknown, pick an ordering guess where selected team is home for scraping target
if home_team == "Unknown" and away_team == "Unknown":
    home_short = TEAM_SHORT.get(team, team)
    away_short = TEAM_SHORT.get(opponent, opponent)

btn_col1, btn_col2 = st.columns([1, 2], gap="large")
with btn_col1:
    if st.button("📥 Fetch latest Team Lists and extract this match"):
        try:
            article_url = find_latest_team_lists_article_url()
            data = extract_match_team_list(article_url, home_short, away_short)

            st.write("Team Lists article:", data.get("article_url"))
            st.write("Match heading:", data.get("match_heading"))
            st.write("Players found:", len(data.get("players")))
            if data.get("note"):
                st.info(data["note"])

            away_players = [p for p in data.get("players", []) if p.get("side") == "away"]
            home_players = [p for p in data.get("players", []) if p.get("side") == "home"]

            if home_players:
                st.session_state["team_list_players_by_team"][team] = _unique_preserve_order([p["name"] for p in sorted(home_players, key=lambda p: p["number"])])
            if away_players:
                st.session_state["team_list_players_by_team"][opponent] = _unique_preserve_order([p["name"] for p in sorted(away_players, key=lambda p: p["number"])])
                bullets = "\n".join([f"- #{p['number']} {p['name']} ({p['position']})" for p in away_players])
                st.session_state["scraped_opponent_list"] = bullets
                st.success("Loaded opponent team list.")
            else:
                st.session_state["scraped_opponent_list"] = ""
                st.warning("No away-team player lines extracted (could be article format).")

            with st.expander("Preview extracted players"):
                st.json(data.get("players", [])[:40])

        except Exception as e:
            st.error(str(e))

with btn_col2:
    if st.session_state["scraped_opponent_list"]:
        st.markdown("**Scraped opponent team list:**")
        st.text(st.session_state["scraped_opponent_list"])
    else:
        st.caption("No scraped team list loaded yet.")

# ---- Generate report ----
st.subheader("6) Generate tactical brief")

generate = st.button("✅ Generate Tactical Brief", type="primary")

if generate:
    try:
        notes_for_generation = opponent_notes.strip()

        if st.session_state["scraped_opponent_list"]:
            if "OPPONENT TEAM LIST" not in notes_for_generation:
                notes_for_generation += "\n\nOPPONENT TEAM LIST (scraped):\n" + st.session_state["scraped_opponent_list"]

        payload = {
            "player": player,
            "team": team,
            "position": position,
            "opponent": opponent,
            "round_label": round_label,
            "home_team": home_team,
            "away_team": away_team,
            "venue": venue,
            "match_url": match_url,
            "player_stats": st.session_state["player_stats_text"] or "Unknown",
            "opponent_notes": notes_for_generation or "Unknown",
            "risk": risk,
            "style_pref": style_pref,
        }

        with st.spinner("Generating..."):
            report = generate_brief(model=model, payload=payload)

        st.markdown(report)
        st.download_button(
            "Download report.md",
            data=report.encode("utf-8"),
            file_name=f"nrl_profiler_{player.replace(' ', '_').lower()}.md",
            mime="text/markdown",
        )

    except Exception as e:
        st.error(str(e))
        st.info("Check OPENAI_API_KEY is set. If model errors, switch it in the sidebar.")
