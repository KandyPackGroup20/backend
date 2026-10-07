import json
import logging
import socket
import time
from typing import Any, Optional, Tuple
from app.core.config import settings

logger = logging.getLogger(__name__)

# local fallback caches if redis is unavailable
_memory_cache: dict[str, dict[str, Any]] = {}
_memory_rate_limit: dict[str, dict[str, Any]] = {}

_redis_client = None
_last_check_time = 0.0
_redis_operational = False
RETRY_INTERVAL_SECONDS = 30.0


def _is_port_available(host: str, port: int, timeout: float = 0.3) -> bool:
    target_host = "127.0.0.1" if host in ("localhost", "127.0.0.1") else host
    try:
        sock = socket.create_connection((target_host, port), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


def get_redis_client():
    global _redis_client, _last_check_time, _redis_operational
    if not settings.REDIS_ENABLED:
        return None

    if _redis_operational and _redis_client is not None:
        return _redis_client

    now = time.time()
    if not _redis_operational and (now - _last_check_time) < RETRY_INTERVAL_SECONDS:
        return None

    _last_check_time = now

    try:
        import redis
        if settings.REDIS_URL:
            client = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
            )
            if client.ping():
                _redis_client = client
                _redis_operational = True
                return _redis_client

        if not _is_port_available(settings.REDIS_HOST, settings.REDIS_PORT):
            _redis_operational = False
            _redis_client = None
            return None

        client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            password=settings.REDIS_PASSWORD or None,
            ssl=settings.REDIS_SSL,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        if client.ping():
            _redis_client = client
            _redis_operational = True
            return _redis_client
    except Exception as e:
        logger.warning(f"redis connection error: {e}")

    _redis_client = None
    _redis_operational = False
    return None


def is_redis_active() -> bool:
    client = get_redis_client()
    if client is None:
        return False
    try:
        return bool(client.ping())
    except Exception:
        return False


# cache helpers

def get_cache(key: str) -> Optional[Any]:
    client = get_redis_client()
    if client:
        try:
            val = client.get(key)
            if val is not None:
                return json.loads(val)
        except Exception:
            pass

    item = _memory_cache.get(key)
    if item:
        if time.time() < item["expires_at"]:
            return item["value"]
        else:
            _memory_cache.pop(key, None)
    return None


def set_cache(key: str, value: Any, ttl_seconds: int = 60) -> bool:
    client = get_redis_client()
    if client:
        try:
            serialized = json.dumps(value)
            client.setex(key, ttl_seconds, serialized)
            return True
        except Exception:
            pass

    _memory_cache[key] = {
        "value": value,
        "expires_at": time.time() + ttl_seconds
    }
    return True


def invalidate_cache(key_prefix: str) -> int:
    deleted_count = 0
    client = get_redis_client()
    if client:
        try:
            keys = client.keys(f"{key_prefix}*")
            if keys:
                deleted_count = client.delete(*keys)
        except Exception:
            pass

    to_delete = [k for k in _memory_cache if k.startswith(key_prefix)]
    for k in to_delete:
        _memory_cache.pop(k, None)
        deleted_count += 1

    return deleted_count


# rate limiting

def check_rate_limit(identifier: str, max_attempts: int = 5, window_seconds: int = 60) -> Tuple[bool, int]:
    key = f"ratelimit:login:{identifier}"
    client = get_redis_client()

    if client:
        try:
            attempts_raw = client.get(key)
            attempts = int(attempts_raw) if attempts_raw else 0
            if attempts >= max_attempts:
                return False, 0
            return True, max_attempts - attempts
        except Exception:
            pass

    now = time.time()
    record = _memory_rate_limit.get(key)
    if record:
        if now < record["expires_at"]:
            attempts = record["attempts"]
            if attempts >= max_attempts:
                return False, 0
            return True, max_attempts - attempts
        else:
            _memory_rate_limit.pop(key, None)

    return True, max_attempts


def record_login_failure(identifier: str, window_seconds: int = 60) -> int:
    key = f"ratelimit:login:{identifier}"
    client = get_redis_client()

    if client:
        try:
            pipe = client.pipeline()
            pipe.incr(key)
            pipe.expire(key, window_seconds)
            results = pipe.execute()
            return int(results[0])
        except Exception:
            pass

    now = time.time()
    record = _memory_rate_limit.get(key)
    if record and now < record["expires_at"]:
        record["attempts"] += 1
        return record["attempts"]
    else:
        _memory_rate_limit[key] = {
            "attempts": 1,
            "expires_at": now + window_seconds
        }
        return 1


def reset_login_failures(identifier: str) -> None:
    key = f"ratelimit:login:{identifier}"
    client = get_redis_client()
    if client:
        try:
            client.delete(key)
        except Exception:
            pass
    _memory_rate_limit.pop(key, None)


def get_cache_status() -> dict:
    active = is_redis_active()
    return {
        "engine": "redis" if active else "in_memory_resilient",
        "redis_connected": active,
        "host": "cloud_redis" if (active and settings.REDIS_URL) else (settings.REDIS_HOST if active else "local_memory"),
        "port": settings.REDIS_PORT if (active and not settings.REDIS_URL) else None,
    }
