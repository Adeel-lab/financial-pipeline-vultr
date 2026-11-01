# FinAnalyst backend orchestration

Runs the whole FinAnalyst pipeline as **jobs**. One job = one company.

```
 upload 2 CSVs + company + question
        |
        +--> financial_gate --report-only --> [YOU approve] --> financial_gate --decisions --> approved_financials.json
        |
        +--> discovery -> triage -> resolve -> verify -> sentiment                           --> sentiment folder
                                         both branches done
                                                |
                                      swot_builder build  --> swot_draft.json --> [YOU review] --> swot_final.json
                                                                                         |
                                                                                  rca.py (items you pick)
```

The two branches run at the same time. The SWOT build starts by itself as soon as both are ready.
The two human steps (approve the financial mapping, review the SWOT) are real pauses: nothing is auto-approved.

## 1. Install

Copy `app.py`, `requirements-backend.txt` and the `backend/` folder into your project root
(`C:\Users\adeel\Desktop\notebooks\vultr`, next to `information_new/` and `planning/`). Then:

```powershell
cd C:\Users\adeel\Desktop\notebooks\vultr
pip install -r requirements-backend.txt
```

Add the lines from `.gitignore.additions` to your `.gitignore` (job folders, databases and `.env` must not be committed).

## 2. Run

```powershell
$env:VULTR_INFERENCE_BASE    = "https://api.groq.com/openai/v1"     # at the hackathon: https://api.vultrinference.com/v1
$env:VULTR_INFERENCE_API_KEY = "<your key>"
$env:VULTR_CHAT_MODEL        = "openai/gpt-oss-120b"
python app.py
```

Open http://127.0.0.1:8000/docs for interactive docs and http://127.0.0.1:8000/health to check the setup
(all eight scripts should show `true`, and `llm_key_configured` should be `true`).

The orchestrator copies your three `VULTR_*` settings to the names `rca.py` needs (`GROQ_API_KEY`, `LLM_API_KEY`,
`LLM_BASE_URL`, `LLM_MODEL`), so RCA uses the same key and model as everything else. If `planning\.env` or a root `.env`
exists it is read too; variables set in the terminal always win.

## 3. Use it (step by step)

Every command below also exists in the interactive docs. `<id>` is the job id returned by step 1.

**1. Start a job** (multipart form; the CSVs are optional, but give both or neither)

```powershell
curl.exe -X POST http://127.0.0.1:8000/jobs `
  -F "company=General Motors" -F "ticker=GM" -F "objective=Assess expansion readiness" `
  -F "units=thousands" -F "currency=USD" `
  -F "balance=@assets\GM_balance_sheet_annual_2020_2026.csv" `
  -F "income=@assets\GM_income_statement_annual_2020_2026.csv"
```

Other optional fields: `location` (places separated by `;`), `alias`, `exclude`, `days` (1-90), `strict_location`, `edition`,
`frequency`, `entity`, `no_llm`, `peers` (a JSON file of peer benchmark values).

**2. Watch progress**

```powershell
curl.exe http://127.0.0.1:8000/jobs/<id>                 # status, progress, every stage, and which actions are available
curl.exe http://127.0.0.1:8000/jobs/<id>/events          # timeline
curl.exe http://127.0.0.1:8000/jobs/<id>/logs/verify     # raw log of one stage
```

**3. Approve the financial mapping.** When `status` is `awaiting_finance_approval`:

```powershell
curl.exe http://127.0.0.1:8000/jobs/<id>/finance/report
```

Fields with `"needs_choice": true` need a decision (several rows disagree). Send the exact row label you pick:

```powershell
$body = @{ approved = $true; choices = @{ short_debt = "DebtCurrent" } } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/jobs/<id>/finance/approve -ContentType "application/json" -Body $body
```

If the report is `blocked`, upload corrected files with `POST /jobs/<id>/finance/files` (fields `balance`, `income`).

**4. Review the SWOT.** When `status` is `awaiting_swot_review`: `GET /jobs/<id>/swot/draft`, then send decisions
(`approve`, `edit` with a `quadrant`, or `reject`) for the items you have judged. Items you skip stay `pending`.

```powershell
$body = @{ reviewer = "adeel"; decisions = @(
  @{ company = "General Motors"; id = "fin:operating_margin"; action = "approve" },
  @{ company = "General Motors"; id = "story:abc123"; action = "edit"; quadrant = "threat"; note = "external risk" }
) } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/jobs/<id>/swot/review -ContentType "application/json" -Body $body
```

**5. Root-cause analysis on items you choose** (runs in the background, one stage `rca_1`, `rca_2`, ...)

```powershell
$body = @{ items = @("fin:operating_margin"); max_whys = 5; notes = @("GM statements include GM Financial") } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/jobs/<id>/rca -ContentType "application/json" -Body $body
curl.exe http://127.0.0.1:8000/jobs/<id>/rca             # results
```

**6. Get everything.** `GET /jobs/<id>/artifacts` lists every file; `GET /jobs/<id>/files/<path>` downloads one
(including the verification screenshots, which are the "receipts" for each news item).

## 4. Endpoints

| Method and path | Purpose |
|---|---|
| `GET /health` | Setup check |
| `POST /jobs` | Start a job |
| `GET /jobs`, `GET /jobs/{id}` | List jobs, job detail with stages and available actions |
| `POST /jobs/{id}/retry` | Re-run from the failed stage (earlier finished stages are kept) |
| `POST /jobs/{id}/cancel` | Stop the job (running processes are killed) |
| `DELETE /jobs/{id}` | Delete a finished or cancelled job and its folder |
| `GET /jobs/{id}/events`, `GET /jobs/{id}/logs/{stage}` | Timeline and raw logs (poll `events?after=<last id>`) |
| `GET /jobs/{id}/finance/report` | The gate's proposal |
| `POST /jobs/{id}/finance/approve` | Approve or reject, with row choices |
| `POST /jobs/{id}/finance/files` | Replace the CSVs and run the gate again |
| `GET /jobs/{id}/news` | Company sentiment and scored articles |
| `GET /jobs/{id}/swot/draft`, `POST .../swot/review`, `GET .../swot/final` | SWOT draft, review, final |
| `POST /jobs/{id}/rca`, `GET .../rca`, `GET .../rca/{name}` | Five Whys |
| `GET /jobs/{id}/artifacts`, `GET /jobs/{id}/files/{path}` | List and download files |

**Job status:** `running`, `awaiting_finance_approval`, `awaiting_swot_review`, `complete`, `failed`, `blocked`, `cancelled`.
**Stage status:** `pending`, `running`, `succeeded`, `failed`, `waiting` (for you), `blocked`, `rejected`, `skipped`, `cancelled`.
Each job's `actions` list tells a UI which buttons to show (`approve_finance`, `replace_finance_files`, `review_swot`, `run_rca`, `retry`, `cancel`).

## 5. What it does for you

- Gives every job its own folder, so runs never mix and nothing depends on "newest folder".
- Passes explicit `--input`, `--base` and `--outdir` to every stage and runs each from the right folder (`information_new/` or `planning/`).
- Never lets a script wait for keyboard input; a stage that would prompt gets an empty answer instead of hanging.
- Checks every stage's output (for example: discovery found articles, at least one page was verified) and stops with a readable error, plus the last lines of the log.
- Kills a stage that runs past its time limit (`FINANALYST_TIMEOUT_<STAGE>` seconds, defaults in `backend/config.py`).
- Survives a restart: interrupted stages are marked failed so you can retry them; queued work restarts.
- Refuses an approval that skips a row choice (the gate would otherwise silently take the first candidate).
- Refuses path tricks in file downloads and values that start with `-` where they could be read as options.

## 6. Settings

| Variable | Default | Meaning |
|---|---|---|
| `VULTR_INFERENCE_BASE`, `VULTR_INFERENCE_API_KEY`, `VULTR_CHAT_MODEL` | none | LLM settings used by every stage |
| `FINANALYST_JOBS_DIR` | `<root>/jobs` | Where jobs and the SQLite database live |
| `FINANALYST_MAX_WORKERS` | 6 | Background tasks at once |
| `FINANALYST_NEWS_CONCURRENCY` | 2 | News pipelines at once (each opens a browser and calls the model) |
| `FINANALYST_TIMEOUT_<STAGE>` | see config | Seconds before a stage is killed, e.g. `FINANALYST_TIMEOUT_VERIFY=3600` |
| `FINANALYST_CORS_ORIGINS` | localhost:3000, localhost:5173 | Comma-separated origins the UI may call from |
| `FINANALYST_SANDBOX` | `local` | `docker` runs the stages in `FINANALYST_SANDBOX_STAGES` inside containers |
| `FINANALYST_SANDBOX_STAGES` | `verify` | Comma-separated stage names for the sandbox |
| `HOST`, `PORT` | 127.0.0.1, 8000 | Where `python app.py` listens (use `HOST=0.0.0.0` on a server) |

## 7. Sandbox (optional, off by default)

The browser stage opens pages from the open internet, so it is the one worth isolating. With
`FINANALYST_SANDBOX=docker` that stage runs in a throwaway container that gets: no API keys, a read-only copy of the code,
write access to the one job folder only, and limits on memory, CPUs and processes (`FINANALYST_DOCKER_*`). Cancelling or timing out
kills the container.

```powershell
docker build -f Dockerfile.sandbox -t finanalyst-sandbox .
$env:FINANALYST_SANDBOX = "docker"
python app.py
```

`LocalRunner` and `DockerRunner` in `backend/runner.py` share one interface, so another runner (for example one that starts a throwaway
Vultr instance) is one new class plus one line in `get_runner`.

## 8. If something fails

- **`/health` shows a script as `false`:** the folder layout differs from `information_new/` + `planning/`. Set `FINANALYST_INFO_DIR` and `FINANALYST_PLAN_DIR`.
- **A stage is `failed`:** `GET /jobs/<id>` shows the error with the last log lines; the full log is `GET /jobs/<id>/logs/<stage>`. Fix the cause, then `POST /jobs/<id>/retry`.
- **`verify` fails to start:** run `python -m playwright install chromium` once in the same Python environment.
- **News items are missing from the SWOT:** the SWOT builder only accepts model-scored articles. Check that `VULTR_INFERENCE_API_KEY` was set when the job was created (a warning appears in the job's events).
- **Status stays `awaiting_finance_approval`:** that is correct; it is waiting for your approval.

## 9. What was tested

Tested by running jobs through the orchestration engine against stand-in scripts that copy your scripts' command-line flags and
output folders: both branches in parallel, a crash and retry, the approval rules, SWOT review rules, RCA, cancel (about 0.6 s), timeout,
blocked statements and file replacement, restart recovery, path-traversal blocking, no API keys in the sandbox environment.
The HTTP layer was exercised with a stand-in for FastAPI (validation, errors, routing logic).

**Not tested here, so check on first run:** your real scripts end to end, FastAPI's own request parsing under Python 3.14, the Docker runner,
and process killing on Windows. Start with `/health`, then one job with `no_llm=true` to confirm the plumbing before spending model calls.
