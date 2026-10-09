# Touch Grass Planner

Tell it how many minutes you have between classes. It finds parks, water and trees near you
(OpenStreetMap), checks the weather (Open-Meteo), picks a loop that fits your time, and an
open-weight model writes a short plan with a "what to notice" checklist. Then it asks you to put
the phone away and starts a timer.

## Why open matters here
- **Private:** your location goes to a model you run (Ollama on your laptop, or your own Render
  service), not to a closed API.
- **Free to run:** OpenStreetMap, Open-Meteo and an open-weight model cost nothing per request.
- **Swappable:** change `LLM_MODEL` to try Qwen, Llama, Gemma or Phi without touching code.
- **Resilient:** if the model, map or weather service is down, the app falls back and says so.

## Run locally
```bash
ollama pull qwen2.5:3b
pip install -r requirements.txt
uvicorn app.main:app --reload
# open http://localhost:8000
```
Browser location needs HTTPS or localhost.

## Deploy on Render
1. Push this folder to GitHub, then create a Blueprint from `render.yaml`.
2. Point the web service at a model host:
   - **A:** set `LLM_BASE_URL` (and `LLM_API_KEY` if needed) to any OpenAI-compatible host serving an open-weight model.
   - **B:** uncomment the `ollama` private service in `render.yaml` (needs a paid instance plus a disk) and the `LLM_HOSTPORT` env var. Untested, so check the logs on first boot.
   - **C:** for Backboard, set `BACKBOARD_API_KEY` in the Render service's environment settings. The key is kept server-side; do not add it to the repository. Backboard defaults to `openai` / `gpt-4o`; optionally set `BACKBOARD_LLM_PROVIDER` and `BACKBOARD_LLM_MODEL` to a model enabled for your account. When configured, Backboard takes priority over `LLM_BASE_URL`.
3. Free instances sleep when idle, so the first request after a break is slow.

## Config
| Variable | Default |
|---|---|
| `LLM_BASE_URL` | `http://localhost:11434/v1` |
| `LLM_MODEL` | `qwen2.5:3b` |
| `LLM_API_KEY` | `ollama` |
| `LLM_HOSTPORT` | unset (overrides base URL) |
| `BACKBOARD_API_KEY` | unset (when set, uses Backboard instead of `LLM_BASE_URL`) |
| `BACKBOARD_LLM_PROVIDER` | `openai` |
| `BACKBOARD_LLM_MODEL` | `gpt-4o` |

## Known limits
- Loops are built from straight-line distances times 1.3. The dashed map line is a rough shape; the directions link gives real streets.
- Public Overpass servers can be slow or rate-limited. Results are cached for 10 minutes.
- Default start point is the Chandigarh University campus. Edit `DEFAULT` in `static/index.html`.
