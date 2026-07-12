import os
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from supabase import create_client
from cryptography.fernet import Fernet
from openai import OpenAI

app = FastAPI()

# 1. Setup Security & Database
# Ensure ENCRYPTION_MASTER_KEY, SUPABASE_URL, and SUPABASE_KEY 
# are set in your Render "Environment" variables
MASTER_KEY = os.environ.get("ENCRYPTION_MASTER_KEY")
cipher_suite = Fernet(MASTER_KEY.encode())

supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

# 2. Models
class VaultEntry(BaseModel):
    company_id: str
    provider: str
    raw_provider_key: str

class ChatRequest(BaseModel):
    model: str
    prompt: str
    max_tokens: int = 50

# --- STORAGE ENDPOINT ---
# Customers use this to store their key securely
@app.post("/v1/vault/store")
async def store_key(entry: VaultEntry):
    encrypted_key = cipher_suite.encrypt(entry.raw_provider_key.encode())
    
    supabase.table("vault_credentials").upsert({
        "company_id": entry.company_id,
        "provider": entry.provider,
        "encrypted_provider_key": encrypted_key.decode()
    }).execute()
    
    return {"status": "success", "message": f"Key secured for {entry.company_id}"}

# --- PROXY ENDPOINT ---
# Customers use this to send prompts. 
# They MUST include their 'company_id' in the header.
@app.post("/v1/proxy/chat")
async def chat_proxy(request: ChatRequest, company_id: str = Header(...)):
    # 1. Fetch key for THIS specific company
    response = supabase.table("vault_credentials") \
        .select("encrypted_provider_key") \
        .eq("company_id", company_id) \
        .execute()
    
    if not response.data:
        raise HTTPException(status_code=404, detail="No API key found for this company.")

    # 2. Decrypt the key on the fly
    encrypted_key = response.data[0]['encrypted_provider_key'].encode()
    decrypted_key = cipher_suite.decrypt(encrypted_key).decode()

    # 3. Forward the request to AI using the customer's key
    client = OpenAI(api_key=decrypted_key)
    
    try:
        ai_response = client.chat.completions.create(
            model=request.model,
            messages=[{"role": "user", "content": request.prompt}],
            max_tokens=request.max_tokens
        )
        return {"response": ai_response.choices[0].message.content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI Provider Error: {str(e)}")