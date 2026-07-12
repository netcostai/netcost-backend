import os
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from supabase import create_client
from cryptography.fernet import Fernet
from openai import OpenAI

app = FastAPI()

# Configuration
MASTER_KEY = os.environ.get("ENCRYPTION_MASTER_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Safety Check: Did we get our keys?
if not MASTER_KEY or not SUPABASE_URL or not SUPABASE_KEY:
    print("CRITICAL: Missing Environment Variables!")

cipher_suite = Fernet(MASTER_KEY.encode())
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

class ChatRequest(BaseModel):
    model: str
    prompt: str
    max_tokens: int = 50

@app.post("/v1/proxy/chat")
async def chat_proxy(request: ChatRequest, company_id: str = Header(..., alias="company-id")):
    print(f"DEBUG: Processing request for company: {company_id}")
    
    try:
        # 1. Fetch
        response = supabase.table("vault_credentials").select("encrypted_provider_key").eq("company_id", company_id).execute()
        
        if not response.data:
            print("DEBUG: No key found in database")
            raise HTTPException(status_code=404, detail="No key found")
            
        encrypted_val = response.data[0]['encrypted_provider_key']
        print("DEBUG: Database fetch successful")
        
        # 2. Decrypt
        decrypted_key = cipher_suite.decrypt(encrypted_val.encode()).decode()
        print("DEBUG: Decryption successful")

        # 3. Call OpenAI
        client = OpenAI(api_key=decrypted_key)
        response = client.chat.completions.create(
            model=request.model,
            messages=[{"role": "user", "content": request.prompt}]
        )
        return {"response": response.choices[0].message.content}

    except Exception as e:
        print(f"DEBUG: Error happened: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))