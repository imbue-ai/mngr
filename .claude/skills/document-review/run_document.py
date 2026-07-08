import sys, json, requests, anthropic

doc_id = sys.argv[1]

# 1. Fetch from your API
resp = requests.get(f"http://api.elsichecklist.org/documents/{doc_id}")
resp.raise_for_status()
doc_text = resp.json()["text"]

# 2. Call Claude — a genuinely separate, fresh-context API request
client = anthropic.Anthropic()
message = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1000,
    system=TRIAGE_PROMPT,
    messages=[{"role": "user", "content": f"Review this document:\n\n{doc_text}"}]
)
raw = message.content[0].text

# 3. Parse the JSON Claude generated
try:
    review = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
except json.JSONDecodeError:
    print(f"Failed to parse Claude's output: {raw[:200]}")
    sys.exit(1)

# 4. POST back to your server
post_resp = requests.post(
    f"http://api.elsichecklist.org/documents/{doc_id}/review",
    json=review
)
post_resp.raise_for_status()
print(f"Posted review for {doc_id}: {review['primary_topic']}")
