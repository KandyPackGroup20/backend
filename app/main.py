from contextlib import asynccontextmanager
from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api.v1 import auth, rail, roster, reports, notifications, orders, inventory
from app.core.notifications import ensure_notification_table
from app.core.migrations import run_migrations

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure all tables and columns exist upon startup (Render cloud DB / local)
    try:
        run_migrations()
    except Exception as e:
        print(f"[STARTUP DB MIGRATION WARNING] Failed to run migrations on startup: {e}")
    try:
        ensure_notification_table()
    except Exception as e:
        print(f"[STARTUP DB INIT WARNING] Failed to ensure tables on startup: {e}")
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)

from app.core.security import is_allowed_origin

# CORS setup for dual frontends (Customer & Admin)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.AUTH_ALLOWED_ORIGINS,
    allow_origin_regex=r"https://.*\.vercel\.app|http://localhost:\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def check_browser_origin(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and not is_allowed_origin(origin):
            return JSONResponse(status_code=403, content={"detail": "ORIGIN_NOT_ALLOWED"})
        if request.cookies.get("kandypack_session") and not origin and request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse(status_code=403, content={"detail": "ORIGIN_NOT_ALLOWED"})
    return await call_next(request)


# Include API v1 Routers
app.include_router(auth.router, prefix=settings.API_V1_STR)
app.include_router(orders.router, prefix=settings.API_V1_STR)
app.include_router(rail.router, prefix=settings.API_V1_STR)
app.include_router(roster.router, prefix=settings.API_V1_STR)
app.include_router(reports.router, prefix=settings.API_V1_STR)
app.include_router(notifications.router, prefix=settings.API_V1_STR)
app.include_router(inventory.router, prefix=settings.API_V1_STR)


@app.get("/")
def root():
    return {
        "message": "Welcome to Kandypack Supply Chain Logistics API Gateway",
        "docs_url": "/docs",
        "health_url": "/health",
        "version": settings.VERSION
    }

@app.get("/health")
def health_check():
    db_status = "disconnected"
    db_error = None
    try:
        from app.core.database import get_db
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 AS alive;")
                res = cursor.fetchone()
                if res and res.get("alive") == 1:
                    db_status = "connected"
    except Exception as e:
        db_error = str(e)
        
    from app.core.cache import get_cache_status
    cache_status = get_cache_status()
        
    return {
        "status": "healthy" if db_status == "connected" else "degraded",
        "database": db_status,
        "database_host": settings.MYSQL_HOST,
        "database_name": settings.MYSQL_DATABASE,
        "cache": cache_status,
        "error": db_error,
        "version": settings.VERSION
    }

@app.get("/migrate")
def trigger_migrate():
    from app.core.migrations import run_migrations
    from app.core.cache import reset_login_failures
    success = run_migrations()
    for email in [
        'store.colombo@kandypack.lk',
        'store.negombo@kandypack.lk',
        'store.galle@kandypack.lk',
        'store.matara@kandypack.lk',
        'store.jaffna@kandypack.lk',
        'store.trinco@kandypack.lk',
        'store.kandy@kandypack.lk'
    ]:
        reset_login_failures(email)
    return {"migrated": success}


