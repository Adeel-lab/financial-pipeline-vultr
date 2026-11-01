import os, json
from openai import OpenAI

MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
_client = OpenAI(
    base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
    api_key=os.getenv("LLM_API_KEY") or os.environ["GROQ_API_KEY"],
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