from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api.v1 import auth, rail, roster, reports, notifications, orders

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

# CORS setup for dual frontends (Customer & Admin)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API v1 Routers
app.include_router(auth.router, prefix=settings.API_V1_STR)
app.include_router(orders.router, prefix=settings.API_V1_STR)
app.include_router(rail.router, prefix=settings.API_V1_STR)
app.include_router(roster.router, prefix=settings.API_V1_STR)
app.include_router(reports.router, prefix=settings.API_V1_STR)
app.include_router(notifications.router, prefix=settings.API_V1_STR)

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

