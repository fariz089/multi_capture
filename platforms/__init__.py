"""Platform-specific login capture handlers."""
from .base import LoginCapture, CaptureResult
from .facebook import FacebookCapture
from .tiktok import TikTokCapture
from .instagram import InstagramCapture
from .twitter import TwitterCapture
from .threads import ThreadsCapture

PLATFORMS = {
    "facebook":  FacebookCapture,
    "tiktok":    TikTokCapture,
    "instagram": InstagramCapture,
    "twitter":   TwitterCapture,
    "threads":   ThreadsCapture,
}

__all__ = ["LoginCapture", "CaptureResult", "PLATFORMS"]
