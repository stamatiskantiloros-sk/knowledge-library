"""knowlib models: one provider per role, saved to the library's config, checked before a run."""
from __future__ import annotations

import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from knowlib import models
from knowlib.config import load_config


def library(tmp_path, extra: str = "") -> Path:
    root = tmp_path / "lib"
    (root / "_knowlib").mkdir(parents=True)
    (root / "_knowlib" / "config.yaml").write_text(
        "# written by knowlib init\n\nlibrary: {mode: inplace}\nframe_selection: {backend: mock}\n" + extra)
    return root


class Server:
    """A tiny OpenAI-compatible server: /v1/models, and chat completions that report prompt tokens,
    either one token per 32x32 pixels (keeps detail) or a fixed 290 per image (caps it)."""

    def __init__(self, models_list, capped=False):
        self.models_list, self.capped = models_list, capped
        self.requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send({"data": outer.models_list})

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(req)
                tokens = 10
                for part in req["messages"][0]["content"]:
                    if part.get("type") == "image_url":
                        from PIL import Image
                        raw = base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
                        w, h = Image.open(io.BytesIO(raw)).size
                        tokens += 290 if outer.capped else w * h // 1024
                self._send({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": tokens}})

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


VISION = {"id": "vlm", "architecture": {"input_modalities": ["text", "image"]}, "meta": {"n_ctx": 262144}}


def test_specs_parse_and_bad_ones_say_how():
    assert models.parse_spec("session:opus") == {"backend": "agent", "agent_model": "opus"}
    assert models.parse_spec("lmstudio:gemma")["manage_lms"] is True
    sec = models.parse_spec("server:https://gpu.example/v1/|vlm|GPU_KEY")
    assert sec == {"backend": "openai", "base_url": "https://gpu.example/v1", "openai_model": "vlm",
                   "manage_lms": False, "api_key_env": "GPU_KEY"}
    assert models.parse_spec("default") is None
    for bad in ("session:gpt", "server:vlm", "cloud:x"):
        with pytest.raises(ValueError):
            models.parse_spec(bad)


def test_choices_are_saved_per_role_and_read_back(tmp_path):
    root = library(tmp_path, "frame_selection:\n  backend: openai\n  caption_model: {openai_model: big}\n"
                             "notes_review: {openai_model: old, max_passage_chars: 9000}\n")
    path = models.save(root, {"reading": models.parse_spec("server:https://gpu.example/v1|vlm|GPU_KEY"),
                              "writer": models.parse_spec("session:sonnet"),
                              "review": models.parse_spec("session:opus"),
                              "digest": models.parse_spec("lmstudio:small")})
    assert path.with_suffix(".yaml.bak").exists() and path.read_text().startswith("# written by knowlib init")
    cfg = load_config(root)
    got = models.summary(cfg)
    assert got["reading"] == "server gpu.example: vlm"                 # the old caption_model no longer overrides it
    assert got["writer"] == "session: sonnet" and got["review"] == "session: opus"
    assert got["digest"] == "LM Studio: small"
    assert cfg["notes_review"]["max_passage_chars"] == 9000           # settings that are not the provider stay
    assert cfg["pdf"]["figures"]["model"] is None
    models.save(root, {"review": None})
    assert "session" not in models.describe(models.section(load_config(root), "review"))


def test_check_finds_a_missing_model_a_text_only_model_and_local_only(tmp_path):
    srv = Server([{"id": "other"}, {"id": "textonly", "architecture": {"input_modalities": ["text"]}}])
    try:
        root = library(tmp_path)
        models.save(root, {"reading": models.parse_spec(f"server:{srv.url}|vlm")})
        ok, msg = models.check(load_config(root), "reading")
        assert not ok and "does not serve vlm" in msg and "other" in msg
        models.save(root, {"reading": models.parse_spec(f"server:{srv.url}|textonly")})
        ok, msg = models.check(load_config(root), "reading")
        assert not ok and "does not take images" in msg
    finally:
        srv.close()
    models.save(root, {"review": models.parse_spec("session:opus")})
    assert models.check(load_config(root, {"local_only": True}), "review")[0] is False


def test_full_check_catches_a_server_that_caps_image_tokens(tmp_path):
    for capped in (False, True):
        srv = Server([VISION], capped=capped)
        try:
            root = library(tmp_path / str(capped))
            models.save(root, {"reading": models.parse_spec(f"server:{srv.url}|vlm")})
            ok, msg = models.check(load_config(root), "reading", full=True)
        finally:
            srv.close()
        assert ok is (not capped), msg
        assert ("caps image tokens" in msg) is capped


def test_preflight_asks_only_when_the_models_change_and_stops_on_a_failed_check(tmp_path, monkeypatch, capsys):
    srv = Server([VISION])
    try:
        root = library(tmp_path)
        models.save(root, {"reading": models.parse_spec(f"server:{srv.url}|vlm")})
        work = tmp_path / "work"
        answers = iter(["n", "y"])
        monkeypatch.setattr("builtins.input", lambda q: next(answers))
        assert models.preflight(load_config(root), work, "process", interactive=True) is False   # new: asked, said no
        assert models.preflight(load_config(root), work, "process", interactive=True) is True    # asked again, yes
        assert models.preflight(load_config(root), work, "process", interactive=True) is True    # same: not asked
        assert "checks OK" in capsys.readouterr().out
        assert models.preflight(load_config(root), work, "pending") is True                     # uses no model
    finally:
        srv.close()
    assert models.preflight(load_config(root), work, "process", interactive=False) is False      # server gone
    assert "does not answer" in capsys.readouterr().out


def test_the_models_command_sets_and_shows_roles(tmp_path, capsys):
    from knowlib.cli import main
    root = library(tmp_path)
    assert main(["models", "-L", str(root), "--use", "writer=session:sonnet", "--use", "review=session:opus"]) == 0
    out = capsys.readouterr().out
    assert "writer    session: sonnet" in out and "review    session: opus" in out
    assert main(["models", "-L", str(root), "--use", "nobody=session:opus"]) == 2


def test_check_accepts_ids_listed_with_a_models_prefix_and_an_empty_key_setting(tmp_path, monkeypatch):
    # Google's OpenAI-compatible endpoint lists "models/<id>" but takes the bare id in requests.
    srv = Server([{"id": "models/vlm", "architecture": {"input_modalities": ["text", "image"]}}])
    try:
        root = library(tmp_path)
        models.save(root, {"reading": models.parse_spec(f"server:{srv.url}|vlm")})
        cfg = load_config(root)
        cfg["frame_selection"]["api_key_env"] = ""          # empty means no key, not a crash
        ok, msg = models.check(cfg, "reading")
        assert ok, msg
    finally:
        srv.close()


def test_a_request_that_never_finishes_stops_at_the_deadline():
    """A server that keeps sending a byte now and then never trips urlopen's per-read timeout;
    the provider's deadline still ends the request."""
    import time
    from knowlib.providers.openai_compat import OpenAICompatProvider

    class Trickle(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                for _ in range(50):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.2)
            except OSError:
                pass

    httpd = HTTPServer(("127.0.0.1", 0), Trickle)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        p = OpenAICompatProvider({"base_url": f"http://127.0.0.1:{httpd.server_port}/v1",
                                  "openai_model": "m", "timeout_s": 1, "api_key_env": ""})
        start = time.monotonic()
        with pytest.raises(TimeoutError, match="no complete reply within 1 s"):
            p.generate([], "hi")
        assert time.monotonic() - start < 3
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_chain_role_is_saved_described_and_checked_member_by_member(tmp_path):
    srv = Server([{"id": "models/lite"}])
    try:
        root = library(tmp_path, f"providers:\n  g:\n    base_url: {srv.url}\n    models: {{lite: {{rpd: 500}}}}\n")
        models.save(root, {"review": models.parse_spec("chain:g/missing,g/lite")})
        cfg = load_config(root)
        assert models.describe(models.section(cfg, "review")) == "chain: g/missing → g/lite"
        ok, msg = models.check(cfg, "review")
        assert ok and "g/lite: ok" in msg and "does not serve missing" in msg
    finally:
        srv.close()
    with pytest.raises(ValueError, match="provider"):
        models.parse_spec("chain:lite")


def test_the_writers_model_may_not_review_and_its_family_is_a_warning(tmp_path):
    root = library(tmp_path, "providers:\n  g: {base_url: 'http://127.0.0.1:9/v1', models: {}}\n"
                             "  n: {base_url: 'http://127.0.0.1:9/v1', models: {}}\n")
    models.save(root, {"writer": {"backend": "agent", "agent_model": "minimax-m3.1-flash"},
                       "review": models.parse_spec("chain:g/gemini-3.5-flash-lite,n/minimaxai/minimax-m3.1-flash")})
    cfg = load_config(root)
    level, msg = models.writer_conflict(cfg)
    assert level == "error" and "minimax-m3.1-flash" in msg
    assert models.check(cfg, "review") == (False, msg)
    models.save(root, {"review": models.parse_spec("chain:g/gemini-3.5-flash-lite,n/minimaxai/minimax-m2")})
    assert models.writer_conflict(load_config(root))[0] == "warning"
    models.save(root, {"review": models.parse_spec("chain:g/gemini-3.5-flash-lite")})
    assert models.writer_conflict(load_config(root)) is None


def test_openrouter_spec_routes_only_to_hosts_that_keep_nothing(tmp_path, monkeypatch):
    sec = models.parse_spec("openrouter:vendor/vlm")
    assert sec["base_url"] == models.OPENROUTER_URL and sec["openai_model"] == "vendor/vlm"
    assert sec["api_key_env"] == "OPENROUTER_API_KEY"
    assert sec["provider_routing"] == {"data_collection": "deny"}
    assert models.describe(sec) == "OpenRouter: vendor/vlm"
    with pytest.raises(ValueError):
        models.parse_spec("openrouter:vlm")
    root = library(tmp_path)
    models.save(root, {"review": sec})
    cfg = load_config(root)
    assert models.summary(cfg)["review"] == "OpenRouter: vendor/vlm"
    assert models.check(load_config(root, {"local_only": True}), "review")[0] is False
    # the routing preference goes out with every request; the model's context comes from context_length
    srv = Server([{"id": "vendor/vlm", "context_length": 200000,
                   "architecture": {"input_modalities": ["text", "image"]}}])
    try:
        from knowlib.providers.openai_compat import OpenAICompatProvider
        prov = OpenAICompatProvider({**sec, "base_url": srv.url})
        assert prov.generate([], "hi") == "ok"
        assert srv.requests[-1]["provider"] == {"data_collection": "deny"}
        ok, msg = models._check_server({**sec, "base_url": srv.url}, "review", cfg, full=False)
        assert ok and "context 200000" in msg
    finally:
        srv.close()
    # switching the role to another provider drops the routing preference
    models.save(root, {"review": models.parse_spec("session:opus")})
    assert "provider_routing" not in load_config(root)["notes_review"]


def test_openrouter_models_in_a_chain_keep_the_vendor_in_the_model_id():
    from knowlib.config import DEFAULTS
    from knowlib.providers.chain import member_sections
    (m,) = member_sections({"backend": "chain", "chain": ["openrouter/vendor/model-x"]}, DEFAULTS)
    assert m["key"] == "openrouter/vendor/model-x"
    assert m["section"]["openai_model"] == "vendor/model-x"
    assert m["section"]["provider_routing"] == {"data_collection": "deny"} and not m["trains"]


def test_remote_models_get_redacted_prompts_and_masked_images(tmp_path, monkeypatch):
    from knowlib.providers import base, guard
    srv = Server([VISION])
    try:
        cfg = {"privacy": {"names": ["Jane Roe"]}, "ocr_crosscheck": {}}
        remote = {"backend": "openai", "base_url": srv.url, "openai_model": "vlm"}
        monkeypatch.setattr("knowlib.providers.openai_compat.LOCAL_HOSTS", set())   # treat the test server as remote
        base._CACHE.clear()
        prov = base.get_vision_provider(remote, cfg)
        assert isinstance(prov, guard.PrivacyGuard) and prov.backend == "openai" and prov.model == "vlm"
        assert prov.is_local is False and prov.provenance() == {"backend": "openai", "model": "vlm"}
        prov.generate([], "Jane Roe wrote to jane@example.org")
        sent = srv.requests[-1]["messages"][0]["content"][-1]["text"]
        assert "Jane" not in sent and "@" not in sent and "[a person]" in sent
        img = tmp_path / "slide.png"
        from PIL import Image
        Image.new("RGB", (200, 100), "white").save(img)
        email_line = ("Contact: someone@example.org", (10, 60, 150, 80))
        # the original shows an email on one line; the masked copy no longer does
        monkeypatch.setattr(guard, "local_lines",
                            lambda p, c: [("Revenue 1988 1989 1990", (10, 10, 150, 30))] +
                            ([email_line] if p == img else []))
        n = len(srv.requests)
        assert prov.generate([img], "Read the slide.") == "ok"
        assert len(srv.requests) == n + 1
        masked = prov.sendable(img)
        assert masked != img
        with Image.open(masked) as m:
            assert m.getpixel((80, 70)) == (0, 0, 0) and m.getpixel((80, 20)) == (255, 255, 255)
        with Image.open(img) as o:
            assert o.getpixel((80, 70)) == (255, 255, 255)        # the original is untouched
        # masking that leaves personal information visible: nothing is sent
        monkeypatch.setattr(guard, "local_lines", lambda p, c: [email_line])
        prov._sendable.clear()
        with pytest.raises(RuntimeError):                       # callers fall back as when a model fails
            prov.generate([img], "Read the slide.")
        assert len(srv.requests) == n + 1
        # an image with nothing personal goes as it is
        monkeypatch.setattr(guard, "local_lines", lambda p, c: [("Revenue 1988 1989 1990", (10, 10, 150, 30))])
        prov._sendable.clear()
        assert prov.sendable(img) == img
        # a model on this machine is not wrapped
        monkeypatch.undo()
        base._CACHE.clear()
        assert not isinstance(base.get_vision_provider(remote, cfg), guard.PrivacyGuard)
    finally:
        base._CACHE.clear()
        srv.close()


def test_openrouter_data_policy_follows_the_library_privacy_setting():
    from knowlib.providers.openai_compat import with_data_policy
    sec = models.parse_spec("openrouter:vendor/m")
    assert with_data_policy(sec, {})["provider_routing"]["data_collection"] == "deny"
    allow = with_data_policy(sec, {"privacy": {"models_may_keep_data": True}})
    assert allow["provider_routing"]["data_collection"] == "allow"
    plain = {"backend": "openai", "base_url": "https://gpu.example/v1"}
    assert with_data_policy(plain, {"privacy": {"models_may_keep_data": True}}) == plain


def test_names_said_in_class_are_proposed_with_where_the_materials_use_them():
    from knowlib import privacy
    lecture = ("---\nsource_type: session\n---\n## Slide 1\n**On slide:** Maria Elena Lagomasino, director\n"
               "**Spoken:**\n[0:01] Okay, Nick, what do you think? And you, Maria? The answer is in the book.\n"
               "[0:02] We read the article about Tim Cook. On Monday we meet again, Nick.\n")
    book = "Chapter 1. Tim Cook said profits matter. The answer is clear."
    got = {e["name"]: e for e in privacy.spoken_names({"lec": (lecture, True), "book": (book, False)})}
    assert got["Nick"]["count"] == 2 and got["Nick"]["materials"] == []
    assert got["Maria"]["materials"] == ["lec"]               # on the slide: text only, never masked
    assert "Tim Cook" in got and got["Tim Cook"]["materials"] == ["book"]
    assert "Monday" not in got and "The" not in got


def test_spoken_only_names_are_replaced_in_text_but_not_masked_in_images():
    from knowlib import privacy
    cfg = {"privacy": {"names": ["Nick"], "spoken_only": ["Maria"]}}
    assert privacy.names(cfg) == ["Nick"] and privacy.text_names(cfg) == ["Nick", "Maria"]
    assert privacy.redact("Nick and Maria", privacy.text_names(cfg)) == "[a person] and [a person]"
