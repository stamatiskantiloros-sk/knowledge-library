"""Web ingestor: URLs (from .url/.webloc/urls.txt) and saved .html files.

trafilatura extracts the main content as Markdown. The URL and fetch date are always stored,
because web pages change; the content hash is of the extracted text, so a re-fetch only
produces a new version when the page actually changed.
"""
from __future__ import annotations

import urllib.request

from ..contract import SourceRecord, hash_file, hash_text, make_id, now_iso, stage
from .base import HTML_EXT, Context, Ingestor, IngestResult, InputItem, first_working, register


def _trafilatura(html: str | None, url: str | None, cfg: dict) -> dict:
    import trafilatura
    if html is None:
        html = trafilatura.fetch_url(url)
        if not html:
            raise RuntimeError(f"could not fetch {url}")
    md = trafilatura.extract(html, url=url, output_format="markdown", include_tables=True,
                             include_links=True, include_images=False, with_metadata=False,
                             favor_recall=True)
    if not md:
        raise RuntimeError("trafilatura found no main content")
    meta = trafilatura.extract_metadata(html, default_url=url)
    return {"markdown": md, "title": getattr(meta, "title", None),
            "date": getattr(meta, "date", None), "author": getattr(meta, "author", None),
            "sitename": getattr(meta, "sitename", None)}


def _jina(html: str | None, url: str | None, cfg: dict) -> dict:
    if not url:
        raise RuntimeError("jina reader needs a URL")
    req = urllib.request.Request(f"https://r.jina.ai/{url}", headers={"Accept": "text/markdown"})
    with urllib.request.urlopen(req, timeout=cfg["web"].get("timeout_s", 30)) as r:
        md = r.read().decode("utf-8", "replace")
    title = next((ln[7:].strip() for ln in md.splitlines() if ln.startswith("Title: ")), None)
    return {"markdown": md, "title": title}


RUNNERS = {"trafilatura": _trafilatura, "jina": _jina}


@register
class WebIngestor(Ingestor):
    name = "web"

    def can_handle(self, item: InputItem) -> bool:
        return item.kind == "url" or (item.kind == "file" and item.suffix in HTML_EXT)

    def ingest(self, item: InputItem, ctx: Context) -> IngestResult:
        cfg = ctx.config
        wc = cfg["web"]
        url = item.url
        html = None
        if item.kind == "file":
            html = item.path.read_text(encoding="utf-8", errors="replace")
        backends = [wc["backend"], *[b for b in wc.get("fallbacks", []) if b != wc["backend"]]]
        if cfg.get("local_only") or item.kind == "file":
            backends = [b for b in backends if b != "jina"]   # jina is a third-party service
        fetched_at = now_iso()
        used, res, errs = first_working(backends, RUNNERS, html, url, cfg)
        md = res["markdown"].strip()
        title = res.get("title") or (url or item.path.stem)
        content_hash = hash_text(md)
        sid = make_id(title, content_hash)
        flags = [f"backend fallback: {e}" for e in errs]
        if len(md) < 400:
            flags.append("very short extraction; page may be dynamic, paywalled or mostly media")
        first = md.lstrip().splitlines()[0] if md.strip() else ""
        if first.lstrip("# ").strip() == title.strip():
            md = md.lstrip()[len(first):].lstrip()
        lines = [f"# {title}", ""]
        meta = [f"- URL: {url}" if url else f"- Saved page: inbox/{item.key}"]
        for k in ("author", "sitename", "date"):
            if res.get(k):
                meta.append(f"- {k.capitalize()}: {res[k]}")
        meta.append(f"- Fetched: {fetched_at}")
        body = "\n".join(lines + meta) + "\n\n" + md
        pipeline = {"extraction": stage(used, package="trafilatura" if used == "trafilatura" else None)}
        w = ctx.writer(sid)
        try:
            rec = SourceRecord(id=sid, title=title, source_type="web",
                               origin=[url] if url else [f"inbox/{item.key}"], hash=content_hash,
                               original_date=res.get("date"), fetch_date=fetched_at,
                               pipeline=pipeline, flags=flags)
            md_path = w.commit(rec, body)
        except Exception:
            w.abort()
            raise
        return IngestResult(sid, md_path, pipeline, flags)


def item_hash(item: InputItem) -> str:
    """Change detection before fetching: URLs are re-fetched only with --force or reprocess;
    saved HTML files are hashed like any file."""
    if item.kind == "url":
        return hash_text(item.url or "")
    return hash_file(item.path)
