"""
End-to-end auth tests: registration, email verification, login, password reset,
admin 2FA and the staff-2FA gate.

Run from the repo root:
    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "py_server"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update({
    "DB_PATH": os.path.join(TMP, "auth.db"),
    "DEMO_MODE": "1",
    "MASTER_KEY": "ab" * 32,
    "ADMIN_EMAIL": "admin@nectra.local",
    "ADMIN_PASSWORD": "Adm1n!Test2026",
    "PRICE_FEED": "sim",
    "HOST": "127.0.0.1",
})

import pyotp
from fastapi.testclient import TestClient

import database
import app as app_module

PASSWORD = "Str0ngPass1!"


class AuthFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.init_db()
        cls.ctx = TestClient(app_module.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def csrf(self):
        self.client.get("/api/public/config")
        return self.client.cookies.get("nx_csrf")

    def post(self, path, body=None):
        return self.client.post(path, json=body or {}, headers={"X-CSRF": self.csrf()})

    # ---- registration + verification -------------------------------------
    def test_register_requires_verified_email(self):
        database.update_setting("invite_only", False)
        database.update_setting("registration_open", True)
        r = self.post("/api/auth/register", {
            "email": "a@example.com", "password": PASSWORD,
            "code": "", "acceptTerms": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["needsVerification"])
        code = r.json().get("devCode")
        self.assertTrue(code)

        self.assertEqual(self.client.get("/api/dashboard").status_code, 403)
        bad = self.post("/api/auth/verify-email", {"code": "000000"})
        self.assertEqual(bad.status_code, 400)
        ok = self.post("/api/auth/verify-email", {"code": code})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(self.client.get("/api/dashboard").status_code, 200)

    def test_honeypot_and_terms(self):
        database.update_setting("registration_open", True)
        r = self.post("/api/auth/register", {
            "email": "hp@example.com", "password": PASSWORD,
            "acceptTerms": True, "hp_check": "bot"})
        self.assertEqual(r.status_code, 400)
        r = self.post("/api/auth/register", {
            "email": "terms@example.com", "password": PASSWORD, "acceptTerms": False})
        self.assertEqual(r.status_code, 400)

    # ---- password reset --------------------------------------------------
    def test_password_reset(self):
        self.post("/api/auth/register", {
            "email": "reset@example.com", "password": PASSWORD, "acceptTerms": True})
        code = self.post("/api/auth/forgot", {"email": "reset@example.com"}).json()["devCode"]
        r = self.post("/api/auth/reset", {
            "email": "reset@example.com", "code": code, "password": "NewPassw0rd!!"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.post("/api/auth/login", {
            "email": "reset@example.com", "password": PASSWORD}).status_code, 401)
        self.assertEqual(self.post("/api/auth/login", {
            "email": "reset@example.com", "password": "NewPassw0rd!!"}).status_code, 200)

    # ---- admin login + 2FA ----------------------------------------------
    def test_admin_login_and_2fa(self):
        r = self.post("/api/auth/login", {
            "email": "admin@nectra.local", "password": "Adm1n!Test2026"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["twofaRequired"])

        setup = self.post("/api/auth/2fa/setup").json()
        self.assertIn("otpauth", setup)
        code = pyotp.TOTP(setup["secret"]).now()
        self.assertEqual(self.post("/api/auth/2fa/enable", {"code": code}).status_code, 200)

        login = self.post("/api/auth/login", {
            "email": "admin@nectra.local", "password": "Adm1n!Test2026"})
        self.assertTrue(login.json()["twofaRequired"])
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 403)

        code = pyotp.TOTP(setup["secret"]).now()
        verified = self.post("/api/auth/2fa/verify", {"code": code})
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 200)

        # Staff gate: with the requirement on and 2FA off, admin routes close.
        database.update_setting("admin_2fa_required", True)
        latest = pyotp.TOTP(setup["secret"]).now()
        self.assertEqual(self.post("/api/auth/2fa/disable", {"code": latest}).status_code, 200)
        r = self.client.get("/api/admin/overview")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json().get("code"), "ADMIN_2FA_REQUIRED")

    # ---- invites ---------------------------------------------------------
    def test_invite_only(self):
        database.update_setting("invite_only", True)
        self.assertEqual(self.post("/api/auth/invite/validate",
                                   {"code": "NOPE1234"}).status_code, 400)
        database.q_run("INSERT INTO invites (code, max_uses, uses, active, created_at)"
                       " VALUES ('GOOD1234', 1, 0, 1, ?)", database.now())
        self.assertEqual(self.post("/api/auth/invite/validate",
                                   {"code": "GOOD1234"}).status_code, 200)
        r = self.post("/api/auth/register", {
            "email": "invited@example.com", "password": PASSWORD,
            "code": "GOOD1234", "acceptTerms": True})
        self.assertEqual(r.status_code, 200, r.text)
        # single-use code is now exhausted
        self.assertEqual(self.post("/api/auth/invite/validate",
                                   {"code": "GOOD1234"}).status_code, 400)
        database.update_setting("invite_only", False)


if __name__ == "__main__":
    unittest.main()
