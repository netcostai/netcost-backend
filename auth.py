import os
import jwt
from jwt import PyJWKClient
from fastapi import Header, HTTPException

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")  # kept as a fallback

if not SUPABASE_URL:
    raise RuntimeError("FATAL: Missing SUPABASE_URL. Refusing to start.")

JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
jwk_client = PyJWKClient(JWKS_URL)


def get_current_user_id(authorization: str = Header(...)) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    token = authorization.removeprefix("Bearer ").strip()

    try:
        signing_key = jwk_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",
        )
        return payload["sub"]
    except Exception:
        pass

    if SUPABASE_JWT_SECRET:
        try:
            payload = jwt.decode(
                token,
                SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience="authenticated",
            )
            return payload["sub"]
        except jwt.PyJWTError:
            pass

    raise HTTPException(status_code=401, detail="Invalid or expired session")
