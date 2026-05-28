#!/usr/bin/env python3
"""
Fetches upcoming and recent game releases from IGDB (Twitch API).
Requires env vars: TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET.

Get credentials: https://dev.twitch.tv/console → register app → copy Client-ID + Secret

Filters applied:
  - Plataformas principales solamente (PC, consolas, no móvil de base)
  - Sin contenido NSFW / erótico (tema IGDB 42)
  - Sin juegos sin portada en IGDB (señal de shovelware)
  - Sin juegos con demasiado bajo interés en plataformas de una sola plataforma
  - Blocklist manual en blocklist.json (desarrolladoras con IA generativa, etc.)
"""

import os
import json
import time
import requests
from datetime import datetime, timezone

CLIENT_ID     = os.environ["TWITCH_CLIENT_ID"]
CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]

# ── Plataformas que nos interesan ───────────────────────────────────────────
PLATFORMS = {
    6:   "PC",
    48:  "PS4",
    49:  "Xbox One",
    130: "Switch",
    167: "PS5",
    169: "Xbox Series X|S",
}
PLATFORM_IDS = ",".join(str(p) for p in PLATFORMS)

# ── Thresholds de calidad ────────────────────────────────────────────────────
# Juegos próximos: mínimo hypes en IGDB para aparecer (filtra shovelware sin audiencia)
MIN_HYPES_UPCOMING = 3
# Juegos recientes: mínimo de votos/reviews en IGDB para aparecer
MIN_RATING_COUNT_RECENT = 5
# Si el juego solo aparece en PC y tiene 0 hypes → probablemente shovelware
PC_ONLY_MIN_HYPES = 5

# ── IGDB theme IDs a excluir ─────────────────────────────────────────────────
# 42 = Erotic / Adult, también filtramos otros themes problemáticos si los hay
EXCLUDED_THEMES = [42]

FIELDS = """
fields name, cover.url, first_release_date,
       platforms.id, platforms.name,
       genres.name, themes.id,
       involved_companies.company.name,
       involved_companies.company.published,
       involved_companies.developer,
       involved_companies.publisher,
       total_rating, total_rating_count,
       summary, videos.video_id,
       hypes, follows;
"""

# ── Blocklist ────────────────────────────────────────────────────────────────
def load_blocklist():
    path = os.path.join(os.path.dirname(__file__), "blocklist.json")
    if not os.path.exists(path):
        return set(), set()
    with open(path, encoding="utf-8") as f:
        bl = json.load(f)
    devs  = {x.lower().strip() for x in bl.get("blocked_developers", [])}
    pubs  = {x.lower().strip() for x in bl.get("blocked_publishers", [])}
    return devs, pubs


def get_token():
    r = requests.post("https://id.twitch.tv/oauth2/token", params={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type":    "client_credentials",
    }, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def igdb(token, endpoint, body):
    r = requests.post(
        f"https://api.igdb.com/v4/{endpoint}",
        headers={
            "Client-ID":     CLIENT_ID,
            "Authorization": f"Bearer {token}",
        },
        data=body,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def cover_url(raw, size="t_cover_big"):
    if not raw:
        return None
    return raw.replace("//", "https://").replace("t_thumb", size)


def process(g):
    platforms = sorted({
        PLATFORMS[p["id"]]
        for p in g.get("platforms", [])
        if p["id"] in PLATFORMS
    })

    developer = None
    publisher = None
    for c in g.get("involved_companies", []):
        co = c.get("company", {})
        if c.get("developer") and not developer:
            developer = co.get("name")
        if c.get("publisher") and not publisher:
            publisher = co.get("name")

    genres = [x["name"] for x in g.get("genres", [])][:3]
    trailer = g["videos"][0]["video_id"] if g.get("videos") else None

    score = None
    if g.get("total_rating") and g.get("total_rating_count", 0) >= 5:
        score = round(g["total_rating"])

    summary = g.get("summary") or ""
    if len(summary) > 320:
        summary = summary[:317] + "…"

    return {
        "id":           g["id"],
        "name":         g["name"],
        "cover":        cover_url(g["cover"]["url"]) if g.get("cover") else None,
        "cover_sm":     cover_url(g["cover"]["url"], "t_cover_small") if g.get("cover") else None,
        "release_date": g.get("first_release_date"),
        "platforms":    platforms,
        "genres":       genres,
        "developer":    developer,
        "publisher":    publisher,
        "score":        score,
        "summary":      summary,
        "trailer":      trailer,
        "hypes":        g.get("hypes", 0) or 0,
        "follows":      g.get("follows", 0) or 0,
    }


def is_quality(game, blocked_devs, blocked_pubs, mode="upcoming"):
    """Devuelve True si el juego pasa todos los filtros de calidad."""

    # Sin portada → probablemente sin datos reales
    if not game["cover"]:
        return False

    # Comprueba blocklist (AI covers, publishers problemáticos)
    dev = (game["developer"] or "").lower().strip()
    pub = (game["publisher"] or "").lower().strip()
    if dev and dev in blocked_devs:
        return False
    if pub and pub in blocked_pubs:
        return False

    hypes = game["hypes"]

    if mode == "upcoming":
        # Si solo está en PC y tiene muy pocos hypes → shovelware probable
        if game["platforms"] == ["PC"] and hypes < PC_ONLY_MIN_HYPES:
            return False
        # Cualquier juego en consola pasa (hay menos shovelware de consola)
        # Juego multi-plataforma → pasa siempre
        if len(game["platforms"]) > 1:
            return True
        # Juego de una sola plataforma → necesita mínimo de hypes
        return hypes >= MIN_HYPES_UPCOMING

    else:  # recent
        # Para recientes, filtramos los sin ningún score ni seguimiento
        rating_count = 0  # no lo guardamos en process(), usamos hypes como proxy
        return hypes >= 1 or (game["score"] is not None)


def fetch_upcoming(token):
    now = int(time.time())
    excluded = ",".join(str(t) for t in EXCLUDED_THEMES)
    body = f"""
{FIELDS}
where first_release_date >= {now}
  & first_release_date <= {now + 120 * 86400}
  & category = 0
  & version_parent = null
  & cover != null
  & themes != ({excluded})
  & platforms = ({PLATFORM_IDS});
sort first_release_date asc;
limit 200;
"""
    return igdb(token, "games", body)


def fetch_recent(token):
    now = int(time.time())
    excluded = ",".join(str(t) for t in EXCLUDED_THEMES)
    body = f"""
{FIELDS}
where first_release_date >= {now - 30 * 86400}
  & first_release_date < {now}
  & category = 0
  & version_parent = null
  & cover != null
  & themes != ({excluded})
  & platforms = ({PLATFORM_IDS});
sort first_release_date desc;
limit 100;
"""
    return igdb(token, "games", body)


def main():
    print("→ Cargando blocklist…")
    blocked_devs, blocked_pubs = load_blocklist()
    print(f"  {len(blocked_devs)} devs bloqueados · {len(blocked_pubs)} publishers bloqueados")

    print("→ Obteniendo token de IGDB…")
    token = get_token()

    print("→ Fetching próximos lanzamientos…")
    raw_upcoming = fetch_upcoming(token)
    upcoming = [process(g) for g in raw_upcoming if g.get("first_release_date")]
    upcoming = [g for g in upcoming if is_quality(g, blocked_devs, blocked_pubs, "upcoming")]
    upcoming.sort(key=lambda g: g["release_date"])

    print("→ Fetching lanzamientos recientes…")
    raw_recent = fetch_recent(token)
    recent = [process(g) for g in raw_recent if g.get("first_release_date")]
    recent = [g for g in recent if is_quality(g, blocked_devs, blocked_pubs, "recent")]

    # Estadísticas de filtrado
    filtered_up  = len(raw_upcoming) - len(upcoming)
    filtered_rec = len(raw_recent) - len(recent)
    print(f"  Próximos: {len(upcoming)} juegos ({filtered_up} filtrados)")
    print(f"  Recientes: {len(recent)} juegos ({filtered_rec} filtrados)")

    data = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "upcoming":   upcoming,
        "recent":     recent,
    }

    out = os.path.join(os.path.dirname(__file__), "releases.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✓ releases.json actualizado con {len(upcoming)} próximos y {len(recent)} recientes.")


if __name__ == "__main__":
    main()
