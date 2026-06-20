"""
MovieCove — MovieBox API wrapper
Powered by movie-box-dl (https://github.com/parthmax2/movie-box)
FastAPI server. Deploy on Render (free tier).

IMPORTANT: MovieBoxHttpClient must be opened with `async with` for every
request — it has no usable state until __aenter__ runs (that's where the
underlying httpx.AsyncClient is actually created). A single shared
long-lived client is not used here; instead each request opens and closes
its own client. This avoids "Client not started" errors and avoids
sharing one httpx connection pool across concurrent requests in a way that
could break under load.
"""

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from movie_box.v3.http_client import MovieBoxHttpClient
from movie_box.v3.constants import SubjectType, TabID, CustomResolutionType
from movie_box.v3.core import (
    Homepage,
    Search,
    ItemDetails,
    SeasonDetails,
    DownloadableVideoFilesDetail,
    DownloadableCaptionFileDetails,
)
from movie_box.v3.exceptions import ZeroSearchResultsError

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="MovieCove API",
    version="2.0.1",
    description="MovieBox proxy API for MovieCove, powered by movie-box-dl.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # restrict to your domain in production
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────
def cover_url(cover: Any) -> str | None:
    if cover is None:
        return None
    if hasattr(cover, "url"):
        return str(cover.url)
    if isinstance(cover, dict):
        return cover.get("url")
    return None


def fmt_search_item(item: Any) -> dict:
    return {
        "id": item.subject_id,
        "title": item.title,
        "description": item.description,
        "poster": cover_url(item.cover),
        "release_date": str(item.release_date) if item.release_date else None,
        "duration": item.duration,
        "genre": item.genre if isinstance(item.genre, list) else [item.genre],
        "rating": item.imdb_rating_value,
        "type": item.subject_type.name.lower(),
        "country": item.country_name,
        "language": item.language,
        "season_count": item.season_numbers,
        "category": item.category,
    }


def fmt_detail(detail: Any) -> dict:
    streams: list[dict] = []
    for det in getattr(detail, "resource_detectors", []) or []:
        for res in getattr(det, "resolution_list", []) or []:
            streams.append({
                "url": str(res.resource_link),
                "resolution": res.resolution.value
                    if hasattr(res.resolution, "value") else int(res.resolution),
                "title": res.title,
                "size": res.size,
                "codec": res.codec_name,
                "season": res.se,
                "episode": res.ep,
            })

    return {
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
        "streams": streams,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "message": "MovieCove API is running", "engine": "movie-box-dl"}


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok"}


@app.get("/search", tags=["Content"])
async def search(
    q: str = Query(..., min_length=1, max_length=120),
    type: str = Query("all", description="all | movie | tv_series"),
    page: int = Query(1, ge=1, le=50),
    per_page: int = Query(20, ge=1, le=50),
):
    """Search movies and TV series. Returns poster URLs, ratings, descriptions."""
    type_map = {
        "movie": SubjectType.MOVIES,
        "movies": SubjectType.MOVIES,
        "tv_series": SubjectType.TV_SERIES,
        "tv": SubjectType.TV_SERIES,
        "anime": SubjectType.ANIME,
        "music": SubjectType.MUSIC,
        "all": SubjectType.ALL,
    }
    subject_type = type_map.get(type.lower(), SubjectType.ALL)

    try:
        async with MovieBoxHttpClient() as client:
            searcher = Search(
                client_session=client,
                query=q,
                subject_type=subject_type,
                page=page,
                per_page=per_page,
            )
            result = await searcher.get_content_model()
        return {
            "query": q,
            "page": result.pager.page,
            "per_page": result.pager.per_page,
            "total": result.pager.total_count,
            "has_more": result.pager.has_more,
            "results": [fmt_search_item(i) for i in result.items],
        }
    except ZeroSearchResultsError:
        return {
            "query": q, "page": page, "per_page": per_page,
            "total": 0, "has_more": False, "results": [],
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/movie/{subject_id}", tags=["Content"])
async def movie_details(subject_id: str):
    """Full details for a movie or TV series — poster, description, genre, rating."""
    try:
        async with MovieBoxHttpClient() as client:
            fetcher = ItemDetails(
                client_session=client,
                include_seasons=True,
            )
            detail = await fetcher.get_content_model(subject_id)
        return fmt_detail(detail)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/streams/{subject_id}", tags=["Content"])
async def stream_links(
    subject_id: str,
    quality: str = Query("best", description="best | 1080p | 720p | 480p | 360p"),
):
    """
    Get direct MP4 stream URLs for a movie or TV series.
    Returns all available resolutions.
    """
    quality_map = {
        "best": CustomResolutionType.BEST,
        "1080p": CustomResolutionType._1080P,
        "720p": CustomResolutionType._720P,
        "480p": CustomResolutionType._480P,
        "360p": CustomResolutionType._360P,
        "worst": CustomResolutionType.WORST,
    }
    resolution = quality_map.get(quality.lower(), CustomResolutionType.BEST)

    try:
        async with MovieBoxHttpClient() as client:
            dl = DownloadableVideoFilesDetail(
                client_session=client,
                resolution=resolution,
            )
            result = await dl.get_content_model(subject_id)

        streams = []
        for item in result.list or []:
            streams.append({
                "url": str(item.resource_link),
                "title": item.title,
                "size": item.size,
                "resolution": item.resolution,
                "codec": item.codec_name,
                "season": item.season,
                "episode": item.episode,
            })

        return {
            "subject_id": subject_id,
            "title": result.subject_title,
            "poster": cover_url(result.cover),
            "total_episodes": result.total_episode,
            "streams": streams,
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/seasons/{subject_id}", tags=["Content"])
async def seasons(subject_id: str):
    """Season and episode count for a TV series."""
    try:
        async with MovieBoxHttpClient() as client:
            fetcher = SeasonDetails(client_session=client)
            result = await fetcher.get_content_model(subject_id)
        return {
            "subject_id": subject_id,
            "total_seasons": result.total_seasons,
            "seasons": [
                {
                    "season": s.se,
                    "episode_count": s.max_ep,
                }
                for s in result.seasons or []
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/home", tags=["Content"])
async def homepage(
    tab: str = Query("all", description="all | movie | tv_series"),
    page: int = Query(1, ge=1, le=10),
):
    """
    Trending / homepage content. Change page (1–10) to get different sets
    on each refresh — perfect for the MovieCove home rows.
    """
    tab_map: dict[str, int | TabID] = {
        "all": 0,
        "movie": TabID.MOVIE,
        "tv_series": TabID.TV_SERIES,
        "tv": TabID.TV_SERIES,
    }
    tab_id = tab_map.get(tab.lower(), 0)

    try:
        async with MovieBoxHttpClient() as client:
            home = Homepage(client_session=client)
            home._page_number = page
            home._tab_id = tab_id
            result = await home.get_content_model()

        items = []
        for topic in result.topics or []:
            for subject in getattr(topic, "subjects", []) or []:
                items.append({
                    "id": subject.subject_id,
                    "title": subject.title,
                    "poster": cover_url(subject.cover),
                    "release_date": str(subject.release_date)
                        if subject.release_date else None,
                    "genre": subject.genre
                        if isinstance(subject.genre, list) else [subject.genre],
                    "type": subject.subject_type.name.lower(),
                })

        return {"tab": tab, "page": page, "items": items}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/subtitles/{subject_id}", tags=["Content"])
async def subtitles(
    subject_id: str,
    resource_id: str = Query(..., description="resource_id from /streams response"),
):
    """
    Get subtitle URLs for a video file.
    Pass the resource_id from a /streams result.
    """
    try:
        async with MovieBoxHttpClient() as client:
            caps = DownloadableCaptionFileDetails(client_session=client)
            result = await caps.get_content_model(subject_id, resource_id)
        return {
            "subject_id": subject_id,
            "subtitles": [
                {
                    "language": cap.lan_name,
                    "code": cap.lan,
                    "url": str(cap.url),
                    "size": cap.size,
                }
                for cap in result.external_captions or []
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
