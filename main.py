import os
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client
from openai import OpenAI

from vault import encrypt_key, decrypt_key, generate_api_key, hash_api_key
from schemas import VaultVaultCreate, VaultCreateResponse

app = FastAPI()

# CORS — restrict this to your actual portal domain before going live
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


class ChatRequest(BaseModel):
    model: str
    prompt: str
    max_tokens: int = 50


@app.post("/v1/vault/create", response_model=VaultCreateResponse)
async def create_vault_entry(request: VaultVaultCreate):
    """Onboard a new company: encrypt + store their provider key,
    and issue a CostAI gateway API key (shown once, stored only as a hash)."""
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

    try:
        decrypted_key = decrypt_key(record["encrypted_provider_key"])
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to process request")

    try:
        client = OpenAI(api_key=decrypted_key)
        completion = client.chat.completions.create(
            model=request.model,
            messages=[{"role": "user", "content": request.prompt}],
        )
        return {"response": completion.choices[0].message.content}
    except Exception:
        raise HTTPException(status_code=500, detail="Provider request failed")