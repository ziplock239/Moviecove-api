# MovieCove API

A FastAPI wrapper around the [moviebox-api](https://github.com/Simatwa/moviebox-api) library,
deployed on Render for use with MovieCove.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/search?q=Avatar&type=movie` | Search movies & TV series |
| GET | `/movie/{subject_id}` | Full details + stream links |
| GET | `/streams/{subject_id}?season=1&episode=1` | Direct MP4 stream URLs |
| GET | `/seasons/{subject_id}` | Season/episode list for series |
| GET | `/home?tab=movie&page=1` | Homepage/trending content |
| GET | `/subtitles/{subject_id}` | Subtitle download URLs |

## Deploy on Render

1. Push this repo to GitHub
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your GitHub repo
4. Render auto-detects `render.yaml` — click **Deploy**
5. Your API will be live at `https://moviecove-api.onrender.com`

## Local dev

```bash
pip install -r requirements.txt
uvicorn main:app --reload
# Visit http://localhost:8000/docs for interactive API docs
```
