"""
MovieBox API — FastAPI wrapper for MovieCove
Deploy on Render (free tier). Exposes search, details,
stream links and homepage content from the MovieBox backend.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from moviebox_api.v3 import MovieBoxHttpClient
from moviebox_api.v3.constants import SubjectType, TabID
from moviebox_api.v3.core import Homepage, Search, SearchV2
from moviebox_api.v3.exceptions import ZeroSearchResultsError

# ── Shared HTTP client (one per worker process) ──────────────────────────────
_client: MovieBoxHttpClient | None = None


def get_client() -> MovieBoxHttpClient:
    global _client
    if _client is None:
        _client = MovieBoxHttpClient()
    return _client


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up the client on startup
    get_client()
    yield
    # Nothing to clean up — httpx closes connections on GC


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="MovieCove — MovieBox API",
    version="1.0.0",
    description="Proxy API wrapping the MovieBox backend for use with MovieCove.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # lock this down to your MovieCove domain in production
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────
def cover_url(cover: Any) -> str | None:
    """Safely extract a poster URL from a cover object."""
    if cover is None:
        return None
    if hasattr(cover, "url"):
        return str(cover.url)
    if isinstance(cover, dict):
        return cover.get("url")
    return None


def serialize_search_item(item: Any) -> dict:
    """Convert a search result item to a plain dict for the response."""
    return {
        "id": item.subject_id,
        "title": item.title,
        "description": item.description,
        "poster": cover_url(item.cover),
        "release_date": str(item.release_date) if item.release_date else None,
        "duration": item.duration,
        "genre": item.genre if isinstance(item.genre, list) else [item.genre],
        "rating": item.imdb_rating_value,
        "type": item.subject_type.name.lower(),   # "movie" | "tv_series"
        "country": item.country_name,
        "language": item.language,
        "season_count": item.season_numbers,
        "category": item.category,
    }


def serialize_detail(detail: Any) -> dict:
    """Convert an item-details model to a plain dict."""
    d: dict[str, Any] = {
        "id": detail.subject_id,
        "title": detail.title,
        "description": detail.description,
        "poster": cover_url(detail.cover),
        "release_date": str(detail.release_date) if detail.release_date else None,
        "duration": detail.duration,
        "genre": detail.genre if isinstance(detail.genre, list) else [detail.genre],
        "rating": detail.imdb_rating_value,
        "type": detail.subject_type.name.lower(),
        "country": detail.country_name,
        "language": detail.language,
        "season_count": detail.season_numbers,
        "category": detail.category,
        "streams": [],
        "subtitles": [],
    }

    # Flatten resource detectors → stream links per resolution
    streams: list[dict] = []
    for detector in getattr(detail, "resource_detectors", []) or []:
        for res in getattr(detector, "resolution_list", []) or []:
            streams.append({
                "url": str(res.resource_link),
                "resolution": res.resolution.value if hasattr(res.resolution, "value") else res.resolution,
                "title": res.title,
                "size": res.size,
                "codec": res.codec_name,
                "season": res.se,
                "episode": res.ep,
            })
    d["streams"] = streams

    # Subtitles
    subtitles: list[dict] = []
    for sub in getattr(detail, "subtitles", []) or []:
        if isinstance(sub, str):
            subtitles.append({"language": sub})
        elif hasattr(sub, "language"):
            subtitles.append({"language": sub.language, "url": str(getattr(sub, "url", ""))})
    d["subtitles"] = subtitles

    return d


def serialize_homepage_item(item: Any) -> dict:
    return {
        "id": item.subject_id,
        "title": item.title,
        "poster": cover_url(item.cover),
        "release_date": str(item.release_date) if item.release_date else None,
        "genre": item.genre if isinstance(item.genre, list) else [item.genre],
        "type": item.subject_type.name.lower(),
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "message": "MovieCove API is running"}


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok"}


@app.get("/search", tags=["Content"])
async def search(
    q: str = Query(..., min_length=1, max_length=120, description="Search query"),
    type: str = Query("all", description="Content type: all | movie | tv_series"),
    page: int = Query(1, ge=1, le=50),
    per_page: int = Query(20, ge=1, le=50),
):
    """
    Search for movies and TV series.

    Returns a list of results with poster URLs, ratings, descriptions etc.
    """
    subject_type_map = {
        "movie": SubjectType.MOVIE,
        "tv_series": SubjectType.TV_SERIES,
        "tv": SubjectType.TV_SERIES,
        "all": SubjectType.ALL,
    }
    subject_type = subject_type_map.get(type.lower(), SubjectType.ALL)

    try:
        searcher = Search(
            client_session=get_client(),
            query=q,
            subject_type=subject_type,
            page=page,
            per_page=per_page,
        )
        result = await searcher.get_content_model()
        items = [serialize_search_item(i) for i in result.items]
        return {
            "query": q,
            "page": result.pager.page,
            "per_page": result.pager.per_page,
            "total": result.pager.total_count,
            "has_more": result.pager.has_more,
            "results": items,
        }
    except ZeroSearchResultsError:
        return {"query": q, "page": page, "per_page": per_page, "total": 0, "has_more": False, "results": []}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {str(e)}")


@app.get("/movie/{subject_id}", tags=["Content"])
async def movie_details(subject_id: str):
    """
    Get full details for a movie or TV series by its MovieBox subject ID.

    Includes poster, description, genre, rating, IMDB rating, and direct
    stream URLs at multiple resolutions.
    """
    from moviebox_api.v3.core import ItemDetails

    try:
        detail_fetcher = ItemDetails(
            client_session=get_client(),
            subject_id=subject_id,
        )
        detail = await detail_fetcher.get_content_model()
        return serialize_detail(detail)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {str(e)}")


@app.get("/streams/{subject_id}", tags=["Content"])
async def stream_links(
    subject_id: str,
    season: int = Query(0, ge=0),
    episode: int = Query(0, ge=0),
):
    """
    Get direct MP4/stream URLs for a movie or a specific TV episode.

    - For movies: season=0, episode=0
    - For TV series: pass the correct season and episode numbers
    """
    from moviebox_api.v3.core import DownloadableFiles

    try:
        dl = DownloadableFiles(
            client_session=get_client(),
            subject_id=subject_id,
            season=season,
            episode=episode,
        )
        result = await dl.get_content_model()

        streams = []
        for item in result.list or []:
            streams.append({
                "url": str(item.resource_link),
                "title": item.title,
                "size": item.size,
                "season": item.se,
                "episode": item.ep,
                "resolution": item.resolution.value if hasattr(item.resolution, "value") else 0,
            })

        return {
            "subject_id": subject_id,
            "title": result.subject_title,
            "poster": cover_url(result.cover),
            "total_episodes": result.total_episode,
            "streams": streams,
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {str(e)}")


@app.get("/seasons/{subject_id}", tags=["Content"])
async def season_info(subject_id: str):
    """
    Get season and episode count information for a TV series.
    """
    from moviebox_api.v3.core import Seasons

    try:
        seasons_fetcher = Seasons(
            client_session=get_client(),
            subject_id=subject_id,
        )
        result = await seasons_fetcher.get_content_model()
        seasons = []
        for s in result.seasons or []:
            seasons.append({
                "season": s.season,
                "episode_count": s.ep_count,
                "title": s.title,
            })
        return {"subject_id": subject_id, "seasons": seasons}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {str(e)}")


@app.get("/home", tags=["Content"])
async def homepage(
    tab: str = Query("all", description="Tab: all | movie | tv_series | anime"),
    page: int = Query(1, ge=1, le=10),
):
    """
    Get homepage/trending content — same as what MovieBox shows on launch.

    Rotate the `page` parameter to get different sets on each load.
    """
    tab_map = {
        "all": 0,
        "movie": TabID.MOVIE,
        "tv_series": TabID.TV_SERIES,
        "tv": TabID.TV_SERIES,
        "anime": TabID.ALL,
    }
    tab_id = tab_map.get(tab.lower(), 0)

    try:
        home = Homepage(client_session=get_client())
        home._page_number = page
        home._tab_id = tab_id
        result = await home.get_content_model()

        items = []
        for topic in result.topics or []:
            for item in getattr(topic, "subjects", []) or []:
                items.append(serialize_homepage_item(item))

        return {
            "tab": tab,
            "page": page,
            "items": items,
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {str(e)}")


@app.get("/subtitles/{subject_id}", tags=["Content"])
async def subtitles(
    subject_id: str,
    season: int = Query(0, ge=0),
    episode: int = Query(0, ge=0),
    language: str = Query("English"),
):
    """
    Get subtitle download URL for a movie or TV episode.
    """
    from moviebox_api.v3.core import Captions

    try:
        caps = Captions(
            client_session=get_client(),
            subject_id=subject_id,
            season=season,
            episode=episode,
        )
        result = await caps.get_content_model()
        subs = []
        for cap in result.list or []:
            subs.append({
                "language": cap.language,
                "url": str(cap.url) if cap.url else None,
                "format": cap.format,
            })
        return {"subject_id": subject_id, "subtitles": subs}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {str(e)}")
