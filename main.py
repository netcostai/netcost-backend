import os
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from supabase import create_client
from openai import OpenAI
from anthropic import Anthropic
from google import genai
from google.genai import types as genai_types

from vault import encrypt_key, decrypt_key, generate_api_key, hash_api_key
from schemas import VaultVaultCreate, VaultCreateResponse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://your-portal-domain.com"],
    allow_methods=["POST"],
    allow_headers=["Authorization", "Content-Type"],
)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("FATAL: Missing SUPABASE_URL or SUPABASE_KEY. Refusing to start.")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Default model per provider, used unless the caller overrides it.
# Verify these are still current model IDs before relying on them long-term —
# providers deprecate/rename models over time.
DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
    "google": "gemini-2.5-flash",
}


class ChatRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    max_tokens: int = 50


def call_openai(api_key: str, model: str, prompt: str, max_tokens: int) -> str:
    client = OpenAI(api_key=api_key)
    completion = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    return completion.choices[0].message.content


def call_anthropic(api_key: str, model: str, prompt: str, max_tokens: int) -> str:
    client = Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def call_google(api_key: str, model: str, prompt: str, max_tokens: int) -> str:
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=genai_types.GenerateContentConfig(max_output_tokens=max_tokens),
    )
    return response.text


PROVIDER_HANDLERS = {
    "openai": call_openai,
    "anthropic": call_anthropic,
    "google": call_google,
}


@app.post("/v1/vault/create", response_model=VaultCreateResponse)
async def create_vault_entry(request: VaultVaultCreate):
    api_key = generate_api_key()
    api_key_hash = hash_api_key(api_key)
    encrypted_provider_key = encrypt_key(request.raw_provider_key)

    result = supabase.table("vault_creden