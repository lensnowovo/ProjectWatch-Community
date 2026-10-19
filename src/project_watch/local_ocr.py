"""Optional offline Chinese/English PDF OCR, called inside the bounded parser child."""

from __future__ import annotations

import importlib.util
import logging
import shutil
from functools import lru_cache
from pathlib import Path


def ocr_capabilities() -> dict:
    local = all(importlib.util.find_spec(name) is not None
                for name in ("rapidocr", "onnxruntime", "pypdfium2"))
    command = bool(shutil.which("tesseract") and shutil.which("pdftoppm"))
    return {"available": local or command, "engine": "rapidocr" if local else "tesseract" if command else "none"}


@lru_cache(maxsize=1)
def _engine():
    from rapidocr import RapidOCR

    # Packaged models only. The parser child receives no provider keys.
    logging.getLogger("RapidOCR").setLevel(logging.ERROR)
    models = Path(importlib.util.find_spec("rapidocr").origin).parent / "models"
    return RapidOCR(params={
        "Det.model_path": str(models / "ch_PP-OCRv4_det_infer.onnx"),
        "Cls.model_path": str(models / "ch_ppocr_mobile_v2.0_cls_infer.onnx"),
        "Rec.model_path": str(models / "ch_PP-OCRv4_rec_infer.onnx"),
        "Rec.rec_keys_path": str(models / "ppocr_keys_v1.txt"),
        "EngineConfig.onnxruntime.intra_op_num_threads": 1,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    })


def recognize_pdf_page(path: Path, page_number: int) -> str | None:
    if not all(importlib.util.find_spec(name) is not None
               for name in ("rapidocr", "onnxruntime", "pypdfium2")):
        return None
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[page_number - 1]
        try:
            # Bound pixels independently of an adversarial PDF's physical page size.
            scale = min(3.0, 2200 / max(page.get_size()))
            bitmap = page.render(scale=scale)
            try:
                image = bitmap.to_pil().convert("RGB")
                result = _engine()(image)
                texts = getattr(result, "txts", None)
                if texts is None:
                    return None
                text = "\n".join(str(t) for t in texts).strip()
                return text[:100_000] or None
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        pdf.close()
