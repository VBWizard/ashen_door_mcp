from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import psycopg2
import os
import re
from typing import Optional, List
from datetime import datetime
import traceback

from dotenv import load_dotenv
load_dotenv()

app = FastAPI()

# Load database connection info from environment variables
DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
AUTH_TOKEN = os.getenv("AUTH_TOKEN")

security = HTTPBearer()

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
    if credentials.credentials != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.post("/query_chat_history", response_model=List[ChatEntry])
def query_chat_history(
    query: ChatHistoryQuery,
    credentials: HTTPAuthorizationCredentials = Depends(validate_token)
):
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
