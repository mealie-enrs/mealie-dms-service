"""
Risk scoring — assigns a 0.0–1.0 risk score to user uploads.

Replaces fully manual approval with automatic pre-scoring so human
reviewers can prioritize high-risk uploads.

Score components:
  - User history     (past rejection rate from Redis feature store)
  - Engagement burst (recent upload volume from Redis)
  - Metadata signals (mime type, file size, filename)

Thresholds:
  score < 0.3  → low risk  → auto-approve candidate
  score < 0.7  → medium    → queue for human review
  score ≥ 0.7  → high risk → flag for priority review

The scorer is called as a Celery task triggered immediately after
upload initiation (POST /uploads/init).
"""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/2")

RISK_LOW    = 0.3
RISK_HIGH   = 0.7


def _redis_client():
    import redis
    return redis.from_url(REDIS_URL, decode_responses=True)


def _safe_zcard(r, key: str) -> int:
    try:
        now = time.time()
        r.zremrangebyscore(key, "-inf", now - 300)  # 5-min window
        return r.zcard(key) or 0
    except Exception:
        return 0


def _safe_int(r, key: str) -> int:
    try:
        val = r.get(key)
        return int(val) if val else 0
    except Exception:
        return 0


def compute_risk_score(
    user_id: str,
    upload_id: int,
    mime_type: str | None = None,
    filename: str | None = None,
    file_size_bytes: int | None = None,
) -> float:
    """
    Compute a risk score 0.0–1.0 for an upload.

    Higher = more likely to need human review.
    """
    score = 0.0

    try:
        r = _redis_client()

        # ── User history signals ───────────────────────────────────────────
        total_uploads    = _safe_int(r, f"user:{user_id}:total_uploads")
        total_rejections = _safe_int(r, f"user:{user_id}:total_rejections")

        if total_uploads > 0:
            rejection_rate = total_rejections / total_uploads
            # High rejection history → higher risk
            score += min(rejection_rate * 0.5, 0.4)

        # ── Burst detection ────────────────────────────────────────────────
        uploads_5min = _safe_zcard(r, f"user:{user_id}:uploads:5min")
        if uploads_5min > 10:
            # More than 10 uploads in 5 minutes → likely bot or spam
            score += 0.3
        elif uploads_5min > 5:
            score += 0.1

        # ── Metadata signals ───────────────────────────────────────────────
        if mime_type and not mime_type.startswith("image/"):
            # Non-image MIME type on an image upload endpoint → suspicious
            score += 0.2

        if file_size_bytes is not None:
            if file_size_bytes < 1024:
                # Suspiciously small (likely corrupt or placeholder)
                score += 0.1
            elif file_size_bytes > 50 * 1024 * 1024:
                # Very large file
                score += 0.1

        if filename:
            fn = filename.lower()
            suspicious_extensions = {".exe", ".sh", ".js", ".php", ".py", ".bat"}
            if any(fn.endswith(ext) for ext in suspicious_extensions):
                score += 0.5

    except Exception as exc:
        log.warning("Risk scoring failed for upload_id=%s: %s", upload_id, exc)
        score = 0.1  # default low risk on error

    final = min(round(score, 3), 1.0)
    log.info(
        "Risk score computed: upload_id=%s user_id=%s score=%.3f",
        upload_id, user_id, final,
    )
    return final


def risk_label(score: float) -> str:
    if score < RISK_LOW:
        return "low"
    elif score < RISK_HIGH:
        return "medium"
    return "high"
