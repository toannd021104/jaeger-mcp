"""
Seed demo traces into Jaeger (OTLP/HTTP on :4318).

Scenario (e-commerce checkout with a failure):
    checkout-service
      ├── payment-service
      │     └── bank-api        (TIMEOUT -> error, status 500)
      └── inventory-service     (ok)

Run on the MCP EC2 instance (Jaeger listens on localhost:4318):
    python3 seed_traces.py
No external dependencies — uses only the Python standard library.
"""

import json
import os
import random
import time
import urllib.request

OTLP = os.environ.get("OTLP_URL", "http://localhost:4318/v1/traces")

# Nanosecond clock helpers
NOW_NS = int(time.time() * 1_000_000_000)


def _hexid(n_bytes: int) -> str:
    return os.urandom(n_bytes).hex()


def span(trace_id, span_id, parent_id, name, start_ns, dur_ns, attrs=None, status_code=0, status_msg=""):
    s = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": 2,  # SERVER
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(start_ns + dur_ns),
        "attributes": [
            {"key": k, "value": {"stringValue": str(v)}} for k, v in (attrs or {}).items()
        ],
        "status": {"code": status_code, "message": status_msg},
    }
    if parent_id:
        s["parentSpanId"] = parent_id
    return s


def resource_spans(service_name, spans):
    return {
        "resource": {
            "attributes": [
                {"key": "service.name", "value": {"stringValue": service_name}}
            ]
        },
        "scopeSpans": [{"scope": {"name": "demo-seeder"}, "spans": spans}],
    }


def build_failed_checkout(offset_ns):
    """One checkout trace that fails at bank-api (500)."""
    trace_id = _hexid(16)
    base = NOW_NS - offset_ns

    checkout_id = _hexid(8)
    payment_id = _hexid(8)
    bank_id = _hexid(8)
    inventory_id = _hexid(8)

    ms = 1_000_000

    # bank-api times out -> error
    bank = span(
        trace_id, bank_id, payment_id, "POST /charge",
        base + 60 * ms, 3000 * ms,
        attrs={"http.method": "POST", "http.route": "/charge", "http.status_code": 504,
               "error": "true", "error.message": "upstream bank gateway timeout after 3000ms"},
        status_code=2, status_msg="bank gateway timeout",
    )
    # payment-service fails because bank failed -> 500
    payment = span(
        trace_id, payment_id, checkout_id, "charge-card",
        base + 50 * ms, 3050 * ms,
        attrs={"http.method": "POST", "http.route": "/pay", "http.status_code": 500,
               "error": "true", "error.message": "payment failed: bank timeout"},
        status_code=2, status_msg="payment failed",
    )
    # inventory-service ok
    inventory = span(
        trace_id, inventory_id, checkout_id, "reserve-stock",
        base + 20 * ms, 25 * ms,
        attrs={"http.method": "POST", "http.route": "/reserve", "http.status_code": 200},
        status_code=1,
    )
    # checkout root -> 500
    checkout = span(
        trace_id, checkout_id, None, "POST /checkout",
        base, 3120 * ms,
        attrs={"http.method": "POST", "http.route": "/checkout", "http.status_code": 500,
               "error": "true", "error.message": "checkout failed downstream"},
        status_code=2, status_msg="checkout failed",
    )

    return [
        resource_spans("checkout-service", [checkout]),
        resource_spans("payment-service", [payment]),
        resource_spans("bank-api", [bank]),
        resource_spans("inventory-service", [inventory]),
    ]


def build_ok_checkout(offset_ns):
    """A healthy checkout trace for contrast."""
    trace_id = _hexid(16)
    base = NOW_NS - offset_ns
    ms = 1_000_000

    checkout_id = _hexid(8)
    payment_id = _hexid(8)
    bank_id = _hexid(8)
    inventory_id = _hexid(8)

    bank = span(trace_id, bank_id, payment_id, "POST /charge",
                base + 60 * ms, 120 * ms,
                attrs={"http.status_code": 200}, status_code=1)
    payment = span(trace_id, payment_id, checkout_id, "charge-card",
                   base + 50 * ms, 150 * ms,
                   attrs={"http.status_code": 200}, status_code=1)
    inventory = span(trace_id, inventory_id, checkout_id, "reserve-stock",
                     base + 20 * ms, 25 * ms,
                     attrs={"http.status_code": 200}, status_code=1)
    checkout = span(trace_id, checkout_id, None, "POST /checkout",
                    base, 230 * ms,
                    attrs={"http.status_code": 200}, status_code=1)

    return [
        resource_spans("checkout-service", [checkout]),
        resource_spans("payment-service", [payment]),
        resource_spans("bank-api", [bank]),
        resource_spans("inventory-service", [inventory]),
    ]


def send(resource_spans_list):
    payload = {"resourceSpans": resource_spans_list}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(OTLP, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def main():
    sent = 0
    ms = 1_000_000
    # a few failed checkouts + some healthy ones, spread over the last ~30 min
    for i in range(5):
        send(build_failed_checkout(offset_ns=(i + 1) * 120 * 1000 * ms))
        sent += 1
    for i in range(8):
        send(build_ok_checkout(offset_ns=(i + 1) * 90 * 1000 * ms))
        sent += 1
    print(f"Seeded {sent} traces into Jaeger via {OTLP}")


if __name__ == "__main__":
    main()
