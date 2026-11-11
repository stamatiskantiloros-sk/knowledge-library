"""API chains: the first model with quota left answers; quotas are counted across runs."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from knowlib.providers import quota
from knowlib.providers.chain import ChainProvider

DAY_429 = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                     "message": "Quota exceeded ... Generate content requests per day limit exceeded on Free Tier.",
                     "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                  "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                                  "quotaValue": "20"}]},
                                 {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "76616s"}]}}
MINUTE_429 = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "per minute",
                        "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                     "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                                                     "quotaValue": "5"}]},
                                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0s"}]}}


class Fake:
    """An OpenAI-compatible server: each model answers with its name, or with the replies queued for it."""

    def __init__(self):
        self.queue: dict[str, list] = {}
        self.calls: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model = req["model"]
                outer.calls.append(model)
                q = outer.queue.get(model) or []
                code, obj = q.pop(0) if q else (200, {"choices": [{"message": {"content": f"from {model}"},
                                                                  "finish_reason": "stop"}]})
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLIB_USAGE", str(tmp_path / "usage.json"))
    f = Fake()
    yield f
    f.close()


def cfg_for(url, **limits):
    return {"providers": {"g": {"base_url": url, "api_key_env": "", "reset_tz": "America/Los_Angeles",
                                "models": {"big": {"rpd": 20, **limits}, "lite": {"rpd": 500},
                                           "gemma": {"tpm": 100, "reasoning_effort": None}}}}}


def test_the_next_model_answers_when_the_first_has_spent_its_day(fake):
    chain = ChainProvider({"backend": "chain", "chain": ["g/big", "g/lite"]}, cfg_for(fake.url))
    fake.queue["big"] = [(429, [DAY_429])]
    assert chain.generate([], "hi") == "from lite" and chain.answered_by == "lite"
    assert chain.generate([], "hi") == "from lite"           # big is skipped now, without asking it
    assert fake.calls == ["big", "lite", "lite"]
    assert any("big: 1/20 (quota spent; free again in" in line for line in chain.usage())


def test_a_per_minute_limit_waits_and_asks_the_same_model_again(fake, monkeypatch):
    monkeypatch.setattr("knowlib.providers.chain.time.sleep", lambda s: None)
    chain = ChainProvider({"backend": "chain", "chain": ["g/big", "g/lite"]}, cfg_for(fake.url))
    fake.queue["big"] = [(429, MINUTE_429)]
    assert chain.generate([], "hi") == "from big"
    assert fake.calls == ["big", "big"]


def test_the_daily_count_stops_a_model_before_the_server_has_to(fake):
    chain = ChainProvider({"backend": "chain", "chain": ["g/big", "g/lite"]}, cfg_for(fake.url, rpd=2))
    replies = [chain.generate([], "hi") for _ in range(3)]
    assert replies == ["from big", "from big", "from lite"]


def test_a_request_bigger_than_a_models_minute_budget_goes_to_the_next(fake):
    chain = ChainProvider({"backend": "chain", "chain": ["g/gemma", "g/lite"]}, cfg_for(fake.url))
    assert chain.generate([], "x" * 2000) == "from lite"     # ~500 tokens, gemma allows 100 a minute
    assert fake.calls == ["lite"]


def test_when_every_model_fails_the_error_names_each(fake):
    chain = ChainProvider({"backend": "chain", "chain": ["g/big"]}, cfg_for(fake.url))
    fake.queue["big"] = [(400, {"error": "bad"})]
    with pytest.raises(RuntimeError, match="g/big: HTTP 400"):
        chain.generate([], "hi")


def test_a_model_setting_overrides_the_roles(fake):
    chain = ChainProvider({"backend": "chain", "chain": ["g/gemma", "g/lite"], "reasoning_effort": "medium"},
                          cfg_for(fake.url))
    assert chain.members[0]["provider"].reasoning_effort is None      # gemma takes no thinking level
    assert chain.members[1]["provider"].reasoning_effort == "medium"


def test_the_ledger_starts_a_new_count_each_day(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLIB_USAGE", str(tmp_path / "usage.json"))
    assert quota.reserve("g/m", {"rpd": 1}, "UTC", 10)
    assert not quota.reserve("g/m", {"rpd": 1}, "UTC", 10)
    data = json.loads((tmp_path / "usage.json").read_text())
    data["g/m"]["day"] = "2000-01-01"
    (tmp_path / "usage.json").write_text(json.dumps(data))
    assert quota.reserve("g/m", {"rpd": 1}, "UTC", 10)


def test_caches_find_a_reply_from_any_model_in_the_chain(fake, tmp_path):
    from knowlib.handbook.maker import Cache
    chain = ChainProvider({"backend": "chain", "chain": ["g/big", "g/lite"]}, cfg_for(fake.url))
    cache = Cache(tmp_path / "c.json")
    cache.put(Cache.key("lite", "q"), "old answer from lite")
    assert cache.ask_checked(chain, "q", 100)[0] == "old answer from lite"
    fake.queue["big"] = [(429, [DAY_429])]
    assert cache.ask_checked(chain, "new", 100)[0] == "from lite"
    assert Cache.key("lite", "new") in cache.data                     # stored under the model that answered


def test_review_batches_many_notes_into_one_request_and_caches_each(fake, tmp_path):
    from knowlib.handbook.maker import Cache
    from knowlib.notereview import review_in_batches
    cfg = cfg_for(fake.url)
    cfg["providers"]["g"]["models"]["lite"] = {"tpm": 100000, "context": 1000000}
    chain = ChainProvider({"backend": "chain", "chain": ["g/lite"]}, cfg)
    answers = {"a": {"claims": [{"n": 1, "verdict": "supported", "reason": "r"}]},
               "b": {"claims": [{"n": 1, "verdict": "partly", "reason": "r"}]}}       # c is missing
    fake.queue["lite"] = [(200, {"choices": [{"message": {"content": json.dumps({"notes": answers})},
                                              "finish_reason": "stop"}]})]
    cache = Cache(tmp_path / "c.json")
    done = review_in_batches([("a", "prompt a", 1), ("b", "prompt b", 1), ("c", "prompt c", 1)], chain, cache, {})
    assert done == 2 and fake.calls == ["lite"]
    assert json.loads(cache.lookup(chain, "prompt b"))["claims"][0]["verdict"] == "partly"
    assert cache.lookup(chain, "prompt c") is None                    # left for the per-note pass


def test_the_digest_asks_for_several_parts_per_request_and_caches_each(fake, tmp_path):
    from knowlib.digest import digest_in_batches
    from knowlib.handbook.maker import Cache
    cfg = cfg_for(fake.url)
    cfg["providers"]["g"]["models"]["lite"] = {"tpm": 100000, "context": 1000000}
    chain = ChainProvider({"backend": "chain", "chain": ["g/lite"]}, cfg)
    part = "### Key terms\n- **T** (p. {n}): x.\n\n### Formulas\n(none)\n\n### Worked examples\n(none)"
    reply = (f"=== DIGEST 1 ===\n{part.format(n=1)}\n=== DIGEST 2 ===\n### Key terms\n- half"   # 2 is incomplete
             f"\n=== DIGEST 3 ===\n{part.format(n=3)}")
    fake.queue["lite"] = [(200, {"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]})]
    cache = Cache(tmp_path / "d.json")
    assert digest_in_batches(["p1", "p2", "p3"], chain, cache, {}) == 2 and fake.calls == ["lite"]
    assert "(p. 3)" in cache.lookup(chain, "p3") and cache.lookup(chain, "p2") is None


def test_a_digest_batch_cut_off_at_the_token_limit_drops_its_last_part(fake, tmp_path):
    from knowlib.digest import digest_in_batches
    from knowlib.handbook.maker import Cache
    cfg = cfg_for(fake.url)
    cfg["providers"]["g"]["models"]["lite"] = {"tpm": 100000}
    chain = ChainProvider({"backend": "chain", "chain": ["g/lite"]}, cfg)
    part = "### Key terms\n- a (p. 1).\n### Formulas\n(none)\n### Worked examples\n- **Shop** (pp. 1–2): fine"
    reply = f"=== DIGEST 1 ===\n{part}\n=== DIGEST 2 ===\n{part}"
    fake.queue["lite"] = [(200, {"choices": [{"message": {"content": reply}, "finish_reason": "length"}]})]
    cache = Cache(tmp_path / "d.json")
    assert digest_in_batches(["p1", "p2"], chain, cache, {}) == 1
    assert cache.lookup(chain, "p1") and cache.lookup(chain, "p2") is None
