#!/usr/bin/env python3
"""
Fetches upcoming and recent game releases from IGDB (Twitch API).
Requires env vars: TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET.

Get credentials: https://dev.twitch.tv/console → register app → copy Client-ID + Secret
"""

import os
import json
import time
import requests
from datetime import datetime, timezone

CLIENT_ID     = os.environ["TWITCH_CLIENT_ID"]
CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]

PLATFORMS = {
    6:   "PC",
    130: "Switch",
    167: "PS5",
    169: "Xbox Series X|S",
    508: "Switch 2",
}
PLATFORM_IDS = ",".join(str(p) for p in PLATFORMS)

EXCLUDED_THEMES = {42}  # Erotic/Adult


# ── API ──────────────────────────────────────────────────────────────────────

def get_token():
    r = requests.post("https://id.twitch.tv/oauth2/token", params={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type":    "client_credentials",
    }, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def igdb(token, endpoint, query):
    r = requests.post(
        f"https://api.igdb.com/v4/{endpoint}",
        headers={
            "Client-ID":     CLIENT_ID,
            "Authorization": f"Bearer {token}",
            "Content-Type":  "text/plain",
        },
        data=query.strip().encode("utf-8"),
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    # IGDB devuelve errores de sintaxis como HTTP 200 con un objeto de error
    if data and isinstance(data, list) and "title" in data[0]:
        raise ValueError(f"IGDB query error: {data[0].get('title')} — {data[0].get('cause','')}\nQuery:\n{query}")
    return data


# ── Blocklist ─────────────────────────────────────────────────────────────────

def load_blocklist():
    path = os.path.join(os.path.dirname(__file__), "blocklist.json")
    if not os.path.exists(path):
        return set(), set()
    with open(path, encoding="utf-8") as f:
        bl = json.load(f)
    devs = {x.lower().strip() for x in bl.get("blocked_developers", [])}
    pubs = {x.lower().strip() for x in bl.get("blocked_publishers", [])}
    return devs, pubs


# ── Processing ────────────────────────────────────────────────────────────────

def cover_url(raw, size="t_cover_big"):
    if not raw:
        return None
    return "https:" + raw.lstrip("/").replace("images.igdb.com", "//images.igdb.com", 1) \
        if not raw.startswith("http") else raw
    # simpler: just do the thumb→size replacement
def cover_url(raw, size="t_cover_big"):
    if not raw:
        return None
    url = raw if raw.startswith("http") else "https:" + raw
    return url.replace("/t_thumb/", f"/{size}/")


def get_companies(token, game_ids):
    """Fetch developer/publisher names via the involved_companies endpoint."""
    if not game_ids:
        return {}
    ids_str = ",".join(str(i) for i in game_ids)
    query = f"""
fields game, company.name, developer, publisher;
where game = ({ids_str});
limit 500;
"""
    try:
        rows = igdb(token, "involved_companies", query)
    except Exception as e:
        print(f"  ⚠ involved_companies fetch failed: {e}")
        return {}

    result = {}  # game_id → {developer, publisher}
    for row in rows:
        gid = row.get("game")
        if not gid:
            continue
        company_name = row.get("company", {}).get("name") if row.get("company") else None
        if gid not in result:
            result[gid] = {"developer": None, "publisher": None}
        if row.get("developer") and not result[gid]["developer"]:
            result[gid]["developer"] = company_name
        if row.get("publisher") and not result[gid]["publisher"]:
            result[gid]["publisher"] = company_name
    return result


def process(g, companies):
    platforms = sorted({
        PLATFORMS[p["id"]]
        for p in g.get("platforms", [])
        if p.get("id") in PLATFORMS
    })

    genres = [x["name"] for x in g.get("genres", [])][:3]
    themes = [t["id"] for t in g.get("themes", [])]
    trailer = g["videos"][0]["video_id"] if g.get("videos") else None

    score = None
    if g.get("total_rating") and g.get("total_rating_count", 0) >= 5:
        score = round(g["total_rating"])

    summary = (g.get("summary") or "")[:320]

    co = companies.get(g["id"], {})

    return {
        "id":           g["id"],
        "name":         g["name"],
        "cover":        cover_url(g["cover"]["url"]) if g.get("cover") else None,
        "cover_sm":     cover_url(g["cover"]["url"], "t_cover_small") if g.get("cover") else None,
        "release_date": g.get("first_release_date"),
        "platforms":    platforms,
        "genres":       genres,
        "themes":       themes,
        "developer":    co.get("developer"),
        "publisher":    co.get("publisher"),
        "score":        score,
        "summary":      summary,
        "trailer":      trailer,
        "follows":      g.get("follows") or 0,
    }


def is_quality(game, blocked_devs, blocked_pubs):
    if not game["cover"]:
        return False
    if any(t in EXCLUDED_THEMES for t in game["themes"]):
        return False
    if not game["platforms"]:
        return False
    dev = (game["developer"] or "").lower().strip()
    pub = (game["publisher"] or "").lower().strip()
    if dev and dev in blocked_devs:
        return False
    if pub and pub in blocked_pubs:
        return False
    return True


# ── Queries ───────────────────────────────────────────────────────────────────

GAME_FIELDS = """fields name, cover.url, first_release_date,
  platforms.id, platforms.name,
  genres.name, themes.id,
  total_rating, total_rating_count,
  summary, videos.video_id, follows;"""


def fetch_games(token, after, before, sort="first_release_date asc", limit=200):
    query = f"""{GAME_FIELDS}
where first_release_date >= {after}
  & first_release_date <= {before}
  & platforms = ({PLATFORM_IDS});
sort {sort};
limit {limit};"""
    return igdb(token, "games", query)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("→ Cargando blocklist…")
    blocked_devs, blocked_pubs = load_blocklist()

    print("→ Obteniendo token de IGDB…")
    token = get_token()

    # Test rápido de conectividad
    test = igdb(token, "games", "fields name; where id = 1942; limit 1;")
    print(f"  Test API: {[g.get('name') for g in test]}")

    now = int(time.time())

    print("→ Fetching próximos lanzamientos…")
    raw_upcoming = fetch_games(token, now, now + 120 * 86400)
    print(f"  IGDB devuelve {len(raw_upcoming)} juegos próximos")

    print("→ Fetching lanzamientos recientes…")
    raw_recent = fetch_games(token, now - 30 * 86400, now - 1,
                             sort="first_release_date desc", limit=100)
    print(f"  IGDB devuelve {len(raw_recent)} juegos recientes")

    # Obtener developer/publisher en batch
    all_ids = [g["id"] for g in raw_upcoming + raw_recent]
    print(f"→ Fetching companies para {len(all_ids)} juegos…")
    companies = get_companies(token, all_ids)

    upcoming = [process(g, companies) for g in raw_upcoming if g.get("first_release_date")]
    upcoming = [g for g in upcoming if is_quality(g, blocked_devs, blocked_pubs)]
    upcoming.sort(key=lambda g: g["release_date"])

    recent = [process(g, companies) for g in raw_recent if g.get("first_release_date")]
    recent = [g for g in recent if is_quality(g, blocked_devs, blocked_pubs)]

    print(f"  Próximos: {len(upcoming)} · Recientes: {len(recent)}")

    data = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "upcoming":   upcoming,
        "recent":     recent,
    }

    out = os.path.join(os.path.dirname(__file__), "releases.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✓ releases.json actualizado — {len(upcoming)} próximos · {len(recent)} recientes")


if __name__ == "__main__":
    main()
