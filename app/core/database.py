import pymysql
import pymysql.cursors
from contextlib import contextmanager
from app.core.config import settings

def get_db_connection():
    """Creates a direct PyMySQL connection with dictionary cursor."""
    return pymysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=settings.MYSQL_DATABASE,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False
    )

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
