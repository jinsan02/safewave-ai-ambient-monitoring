"""경보 경로 점검: ai:emergency → API alert worker → FCM 발송 시도.

실제 낙상 없이 API 쪽 경로를 확인한다. ai-qwen이 꺼져 있어도 동작해야 한다.
1) 시험용 기기 토큰을 등록한다(device_id=e2e-check, 끝나면 삭제).
2) 시험 노드(기본 9)로 critical 항목을 ai:emergency에 쓴다(slm_mode=e2e_check).
3) API가 phase2:active:{node} 락과 notify:sent:{msg_id}:e2e-check 키를 만드는지 본다.
   Firebase 키가 없으면 실제 푸시는 실패하지만(api 로그 fcm_send_failed) 발송 시도까지는 확인된다.
--rule-wait N: ai-experts 규칙 경보(slm_mode=rule)가 N초 안에 기록되는지도 확인한다.

예) python scripts/alert_e2e_check.py --api http://127.0.0.1:8000 --rule-wait 60
"""

import argparse
import json
import sys
import time
import urllib.request

import redis

DEVICE_ID = "e2e-check"


def post_json(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def wait_for(fn, timeout: float, interval: float = 0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--redis-host", default="127.0.0.1")
    ap.add_argument("--redis-port", type=int, default=6379)
    ap.add_argument("--node", type=int, default=9)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--rule-wait", type=float, default=0.0)
    args = ap.parse_args()

    r = redis.Redis(host=args.redis_host, port=args.redis_port, decode_responses=True)
    results = {}

    post_json(f"{args.api}/auth/register-token", {"token": "e2e-dummy-token", "device_id": DEVICE_ID})
    try:
        r.delete(f"phase2:active:{args.node}")
        entry = {
            "ts_ms": int(time.time() * 1000), "node_id": args.node, "risk_score": 0.9,
            "risk_level": "critical", "emergency": True, "slm_invoked": False,
            "slm_mode": "e2e_check", "summary": "경보 경로 점검(시험)",
        }
        msg_id = r.xadd("ai:emergency", {"data": json.dumps(entry, ensure_ascii=False)},
                        maxlen=3600, approximate=True)
        t0 = time.time()
        results["lock"] = wait_for(lambda: r.get(f"phase2:active:{args.node}"), args.timeout)
        results["notify_claimed"] = bool(
            wait_for(lambda: r.exists(f"notify:sent:{msg_id}:{DEVICE_ID}"), args.timeout)
        )
        results["claim_delay_s"] = round(time.time() - t0, 2)
    finally:
        r.delete(f"fcm:token:{DEVICE_ID}")

    if args.rule_wait > 0:
        since = f"{int(time.time() * 1000) - int(args.rule_wait * 1000)}-0"

        def rule_entry():
            for _id, fields in r.xrange("ai:emergency", min=since):
                data = json.loads(fields.get("data", "{}"))
                if data.get("slm_mode") == "rule":
                    return data
            return None

        found = wait_for(rule_entry, args.rule_wait, 1.0)
        results["rule_alert"] = {k: found.get(k) for k in ("node_id", "risk_level", "summary")} \
            if found else None

    ok = bool(results.get("lock") and results.get("notify_claimed")
              and (args.rule_wait <= 0 or results.get("rule_alert")))
    print(json.dumps({"ok": ok, **results}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
