import os
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from supabase import create_client
from openai import OpenAI
from anthropic import Anthropic
from google import genai
from google.genai import types as genai_types

from vault import encrypt_key, decrypt_key
from schemas import VaultEntryCreate, VaultEntryResponse, Provider
from auth import get_current_user_id

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://netcost.ai", "https://www.netcost.ai"],
    allow_methods=["GET", "POST"],
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
    provider: Provider
    prompt: str
    model: Optional[str] = None
    max_tokens: int = 50


class CompleteSignupRequest(BaseModel):
    company_name: str


def get_company_for_user(user_id: str) -> dict:
    result = (
        supabase.table("company_users")
        .select("company_id, companies(name)")
        .eq("user_id", user_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="No company linked to this account")
    row = result.data[0]
    return {"company_id": row["company_id"], "company_name": row["companies"]["name"]}


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


@app.post("/v1/auth/complete-signup")
async def complete_signup(request: CompleteSignupRequest, user_id: str = Depends(get_current_user_id)):
    existing = supabase.table("company_users").select("company_id").eq("user_id", user_id).execute()
    if existing.data:
        return {"company_id": existing.data[0]["company_id"]}

    company_result = supabase.table("companies").insert({"name": request.company_name}).execute()
    if not company_result.data:
        raise HTTPException(status_code=500, detail="Failed to create company")

    company_id = company_result.data[0]["id"]
    supabase.table("company_users").insert({"user_id": user_id, "company_id": company_id}).execute()

    return {"company_id": company_id}


@app.get("/v1/me")
async def get_me(user_id: str = Depends(get_current_user_id)):
    company = get_company_for_user(user_id)
    return company


@app.get("/v1/vault/status")
async def vault_status(user_id: str = Depends(get_current_user_id)):
    company = get_company_for_user(user_id)
    result = (
        supabase.table("vault_credentials")
        .select("provider")
        .eq("company_id", company["company_id"])
        .execute()
    )
    connected = [row["provider"] for row in result.data]
    return {"connected_providers": connected}


@app.post("/v1/vault/create", response_model=VaultEntryResponse)
async def create_vault_entry(request: VaultEntryCreate, user_id: str = Depends(get_current_user_id)):
    company = get_company_for_user(user_id)
    encrypted_provider_key = encrypt_key(request.raw_provider_key)

    existing = (
        supabase.table("vault_credentials")
        .select("id")
        .eq("company_id", company["company_id"])
        .eq("provider", request.provider)
        .execute()
    )

    payload = {
        "company_id": company["company_id"],
        "company_name": company["company_name"],
        "provider": request.provider,
        "encrypted_provider_key": encrypted_provider_key,
    }

    if existing.data:
        result = (
            supabase.table("vault_credentials")
            .update(payload)
            .eq("id", existing.data[0]["id"])
            .execute()
        )
    else:
        result = supabase.table("vault_credentials").insert(payload).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to save key")

    row = result.data[0]
    return VaultEntryResponse(id=row["id"], provider=row["provider"], created_at=row["created_at"])


@app.post("/v1/proxy/chat")
async def chat_proxy(request: ChatRequest, user_id: str = Depends(get_current_user_id)):
    company = get_company_for_user(user_id)

    result = (
        supabase.table("vault_credentials")
        .select("encrypted_provider_key")
        .eq("company_id", company["company_id"])
        .eq("provider", request.provider)
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail=f"No {request.provider} key connected")

    handler = PROVIDER_HANDLERS.get(request.provider)
    if not handler:
        raise HTTPException(status_code=500, detail=f"Unsupported provider: {request.provider}")

    try:
        decrypted_key = decrypt_key(result.data[0]["encrypted_provider_key"])
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to process request")

    model = request.model or DEFAULT_MODELS[request.provider]

    try:
        text = handler(decrypted_key, model, request.prompt, request.max_tokens)
        return {"response": text}
    except Exception:
        raise HTTPException(status_code=500, detail="Provider request failed")