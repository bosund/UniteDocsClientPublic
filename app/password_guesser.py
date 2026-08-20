
import multiprocessing
import time
import threading
from pathlib import Path
import pypdfium2 as pdfium

from . import pdf_crypto
from .logging_config import get_logger

logger = get_logger(__name__)


# Global variables for multiprocessing worker initialization
_worker_pdf_data = None
_worker_pdf_path = None
_worker_verifier = None

def _init_worker(pdf_path: str):
    """Initializer for multiprocessing pool to load PDF data once per worker."""
    global _worker_pdf_data, _worker_pdf_path
    _worker_pdf_path = pdf_path
    with open(pdf_path, "rb") as f:
        _worker_pdf_data = f.read()


def _init_crypto_worker(params):
    """Initializer that builds a password verifier once per worker.

    The verifier tests a candidate with only the standard security handler's
    key-derivation step, so no PDF parsing happens per attempt.
    """
    global _worker_verifier
    _worker_verifier = pdf_crypto.make_verifier(params)


def _worker_crypto_task(args) -> str | None:
    """Worker function for the hash-based numeric bruteforce."""
    start, end, length, _ = args
    verify = _worker_verifier
    for i in range(start, end):
        attempt = str(i).zfill(length)
        if verify(attempt):
            return attempt
    return None

def _worker_task(args) -> str | None:
    """Worker function for numeric bruteforce."""
    start, end, length, _ = args # pdf_data is not used directly, _worker_pdf_data is used
    
    for i in range(start, end):
        attempt = str(i).zfill(length)
        try:
            pdfium.PdfDocument(_worker_pdf_data, password=attempt)
            return attempt
        except Exception:
            pass
    return None

# Sentinel: the crypto fast path could not handle this file (unsupported or
# non-standard security handler) and the caller should use the pdfium/C path.
_CRYPTO_UNSUPPORTED = object()


def _bruteforce_numeric_crypto(pdf_data: bytes, max_len: int, cpu_count: int,
                               ui_stop_flag: threading.Event | None):
    """Numeric bruteforce that verifies each candidate against the encryption
    hash directly (no PDF parse per attempt).

    Returns the found password (str), ``None`` if the whole space was searched
    without a match, or ``_CRYPTO_UNSUPPORTED`` if this file cannot be handled
    and the caller should fall back to the pdfium path.
    """
    try:
        params = pdf_crypto.extract_encryption_params(pdf_data)
    except Exception as e:
        logger.warning("Encryption param extraction failed: %s", e)
        params = None
    if params is None:
        return _CRYPTO_UNSUPPORTED

    # Revision 6 (AES-256) uses a deliberately expensive hash, so use smaller
    # batches to keep the stop flag responsive; RC4/AES-128 attempts are cheap.
    batch = 2000 if params.R >= 5 else 20000

    def task_gen():
        for length in range(1, max_len + 1):
            max_val = 10 ** length
            for batch_start in range(0, max_val, batch):
                yield (batch_start, min(batch_start + batch, max_val), length, None)

    try:
        with multiprocessing.Pool(
            processes=cpu_count,
            initializer=_init_crypto_worker,
            initargs=(params,),
        ) as pool:
            for result in pool.imap_unordered(_worker_crypto_task, task_gen(), chunksize=1):
                if ui_stop_flag and ui_stop_flag.is_set():
                    pool.terminate()
                    return None
                if result:
                    pool.terminate()
                    return result
    except Exception as e:
        # Any failure in the fast path (e.g. missing AES backend for R6) should
        # not break recovery — fall back to the pdfium path instead.
        logger.warning("Crypto fast path failed, falling back: %s", e)
        return _CRYPTO_UNSUPPORTED
    return None


def _bruteforce_numeric(pdf_path: str, max_len: int = 4, chunk_size: int = 500000, ui_stop_flag: threading.Event | None = None) -> str | None:
    if ui_stop_flag and ui_stop_flag.is_set():
        return None

    # Hurtigt tjek om den er åben uden password. pdfium er ikke trådsikkert, og
    # dette kald kan overlappe med thumbnail-rendering, så del samme lås.
    try:
        from .pdf_renderer import PDFIUM_LOCK
        with PDFIUM_LOCK:
            pdfium.PdfDocument(pdf_path, password="")
        return ""
    except Exception:
        pass

    # Read PDF data once
    try:
        with open(pdf_path, "rb") as f:
            pdf_data = f.read()
    except Exception:
        return None

    cpu_count = multiprocessing.cpu_count()

    # Fast path: verify candidates directly against the encryption hash without
    # re-parsing the PDF for every attempt. Falls back to the pdfium path below
    # when the file uses an unsupported or non-standard security handler.
    crypto_result = _bruteforce_numeric_crypto(
        pdf_data, max_len, cpu_count, ui_stop_flag
    )
    if crypto_result is not _CRYPTO_UNSUPPORTED:
        return crypto_result

    # Fallback: open the PDF with pdfium once per candidate across worker
    # processes. Only reached for security handlers the crypto path can't verify.
    def py_task_gen():
        for length in range(1, max_len + 1):
            max_val = 10 ** length
            for batch_start in range(0, max_val, 5000):
                batch_end = min(batch_start + 5000, max_val)
                yield (batch_start, batch_end, length, None)

    with multiprocessing.Pool(processes=cpu_count, initializer=_init_worker, initargs=(pdf_path,)) as pool:
        task_iterator = py_task_gen()
        for result in pool.imap_unordered(_worker_task, task_iterator, chunksize=1):
            if ui_stop_flag and ui_stop_flag.is_set():
                pool.terminate()
                return None
            if result:
                pool.terminate()
                return result

    return None


# Generic entry point
def guess_password(pdf_path: str, max_len: int = 5, ui_stop_flag: threading.Event | None = None) -> str | None:
    return _bruteforce_numeric(pdf_path, max_len, ui_stop_flag=ui_stop_flag)
