"""Synthetic test material: a lecture session (video + better separate recording + slide deck),
a loose audio lecture, a PDF with a figure, a .docx, a .pptx, a saved web page, and an
unsupported file. Generated, so the suite runs anywhere ffmpeg is installed."""
from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

SR = 16000
W, H = 1920, 1080


def _font(size: int):
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
              "/Library/Fonts/Arial.ttf"]:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def slide(path: Path, title: str, lines: list[str], small: bool = False) -> Path:
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 150], fill=(20, 60, 120))
    d.text((80, 40), title, fill="white", font=_font(64))
    f = _font(26 if small else 48)
    y = 220
    for ln in lines:
        d.text((100, y), ln, fill="black", font=f)
        y += 40 if small else 80
    img.save(path)
    return path


def speaker(path: Path, seed: int) -> Path:
    rng = np.random.default_rng(seed)
    a = np.full((H, W, 3), 128, np.uint8) + rng.integers(0, 3, (H, W, 3), dtype=np.uint8)
    Image.fromarray(a).save(path)
    return path


def speechlike(seconds: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    env = np.zeros(n, np.float32)
    t = 0
    while t < n - SR:
        L = int(rng.uniform(0.08, 0.35) * SR)
        env[t:t + L] = rng.uniform(0.3, 1.0) * np.hanning(L)
        t += L + int(rng.exponential(0.12) * SR)
        if rng.random() < 0.03:
            t += int(rng.uniform(0.4, 1.5) * SR)
    return (rng.standard_normal(n).astype(np.float32) * env)


def write_wav(path: Path, x: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *args], check=True)


def build_session(folder: Path, tmp: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    s1 = slide(tmp / "s1.png", "Cost of capital", ["Why firms need a hurdle rate", "Investors' required return"])
    s2 = slide(tmp / "s2.png", "WACC", ["WACC = E/V x Re + D/V x Rd x (1 - Tc)", "Example: WACC = 8.5%"])
    s3a = slide(tmp / "s3a.png", "Capital structure", ["- Debt is cheaper than equity"])
    s3b = slide(tmp / "s3b.png", "Capital structure", ["- Debt is cheaper than equity", "- But leverage raises risk"])
    rows = ["Year   Revenue   EBIT    Debt    Equity   WACC"] + \
           [f"{2018 + i}   {1200 + 87 * i}      {210 + 13 * i}     {640 + 25 * i}     {900 + 31 * i}     {7.9 + i / 10:.1f}%" for i in range(8)]
    s4 = slide(tmp / "s4.png", "Case study: historical figures", rows, small=True)
    sp = speaker(tmp / "sp.png", 1)
    timeline = [(s1, 20), (sp, 15), (s2, 25), (s3a, 10), (s3b, 20), (s4, 30)]   # 120 s
    lst = tmp / "concat.txt"
    lst.write_text("".join(f"file '{p}'\nduration {d}\n" for p, d in timeline) + f"file '{timeline[-1][0]}'\n")

    src = speechlike(125, seed=3)
    lead = 2.3   # the camera started 2.3 s before the separate recorder
    cam = np.concatenate([np.zeros(int(lead * SR), np.float32), src])[: int(120 * SR)]
    cam = 0.2 * np.convolve(cam, np.exp(-np.arange(600) / 150) / 40, mode="same") + \
        0.02 * np.random.default_rng(9).standard_normal(len(cam)).astype(np.float32)
    write_wav(tmp / "cam.wav", cam)
    ff("-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(tmp / "cam.wav"), "-vf", "fps=25,format=yuv420p",
       "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-shortest", str(folder / "lecture.mp4"))
    write_wav(tmp / "rec.wav", 0.8 * src[: int(115 * SR)])
    ff("-i", str(tmp / "rec.wav"), "-c:a", "aac", "-b:a", "128k", str(folder / "recorder.m4a"))

    # the slide deck as an image-only PDF (no text layer: exercises the scan/OCR path)
    import fitz
    doc = fitz.open()
    for p in (s1, s2, s3b, s4):
        page = doc.new_page(width=960, height=540)
        page.insert_image(page.rect, filename=str(p))
    doc.save(folder / "slides.pdf")
    (folder / "session.yaml").write_text(
        "title: Corporate Finance - Lecture 4\ndate: 2026-10-28\nlanguage: en\n"
        "glossary: [WACC, CAPM, NPV]\nprimary_audio: recorder.m4a\nslides: slides.pdf\ntags: [mba, finance]\n",
        encoding="utf-8")
    return {"lead_s": lead}


def build_inbox(inbox: Path, tmp: Path) -> dict:
    inbox.mkdir(parents=True, exist_ok=True)
    info = build_session(inbox / "mba" / "2026-10-28-corporate-finance-l4", tmp / "session")

    # audio-only lecture
    write_wav(tmp / "talk.wav", 0.6 * speechlike(70, seed=5))
    ff("-i", str(tmp / "talk.wav"), "-c:a", "libmp3lame", "-b:a", "96k", str(inbox / "guest-talk.mp3"))

    # PDF with text and a vector chart
    import fitz
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 72), "Regulated asset base and allowed return", fontsize=16)
    p.insert_text((72, 110), "The allowed return is set as WACC times the regulated asset base.", fontsize=11)
    for i in range(60):   # bar chart from vector drawings
        p.draw_rect(fitz.Rect(80 + i * 7, 400 - (i * 37 % 150), 85 + i * 7, 400), color=(0, 0, 1), fill=(0.3, 0.5, 0.9))
    p2 = doc.new_page()
    p2.insert_text((72, 72), "Appendix: methodology", fontsize=14)
    p2.insert_text((72, 100), "Beta is estimated from comparable listed transmission operators.", fontsize=11)
    doc.set_metadata({"title": "Regulatory WACC note"})
    doc.save(inbox / "regulatory-wacc-note.pdf")

    # docx via pandoc
    md = tmp / "memo.md"
    md.write_text("# Supplier evaluation memo\n\nCriteria agreed for the tender.\n\n"
                  "| Criterion | Weight |\n| --- | --- |\n| Price | 40% |\n| Technical | 60% |\n", encoding="utf-8")
    if shutil.which("pandoc"):
        subprocess.run(["pandoc", str(md), "-o", str(inbox / "supplier-memo.docx")], check=True)

    # pptx
    try:
        from pptx import Presentation
        prs = Presentation()
        for t, b, n in [("Project kickoff", "Scope and milestones", "Mention the budget"),
                        ("Risks", "Supplier delay\nPermit timing", "")]:
            s = prs.slides.add_slide(prs.slide_layouts[1])
            s.shapes.title.text = t
            s.placeholders[1].text = b
            if n:
                s.notes_slide.notes_text_frame.text = n
        prs.save(inbox / "kickoff.pptx")
    except ImportError:
        pass

    # saved web page
    para = "Transmission system operators recover costs through regulated tariffs. " * 12
    (inbox / "tso-article.html").write_text(
        f"<html><head><title>How gas TSOs earn returns</title></head><body><nav>Home | About</nav>"
        f"<article><h1>How gas TSOs earn returns</h1><p>{para}</p><h2>Allowed return</h2><p>{para}</p></article>"
        f"<footer>© 2026</footer></body></html>", encoding="utf-8")

    (inbox / "archive.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    return info


TEST_CONFIG = """
transcription: {backend: mock}
frame_selection: {backend: mock}
ocr_crosscheck: {backends: [tesseract], tesseract_langs: eng}
pdf: {backend: docling, fallbacks: [pymupdf]}
office: {backend: docling, fallbacks: [pandoc]}
"""
