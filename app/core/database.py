import pymysql
import pymysql.cursors
import ssl
from contextlib import contextmanager
from app.core.config import settings

def get_db_connection():
    """Creates a PyMySQL connection supporting both local and SSL cloud MySQL (Aiven/Render)."""
    kwargs = {
        "host": settings.MYSQL_HOST,
        "port": settings.MYSQL_PORT,
        "user": settings.MYSQL_USER,
        "password": settings.MYSQL_PASSWORD,
        "database": settings.MYSQL_DATABASE,
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": False
    }
    
    # If connecting to cloud host (e.g. Aiven), enable SSL
    if "aivencloud.com" in settings.MYSQL_HOST or "render.com" in settings.MYSQL_HOST:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl"] = ssl_ctx

    return pymysql.connect(**kwargs)

@contextmanager
def get_db():
    """Context manager for DB transactions."""
    conn = get_db_connection()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
