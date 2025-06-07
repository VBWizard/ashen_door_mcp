from fastapi import FastAPI, HTTPException, Depends
from fastapi import Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import psycopg2
import os
import re
from typing import Optional, List
from datetime import datetime
import traceback
import requests
from fastapi.responses import RedirectResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv()
GITHUB_CLIENT_ID = os.environ["GITHUB_CLIENT_ID"]
GITHUB_CLIENT_SECRET = os.environ["GITHUB_CLIENT_SECRET"]

REDIRECT_URI = "https://ashendoormcp-production.up.railway.app/auth/callback"
DEV_REDIRECT_URI = "http://localhost:8000/auth/callback"

app = FastAPI()

# Load database connection info from environment variables
DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASS = os.environ["DB_PASS"]
AUTH_TOKEN = os.environ["AUTH_TOKEN"]

security = HTTPBearer(auto_error=True)

@app.get("/login")
def login():
    github_auth_url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope=read:user"
    )
    return RedirectResponse(github_auth_url)

@app.get("/auth/callback")
def auth_callback(code: str):
    # Exchange the code for a token
    token_response = requests.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
    )

    if token_response.status_code != 200:
        return JSONResponse(status_code=token_response.status_code, content={"error": "Token exchange failed"})

    token_json = token_response.json()
    access_token = token_json.get("access_token")

    if not access_token:
        return JSONResponse(status_code=400, content={"error": "No access token received"})

    # OpenAI expects access_token + token_type in response
    return JSONResponse({
        "access_token": access_token,
        "token_type": "bearer"
    })

# Connect to the PostgreSQL database
def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS
    )

# Input schema for MCP search
class ChatHistoryQuery(BaseModel):
    search_term: str
    author_role: Optional[str] = None
    conversation_title: Optional[str] = None
    limit: Optional[int] = 10
    context_radius: Optional[int] = 2500

# Output schema
class ChatEntry(BaseModel):
    timestamp: datetime
    author: str
    title: Optional[str]
    content: str
    truncated: Optional[bool] = False

# Validate bearer token
def validate_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials

    # Allow legacy static token (fallback only)
    if token == AUTH_TOKEN:
        return

    # Check if GitHub-style token
    if token.startswith("gho_"):
        user_info = requests.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}"}
        )

        if user_info.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid GitHub token")

        user = user_info.json()
        github_login = user.get("login")

        # Optionally restrict access to yourself only
        if github_login != "VBWizard":
            raise HTTPException(status_code=403, detail="Unauthorized GitHub user")

        return

    raise HTTPException(status_code=401, detail="Unauthorized token format")

@app.post("/query_chat_history", response_model=List[ChatEntry])
def query_chat_history(
    query: ChatHistoryQuery,
    credentials: HTTPAuthorizationCredentials = Security(security)
):
    user = validate_token(credentials)  # Optionally return GitHub user info
    conn = get_db_connection()
    cur = conn.cursor()

    params = []
    conditions = ["m.content ILIKE %s", "m.author_role != 'tool'"]
    params.append(f"%{query.search_term}%")

    if query.author_role:
        conditions.append("m.author_role = %s")
        params.append(query.author_role)

    if query.conversation_title:
        conditions.append("c.title ILIKE %s")
        params.append(f"%{query.conversation_title}%")

    sql = f"""
        SELECT m.timestamp, m.author_role, c.title, m.content
        FROM messages m
        JOIN conversations c ON m.conversation_id = c.id
        WHERE {' AND '.join(conditions)}
        ORDER BY m.timestamp DESC
        LIMIT %s
    """

    params.append(query.limit)

    try:
        cur.execute(sql, params)
        rows = cur.fetchall()
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

    results = []
    for row in rows:
        content = row[3]
        if len(content) <= query.context_radius:
            results.append(ChatEntry(
                timestamp=row[0],
                author=row[1],
                title=row[2],
                content=content,
                truncated=False
            ))
        else:
            match = re.search(re.escape(query.search_term), content, re.IGNORECASE)
            if match:
                start = max(0, match.start() - query.context_radius // 2)
                end = min(len(content), match.end() + query.context_radius // 2)
                snippet = content[start:end]
                if start > 0:
                    snippet = "..." + snippet
                if end < len(content):
                    snippet = snippet + "..."
                results.append(ChatEntry(
                    timestamp=row[0],
                    author=row[1],
                    title=row[2],
                    content=snippet,
                    truncated=True
                ))
            else:
                snippet = content[:query.context_radius]
                if len(content) > query.context_radius:
                    snippet += "..."
                results.append(ChatEntry(
                    timestamp=row[0],
                    author=row[1],
                    title=row[2],
                    content=snippet,
                    truncated=True
                ))

    return results
