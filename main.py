import os
from typing import Callable, Iterator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from supabase import create_client, Client

from anthropic import Anthropic
from anthropic import APIError as AnthropicAPIError
from google import genai
from google.genai import types as genai_types
from google.genai.errors import APIError as GoogleAPIError
from openai import OpenAI, OpenAIError

from vault import decrypt_key, encrypt_key
from schemas import Provider, VaultVaultCreate, VaultVaultResponse

load_dotenv()

app = FastAPI(title="NetCost Wholesale Token Gateway")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Supabase
url: str = os.getenv("SUPABASE_URL")
key: str = os.getenv("SUPABASE_KEY")
if not url or not key:
    raise ValueError("Supabase configuration variables are missing.")
supabase: Client = create_client(url, key)

# Domain-Locked Isolation Configuration
BANNED_CONSUMER_DOMAINS = ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com"]

# Model string prefix -> provider routing table
MODEL_PREFIX_ROUTES: dict[str, Provider] = {
    "claude-": "anthropic",
    "gemini-": "google",
    "gpt-": "openai",
    "chatgpt-": "openai",
    "o1": "openai",
    "o3": "openai",
    "o4": "openai",
}


class ProxyRequest(BaseModel):
    model: str = "gpt-4o"
    prompt: str
    system: Optional[str] = None  # stable instructions kept ahead of `prompt` to form the cacheable prefix
    max_tokens: int = 1024
    user_id: str  # Enterprise routing expects a business email identifier


@app.get("/")
async def status():
    return {"status": "online", "gateway": "NetCost Token Proxy"}


def detect_provider(model: str) -> Provider:
    """Maps an incoming model string to its owning provider."""
    model_lower = model.lower()
    for prefix, provider in MODEL_PREFIX_ROUTES.items():
        if model_lower.startswith(prefix):
            return provider
    raise HTTPException(status_code=400, detail=f"Unrecognized model '{model}': no provider mapping found.")


def get_tenant_provider_key(company_name: str, provider: Provider) -> str:
    """Fetches the matching provider credential for a tenant from Supabase vault_credentials and decrypts it."""
    response = (
        supabase.table("vault_credentials")
        .select("encrypted_provider_key")
        .eq("company_name", company_name)
        .eq("provider", provider)
        .limit(1)
        .execute()
    )
    if not response.data:
        raise HTTPException(
            status_code=404,
            detail=f"No vaulted '{provider}' credential found for tenant '{company_name}'.",
        )
    return decrypt_key(response.data[0]["encrypted_provider_key"])


def stream_openai(api_key: str, model: str, prompt: str, system: Optional[str], max_tokens: int) -> Iterator[str]:
    """OpenAI caches prompts >=1024 tokens automatically; no request flag is needed,
    so the stable `system` block is simply placed ahead of the variable `prompt`."""
    client = OpenAI(api_key=api_key)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    except OpenAIError as e:
        yield f"[openai upstream error: {str(e)}]"


def stream_anthropic(api_key: str, model: str, prompt: str, system: Optional[str], max_tokens: int) -> Iterator[str]:
    """Anthropic caching is explicit: the stable `system` block carries an
    ephemeral cache_control breakpoint so repeat requests read it from cache."""
    client = Anthropic(api_key=api_key)
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    try:
        with client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                yield text
    except AnthropicAPIError as e:
        yield f"[anthropic upstream error: {str(e)}]"


def stream_google(api_key: str, model: str, prompt: str, system: Optional[str], max_tokens: int) -> Iterator[str]:
    """Gemini applies implicit prompt caching automatically on supported models;
    keeping `system_instruction` ahead of the per-request prompt lets Google
    detect and cache the shared prefix with no explicit cache object required."""
    client = genai.Client(api_key=api_key)
    config = genai_types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=max_tokens,
    )
    try:
        for chunk in client.models.generate_content_stream(model=model, contents=prompt, config=config):
            if chunk.text:
                yield chunk.text
    except GoogleAPIError as e:
        yield f"[google upstream error: {str(e)}]"


PROVIDER_STREAMERS: dict[Provider, Callable[[str, str, str, Optional[str], int], Iterator[str]]] = {
    "openai": stream_openai,
    "anthropic": stream_anthropic,
    "google": stream_google,
}


@app.post("/v1/proxy/chat")
async def proxy_chat(request: ProxyRequest):
    # 1. Enforce Domain-Locked Isolation Guardrail
    if "@" in request.user_id:
        user_domain = request.user_id.split("@")[-1].lower().strip()
        if user_domain in BANNED_CONSUMER_DOMAINS:
            raise HTTPException(
                status_code=403,
                detail=f"Access Denied: Domain '{user_domain}' is isolated from wholesale routing. Corporate credentials required."
            )

    # 2. Route to the correct provider based on the model string
    provider = detect_provider(request.model)

    # 3. Fetch and decrypt the wholesale tenant's provider key from the vault
    api_key = get_tenant_provider_key("Acme Corp", provider)

    # 4. Stream the live completion back to the caller using that provider's native caching strategy
    streamer = PROVIDER_STREAMERS[provider]
    return StreamingResponse(
        streamer(api_key, request.model, request.prompt, request.system, request.max_tokens),
        media_type="text/plain",
    )


@app.post("/v1/vault/store")
async def store_secure_credential(payload: VaultVaultCreate):
    try:
        scrambled_key = encrypt_key(payload.raw_provider_key)
        db_row = {
            "company_name": payload.company_name,
            "provider": payload.provider,
            "encrypted_provider_key": scrambled_key,
        }
        response = supabase.table("vault_credentials").insert(db_row).execute()
        if response.data:
            return response.data[0]
        else:
            raise HTTPException(status_code=500, detail="Database write confirmed but record payload empty.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database pipeline failure: {str(e)}")
