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
