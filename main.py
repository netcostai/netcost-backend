import os
from fastapi import FastAPI, Header, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from supabase import create_client
from openai import OpenAI
from anthropic import Anthropic
from google import genai
from google.genai import types as genai_types
import stripe

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

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")  # added after Step 6

if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
    raise RuntimeError("FATAL: Missing Stripe configuration. Refusing to start.")

stripe.api_key = STRIPE_SECRET_KEY

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


def get_user_email(user_id: str) -> str:
    try:
        user = supabase.auth.admin.get_user_by_id(user_id)
        return user.user.email if user and user.user else "Unknown"
    except Exception:
        return "Unknown"


def get_company_for_user(user_id: str) -> dict:
    result = (
        supabase.table("company_users")
        .select("company_id, role, status, companies(name, invite_code, subscription_status)")
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
        "subscription_status": row["companies"]["subscription_status"],
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


def require_subscribed(company: dict = Depends(require_active)) -> dict:
    if company["subscription_status"] not in ("trialing", "active"):
        raise HTTPException(status_code=402, detail="Payment required to use the gateway")
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
        pass


def sync_subscription_quantity(company_id: str):
    result = supabase.table("companies").select("stripe_subscription_id").eq("id", company_id).execute()
    if not result.data or not result.data[0]["stripe_subscription_id"]:
        return

    subscription_id = result.data[0]["stripe_subscription_id"]

    members = (
        supabase.table("company_users")
        .select("user_id", count="exact")
        .eq("company_id", company_id)
        .eq("status", "active")
        .execute()
    )
    quantity = members.count or 1

    try:
        subscription = stripe.Subscription.retrieve(subscription_id)
        item_id = subscription["items"]["data"][0]["id"]
        stripe.Subscription.modify(
            subscription_id,
            items=[{"id": item_id, "quantity": quantity}],
            proration_behavior="create_prorations",
        )
    except Exception:
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
    pending = [{"user_id": row["user_id"], "email": get_user_email(row["user_id"])} for row in result.data]
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

    sync_subscription_quantity(company["company_id"])

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
async def chat_proxy(request: ChatRequest, company: dict = Depends(require_subscribed)):
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

    usage = [{"user_id": uid, "email": get_user_email(uid), **stats} for uid, stats in by_user.items()]
    usage.sort(key=lambda u: u["input_tokens"] + u["output_tokens"], reverse=True)

    return {"usage": usage}


@app.post("/v1/billing/create-checkout-session")
async def create_checkout_session(company: dict = Depends(require_admin)):
    members = (
        supabase.table("company_users")
        .select("user_id", count="exact")
        .eq("company_id", company["company_id"])
        .eq("status", "active")
        .execute()
    )
    quantity = members.count or 1

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": quantity}],
            subscription_data={
                "trial_period_days": 14,
                "metadata": {"company_id": company["company_id"]},
            },
            client_reference_id=company["company_id"],
            customer_email=get_user_email(company["user_id"]),
            success_url="https://netcost.ai/gateway?billing=success",
            cancel_url="https://netcost.ai/billing/setup?billing=cancelled",
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to start checkout")

    return {"url": session.url}


@app.post("/v1/billing/portal")
async def billing_portal(company: dict = Depends(require_admin)):
    result = supabase.table("companies").select("stripe_customer_id").eq("id", company["company_id"]).execute()
    if not result.data or not result.data[0]["stripe_customer_id"]:
        raise HTTPException(status_code=400, detail="No billing account found")

    try:
        session = stripe.billing_portal.Session.create(
            customer=result.data[0]["stripe_customer_id"],
            return_url="https://netcost.ai/gateway",
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to open billing portal")

    return {"url": session.url}


@app.post("/v1/billing/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None, alias="stripe-signature")):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Webhook not configured yet")

    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        company_id = data.get("client_reference_id")
        if company_id:
            supabase.table("companies").update({
                "stripe_customer_id": data.get("customer"),
                "stripe_subscription_id": data.get("subscription"),
                "subscription_status": "trialing",
            }).eq("id", company_id).execute()

    elif event_type in ("customer.subscription.updated", "customer.subscription.created"):
        subscription_id = data.get("id")
        status = data.get("status")
        supabase.table("companies").update({"subscription_status": status}).eq(
            "stripe_subscription_id", subscription_id
        ).execute()

    elif event_type == "customer.subscription.deleted":
        subscription_id = data.get("id")
        supabase.table("companies").update({"subscription_status": "canceled"}).eq(
            "stripe_subscription_id", subscription_id
        ).execute()

    return {"received": True}