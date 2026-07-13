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

    result = supabase.table("vault_credentials").insert({
        "company_name": request.company_name,
        "provider": request.provider,
        "encrypted_provider_key": encrypted_provider_key,
        "api_key_hash": api_key_hash,
    }).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create vault entry")

    row = result.data[0]
    return VaultCreateResponse(
        id=row["id"],
        company_name=row["company_name"],
        provider=row["provider"],
        api_key=api_key,
        created_at=row["created_at"],
    )


@app.post("/v1/proxy/chat")
async def chat_proxy(request: ChatRequest, authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    api_key = authorization.removeprefix("Bearer ").strip()
    api_key_hash = hash_api_key(api_key)

    result = (
        supabase.table("vault_credentials")
        .select("id, provider, encrypted_provider_key")
        .eq("api_key_hash", api_key_hash)
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=401, detail="Invalid API key")

    record = result.data[0]
    provider = record["provider"]

    handler = PROVIDER_HANDLERS.get(provider)
    if not handler:
        raise HTTPException(status_code=500, detail=f"Unsupported provider: {provider}")

    try:
        decrypted_key = decrypt_key(record["encrypted_provider_key"])
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to process request")

    model = request.model or DEFAULT_MODELS[provider]

    try:
        text = handler(decrypted_key, model, request.prompt, request.max_tokens)
        return {"response": text}
    except Exception:
        raise HTTPException(status_code=500, detail="Provider request failed")