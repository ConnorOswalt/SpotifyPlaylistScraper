# Spotify Playlist Scraper

Syncs a Spotify playlist to Jellyfin by checking your local library for each track and automatically acquiring any missing ones via [slskd](https://github.com/slskd/slskd) (a Soulseek daemon with a REST API).

```
[ Spotify API ] ──> Get Playlist Tracks
                          │
                          ▼
[ Jellyfin API ] ──> Search Local Library ──(Found)──> Add to Jellyfin Playlist
                          │
                      (Missing)
                          ▼
 [ slskd API ] ───> Search & Download ───> Move to Library ───> Refresh Jellyfin
```

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | Tested on 3.11 and 3.12 |
| [Jellyfin](https://jellyfin.org/) | Running and reachable from this machine |
| [Docker](https://www.docker.com/) | For running slskd |
| Spotify developer app | Free — [create one here](https://developer.spotify.com/dashboard) |

---

## Setup

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Start slskd

```bash
docker compose up -d
```

Open `http://localhost:5030`, log in with the default credentials (`slskd` / `slskd`), and connect your Soulseek account under **Settings → Soulseek**.

### 3. Configure

Edit `config.yaml` with your credentials.  Every key is documented inline.

| Key | Where to find it |
|---|---|
| `spotify.client_id` / `client_secret` | [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) → your app → Settings |
| `jellyfin.api_key` | Jellyfin → Dashboard → Advanced → API Keys → **+** |
| `jellyfin.user_id` | Jellyfin → Dashboard → Users → click your user → copy the UUID from the browser URL |
| `slskd.api_key` | slskd web UI → Settings → Security → add an API key |

> **Path note:** `slskd.download_dir` and `library.music_dir` must be the paths
> as seen **from the machine running this script**.  The `docker-compose.yml`
> maps `./downloads` → `/downloads` (inside the container) and `./music` →
> `/music`.  If your Jellyfin library lives somewhere else, update both the
> compose file and `library.music_dir`.

### 4. Run

```bash
# Full sync: match library, download missing tracks
python main.py \
    --spotify-playlist "https://open.spotify.com/playlist/37i9dQZF1DX..." \
    --jellyfin-playlist "My Mix"

# Dry run — print track list only, make no changes
python main.py --spotify-playlist <id> --jellyfin-playlist <name> --dry-run

# Library-only — match existing tracks, skip all downloads
python main.py --spotify-playlist <id> --jellyfin-playlist <name> --no-download

# Verbose output
python main.py --spotify-playlist <id> --jellyfin-playlist <name> --log-level DEBUG
```

On first run spotipy will open a browser tab for Spotify OAuth.  After you
approve, the token is cached in `.spotify_cache` so subsequent runs are
headless.

---

## How it works

1. **Spotify Fetcher** (`src/spotify.py`) — paginates the full playlist and collects the track title, artist(s), album name, ISRC, and duration for every item.

2. **Jellyfin Matcher** (`src/jellyfin.py`) — searches `/Items` by title and cross-checks the artist list.  On a hit the item is immediately added to the target playlist.

3. **slskd Acquirer** (`src/slskd.py`) — for unmatched tracks:
   - POSTs a search to `/api/v0/searches` and polls until it completes.
   - Filters results: rejects wrong formats, low-bitrate MP3s, and paths containing telltale strings like `youtube`, `(rip)`, `radio edit`, etc.
   - Ranks survivors: free-slot peers first → album path match → FLAC over MP3 → highest bitrate.
   - Downloads the best candidate via `/api/v0/transfers/downloads/{username}` and polls until the transfer reaches a terminal state.

4. **Library Manager** (`src/library.py`) — moves the completed file to `<music_dir>/<Artist>/<Album>/`, creating the directory tree as needed.

5. **Handshake** (`main.py`) — fires `POST /Library/Refresh` to Jellyfin, waits `scan_wait` seconds, then searches for the new item and adds it to the playlist.

---

## Project structure

```
SpotifyPlaylistScraper/
├── main.py              # Orchestrator & CLI entry point
├── config.yaml          # All credentials and tuning parameters
├── requirements.txt
├── docker-compose.yml   # slskd container
└── src/
    ├── spotify.py       # Spotify playlist fetcher
    ├── jellyfin.py      # Jellyfin library search & playlist management
    ├── slskd.py         # Soulseek search, filtering, and download
    └── library.py       # File move into Jellyfin library layout
```
