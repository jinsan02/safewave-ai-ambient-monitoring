"""앱 연동 HTTP 오류 계약. 무거운 서비스 의존성 없이 핸들러만 검사한다."""

import ast
import json
import logging
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError


ROOT = Path(__file__).resolve().parents[1]


def _load_nodes(path: Path, names: set[str], namespace: dict) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
             and node.name in names]
    assert {node.name for node in nodes} == names
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class AppHttpErrorTests(unittest.TestCase):
    def test_fcm_errors_have_distinct_status_without_leaking_details(self):
        class UnregisteredError(Exception):
            pass

        class SenderIdMismatchError(Exception):
            pass

        class FirebaseError(Exception):
            pass

        class InvalidArgumentError(FirebaseError):
            pass

        ns = _load_nodes(
            ROOT / "api" / "notifier.py", {"FcmUnavailableError", "_fcm_error_response"},
            {"JSONResponse": JSONResponse,
             "messaging": SimpleNamespace(UnregisteredError=UnregisteredError,
                                          SenderIdMismatchError=SenderIdMismatchError),
             "firebase_exceptions": SimpleNamespace(InvalidArgumentError=InvalidArgumentError,
                                                    FirebaseError=FirebaseError)},
        )
        cases = (
            (UnregisteredError("private token"), 410, "fcm_token_unregistered"),
            (SenderIdMismatchError("private project"), 409, "fcm_project_mismatch"),
            (InvalidArgumentError("private payload"), 422, "invalid_fcm_request"),
            (ValueError("private malformed token"), 422, "invalid_fcm_request"),
            (ns["FcmUnavailableError"]("private key path"), 503, "fcm_unavailable"),
            (FirebaseError("private upstream detail"), 502, "fcm_send_failed"),
            (RuntimeError("private internal detail"), 500, "internal_error"),
        )
        for exc, status, code in cases:
            with self.subTest(code=code):
                response = ns["_fcm_error_response"](exc, sent=False)
                self.assertEqual(response.status_code, status)
                self.assertEqual(json.loads(response.body),
                                 {"ok": False, "error": code, "sent": False})
                self.assertNotIn("private", response.body.decode())

        ns["TestNotifyRequest"] = object
        _load_nodes(ROOT / "api" / "notifier.py", {"test_notification"}, ns)
        ns["_send_fcm"] = lambda *args, **kw: (_ for _ in ()).throw(
            ns["FcmUnavailableError"]("private key path"))
        with self.assertRaises(StopIteration) as done:
            ns["test_notification"](SimpleNamespace(token="token")).send(None)
        self.assertEqual(done.exception.value.status_code, 503)

    def test_redis_failure_is_503_without_internal_error_text(self):
        class RedisError(Exception):
            pass

        logs = []
        ns = _load_nodes(
            ROOT / "api" / "main.py", {"_redis_unavailable_response"},
            {"Request": object, "RedisError": RedisError, "JSONResponse": JSONResponse,
             "logging": logging, "_log": lambda *args, **kw: logs.append((args, kw))},
        )
        request = SimpleNamespace(url=SimpleNamespace(path="/app/summary"))
        with self.assertRaises(StopIteration) as done:
            ns["_redis_unavailable_response"](
                request, RedisError("private redis host")).send(None)
        response = done.exception.value
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body), {"error": "redis_unavailable"})
        self.assertEqual(logs[0][1]["path"], "/app/summary")
        self.assertNotIn("private", response.body.decode())

        async def unavailable():
            raise RedisError("private redis host")

        health = _load_nodes(
            ROOT / "api" / "main.py", {"get_system_health"},
            {"RedisError": RedisError, "JSONResponse": JSONResponse,
             "logging": logging, "_log": lambda *args, **kw: logs.append((args, kw)),
             "_ensure_redis": unavailable},
        )
        with self.assertRaises(StopIteration) as done:
            health["get_system_health"]().send(None)
        response = done.exception.value
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body)["redis"],
                         {"connected": False, "error": "redis_unavailable"})

    def test_invalid_history_cursor_is_400_before_redis_access(self):
        ns = _load_nodes(
            ROOT / "api" / "main.py", {"get_history"},
            {"JSONResponse": JSONResponse,
             "_MSG_ID_RE": re.compile(r"^\d{1,20}-\d{1,20}$"),
             "_ensure_redis": lambda: self.fail("Redis must not be accessed")},
        )
        with self.assertRaises(StopIteration) as done:
            ns["get_history"](before="invalid").send(None)
        response = done.exception.value
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body), {"error": "invalid before msg_id"})

        ns["AlertFeedback"] = object
        _load_nodes(ROOT / "api" / "main.py", {"post_alert_feedback"}, ns)
        with self.assertRaises(StopIteration) as done:
            ns["post_alert_feedback"]("bad-id", object()).send(None)
        self.assertEqual(done.exception.value.status_code, 400)

    def test_empty_token_and_invalid_device_id_are_422_inputs(self):
        ns = _load_nodes(ROOT / "api" / "main.py", {"TokenRegistration"},
                         {"BaseModel": BaseModel, "Field": Field})
        with self.assertRaises(ValidationError):
            ns["TokenRegistration"](token="", device_id="phone-1")
        with self.assertRaises(ValidationError):
            ns["TokenRegistration"](token="token", device_id="bad id")
        self.assertEqual(ns["TokenRegistration"](token="token", device_id="phone-1").device_id,
                         "phone-1")


if __name__ == "__main__":
    unittest.main()
