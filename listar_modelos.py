import os
from pathlib import Path
from dotenv import load_dotenv
from google import genai

load_dotenv(Path(__file__).parent / "token.env")
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

for m in client.models.list():
    acoes = getattr(m, "supported_actions", None) or []
    if not acoes or "generateContent" in acoes:
        print(m.name)
        