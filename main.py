import os
from fastapi import FastAPI, Header, HTTPException, Depends
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
    company_name: Optional[str] = None
    invite_code: Optional[str] = None


class TeamActionRequest(BaseModel):
    user_id: str


def get_company_for_user(user_id: str) -> dict:
    result = (
        supabase.table("company_users")
        .select("company_id, role, status, companies(name, invite_code)")
        .eq("user_id", user_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="No company linked to this account")
    row = result.data[0]
    return {
        "company_id": row["company_id"],
        "company_name": row["companies"]["name"],
        "invite_code": row["companies"]["invite_code"],
        "role": row["role"],
        "status": row["status"],
        "user_id": user_id,
    }


def require_active(user_id: str = Depends(get_current_user_id)) -> dict:
    company = get_company_for_user(user_id)
    if company["status"] != "active":
        raise HTTPException(status_code=403, detail="Your account is pending admin approval")
    return company


def require_admin(company: dict = Depends(require_active)) -> dict:
    if company["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only company admins can do this")
    return company


def call_openai(api_key: str, model: str, prompt: str, max_tokens: int):
    client = OpenAI(api_key=api_key)
    completion = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    text = completion.choices[0].message.content
    usage = completion.usage
    input_tokens = usage.prompt_tokens if usage else None
    output_tokens = usage.completion_tokens if usage else None
    return text, input_tokens, output_tokens


def call_anthropic(api_key: str, model: str, prompt: str, max_tokens: int):
    client = Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text
    input_tokens = message.usage.input_tokens if message.usage else None
    output_tokens = message.usage.output_tokens if message.usage else None
    return text, input_tokens, output_tokens


def call_google(api_key: str, model: str, prompt: str, max_tokens: int):
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=genai_types.GenerateContentConfig(max_output_tokens=max_tokens),
    )
    text = response.text
    usage = response.usage_metadata
    input_tokens = usage.prompt_token_count if usage else None
    output_tokens = usage.candidates_token_count if usage else None
    return text, input_tokens, output_tokens


PROVIDER_HANDLERS = {
    "openai": call_openai,
    "anthropic": call_anthropic,
    "google": call_google,
}


def log_usage(company_id: str, user_id: str, provider: str, model: str, input_tokens, output_tokens):
    try:
        supabase.table("usage_logs").insert({
            "company_id": company_id,
            "user_id": user_id,
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }).execute()
    except Exception:
        # Usage logging is best-effort — a logging failure should never
        # break the actual chat response the user is waiting on.
        pass


@app.post("/v1/auth/complete-signup")
async def complete_signup(request: CompleteSignupRequest, user_id: str = Depends(get_current_user_id)):
    existing = supabase.table("company_users").select("company_id").eq("user_id", user_id).execute()
    if existing.data:
        return {"company_id": existing.data[0]["company_id"]}

    if request.invite_code:
        company_result = supabase.table("companies").select("id").eq("invite_code", request.invite_code).execute()
        if not company_result.data:
            raise HTTPException(status_code=404, detail="Invalid invite code")
        company_id = company_result.data[0]["id"]
        supabase.table("company_users").insert(
            {"user_id": user_id, "company_id": company_id, "role": "member", "status": "pending"}
        ).execute()
        return {"company_id": company_id, "status": "pending"}

    if not request.company_name:
        raise HTTPException(status_code=400, detail="Company name is required to create a new company")

    company_result = supabase.table("companies").insert({"name": request.company_name}).execute()
    if not company_result.data:
        raise HTTPException(status_code=500, detail="Failed to create company")

    company_id = company_result.data[0]["id"]
    supabase.table("company_users").insert(
        {"user_id": user_id, "company_id": company_id, "role": "admin", "status": "active"}
    ).execute()

    return {"company_id": company_id, "status": "active"}


@app.get("/v1/me")
async def get_me(user_id: str = Depends(get_current_user_id)):
    return get_company_for_user(user_id)


@app.get("/v1/team/pending")
async def list_pending(company: dict = Depends(require_admin)):
    result = (
        supabase.table("company_users")
        .select("user_id")
        .eq("company_id", company["company_id"])
        .eq("status", "pending")
        .execute()
    )

    pending = []
    for row in result.data:
        try:
            user = supabase.auth.admin.get_user_by_id(row["user_id"])
            email = user.user.email if user and user.user else "Unknown"
        except Exception:
            email = "Unknown"
        pending.append({"user_id": row["user_id"], "email": email})

    return {"pending": pending}


@app.post("/v1/team/approve")
async def approve_member(request: TeamActionRequest, company: dict = Depends(require_admin)):
    result = (
        supabase.table("company_users")
        .update({"status": "active"})
        .eq("user_id", request.user_id)
        .eq("company_id", company["company_id"])
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="No pending request found for this user")
    return {"status": "approved"}


@app.post("/v1/team/deny")
async def deny_member(request: TeamActionRequest, company: dict = Depends(require_admin)):
    result = (
        supabase.table("company_users")
        .delete()
        .eq("user_id", request.user_id)
        .eq("company_id", company["company_id"])
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="No pending request found for this user")
    return {"status": "denied"}


@app.get("/v1/vault/status")
async def vault_status(company: dict = Depends(require_active)):
    result = (
        supabase.table("vault_credentials")
        .select("provider")
        .eq("company_id", company["company_id"])
        .execute()
    )
    connected = [row["provider"] for row in result.data]
    return {"connected_providers": connected}


@app.post("/v1/vault/create", response_model=VaultEntryResponse)
async def create_vault_entry(request: VaultEntryCreate, company: dict = Depends(require_admin)):
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
async def chat_proxy(request: ChatRequest, company: dict = Depends(require_active)):
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
        text, input_tokens, output_tokens = handler(decrypted_key, model, request.prompt, request.max_tokens)
    except Exception:
        raise HTTPException(status_code=500, detail="Provider request failed")

    log_usage(company["company_id"], company["user_id"], request.provider, model, input_tokens, output_tokens)

    return {"response": text}


@app.get("/v1/usage/summary")
async def usage_summary(company: dict = Depends(require_admin)):
    result = (
        supabase.table("usage_logs")
        .select("user_id, provider, input_tokens, output_tokens")
        .eq("company_id", company["company_id"])
        .execute()
    )

    by_user: dict = {}
    for row in result.data:
        uid = row["user_id"]
        if uid not in by_user:
            by_user[uid] = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
        by_user[uid]["requests"] += 1
        by_user[uid]["input_tokens"] += row["input_tokens"] or 0
        by_user[uid]["output_tokens"] += row["output_tokens"] or 0

    usage = []
    for uid, stats in by_user.items():
        try:
            user = supabase.auth.admin.get_user_by_id(uid)
            email = user.user.email if user and user.user else "Unknown"
        except Exception:
            email = "Unknown"
        usage.append({"user_id": uid, "email": email, **stats})

    usage.sort(key=lambda u: u["input_tokens"] + u["output_tokens"], reverse=True)

    return {"usage": usage}