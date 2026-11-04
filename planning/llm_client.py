import os
import json
from openai import OpenAI

BASE_URL = "https://api.vultrinference.com/v1"
MODEL = "deepseek-v4.1-flash-1017"

API_KEY = os.environ.get("VULTR_INFERENCE_API_KEY", "").strip()

if not API_KEY:
    raise RuntimeError(
        "VULTR_INFERENCE_API_KEY is missing in the RCA process. "
        "Start the backend from the terminal where the key is configured."
    )

_client = OpenAI(
    base_url=BASE_URL,
    api_key=API_KEY,
)

print(
    f"[llm] provider=Vultr endpoint={BASE_URL} model={MODEL}",
    flush=True,
)
def llm_json(prompt, system="You return only valid JSON.", retries=2):
    last = None
    for _ in range(retries + 1):
        r = _client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        print(f"[llm] answered by: {r.model}")
        last = r.choices[0].message.content
        try:
            return json.loads(last)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"LLM did not return valid JSON: {last[:200]}")