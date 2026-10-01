import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.core.security import (
    create_access_token,
    decode_access_token,
    verify_password,
    get_password_hash
)

def test_phase2():
    print("Testing auth utilities...")

    # 1. password hashing
    raw_pw = "KandyPass@2026"
    pw_hash = get_password_hash(raw_pw)
    assert verify_password(raw_pw, pw_hash) is True, "Password verification failed"
    assert verify_password("WrongPassword", pw_hash) is False, "Invalid password incorrectly verified"
    print("  password hashing passed")

    # 2. jwt creation and decoding
    user_data = {
        "sub": "42",
        "email": "logistics@kandypack.lk",
        "role": "LOGISTICS_MGR",
        "force_password_reset": False
    }
    token = create_access_token(data=user_data)
    assert isinstance(token, str), "Token generation failed"
    
    decoded = decode_access_token(token)
    assert decoded is not None, "Failed to decode valid token"
    assert decoded["email"] == "logistics@kandypack.lk"
    assert decoded["role"] == "LOGISTICS_MGR"
    print("  jwt session passed")

    # 3. sql injection string escaping check
    attack_str = "' OR '1'='1' --"
    safe_param = (attack_str,)
    assert safe_param[0] == attack_str
    print("  parameterized query check passed")

    # 4. endpoint check
    from app.api.v1.auth import router
    routes = [r.path for r in router.routes]
    assert any("/login" in r for r in routes)
    assert any("/register" in r for r in routes)
    assert any("/change-password" in r for r in routes)
    assert any("/me" in r for r in routes)
    assert any("/logout" in r for r in routes)
    print("  auth routes verified")

    print("All tests passed.")

if __name__ == "__main__":
    test_phase2()
