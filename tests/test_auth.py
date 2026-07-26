import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from auth import AuthError, validate_telegram_init_data


def signed_init_data(bot_token: str, user: dict, auth_date: int | None = None) -> str:
    values = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class TelegramAuthTests(unittest.TestCase):
    def test_accepts_valid_signature(self):
        token = "123456:telegram-test-token"
        data = signed_init_data(token, {"id": 42, "first_name": "Лев"})

        user = validate_telegram_init_data(data, token)

        self.assertEqual(user["id"], 42)

    def test_rejects_tampered_user(self):
        token = "123456:telegram-test-token"
        data = signed_init_data(token, {"id": 42}).replace("%3A42", "%3A43")

        with self.assertRaises(AuthError):
            validate_telegram_init_data(data, token)

    def test_rejects_expired_data(self):
        token = "123456:telegram-test-token"
        data = signed_init_data(token, {"id": 42}, auth_date=int(time.time()) - 90000)

        with self.assertRaises(AuthError):
            validate_telegram_init_data(data, token)


if __name__ == "__main__":
    unittest.main()
