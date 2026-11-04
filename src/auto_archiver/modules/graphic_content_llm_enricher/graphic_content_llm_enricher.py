"""Rates how distressing each image, and each video's first frame, is (1 to 5) with a local LLM through Ollama.

The prompt and scale come from the hackathon project's classify_image.py.
"""

import base64
import hashlib
import io
import json
import os
import platform
import re
import time
import urllib.request

import ffmpeg
from PIL import Image

from auto_archiver.core import Enricher, Metadata
from auto_archiver.utils.custom_logger import logger
from auto_archiver.utils.misc import random_str

NEEDS_REVIEW = "needs review"
BLUR_FROM = 3  # ratings the HTML formatter blurs; their descriptions stay out of the summary
# a CPU takes a few minutes per image, and loading the model first can take longer. Ollama sends nothing until done
TIMEOUT_SECONDS = 3600

RATE_PROMPT = """You help protect people who review archived images from unexpected distressing content.
Rate how distressing this image is to look at, using this scale:

1: No graphic content. Safe to view.
2: Upsetting context only, with nothing graphic visible, e.g. damage, grief, covered stretchers.
3: Death or injury is clear but the body is hidden, e.g. a body bag or covered body.
4: Injury or a body is visible but limited, e.g. blurred, distant or partly hidden.
5: Truly disturbing, clear pictures of human bodies. Horrible to see.

Rate what is actually visible. Blurring lowers the rating. If you are unsure between two
ratings, choose the higher one.

Reply with exactly two lines:
RATING: a number from 1 to 5
REASON: one short sentence describing what you see"""

SUMMARY_PROMPT = """Below are notes on the images (and first frames of videos) in one archived post, each with a
distress rating from 1 (safe) to 5 (truly disturbing). Summarise what they show in one short paragraph.

Describe the content directly, eg "A man in a volunteer vest stands by an archway, and a group of people load dogs
into a van." Don't write about the post, the items, their ratings, whether they are safe, or reviewing them.

If any notes just say "distressing", end with one sentence saying how many images or videos are distressing, eg
"Two videos are distressing." Never describe what they show: do not mention bodies, injury, blood, death, violence,
weapons, children or anything else upsetting. Reply with the paragraph only.

"""


class GraphicContentLlmEnricher(Enricher):
    def setup(self) -> None:
        # the dev machine (WSL2, eg kernel 5.15.167.4-microsoft-standard-WSL2) reaches the LLM on another address
        if self.ollama_url_dev and "microsoft" in platform.release().lower():
            self.ollama_url = self.ollama_url_dev
        logger.info(f"Graphic content LLM at {self.ollama_url}")

    def enrich(self, to_enrich: Metadata) -> None:
        media = [m for m in to_enrich.media if (m.is_image() or m.is_video()) and "screenshot" not in m.get("id", "")]
        # a video is rated on its first frame, which the browser shows before it plays. Videos go first as they are
        # few and usually the post itself, then images largest first, so icons don't use up the time limit
        videos = [m for m in media if m.is_video() and has_video_stream(m.filename)]
        sized = [(image_size(m.filename), m) for m in media if m.is_image()]
        sized = [(size, m) for size, m in sized if size and min(size) >= self.min_side]
        images = videos + [m for _, m in sorted(sized, key=lambda s: s[0][0] * s[0][1], reverse=True)]
        if skipped := len(media) - len(images):
            logger.info(f"Not rating {skipped} file(s): audio-only, unreadable or under {self.min_side} px")
        # the same file can be in the media list more than once (eg 38 of 176 on an Instagram profile), so rate each
        # once and copy its rating to the copies. Hashed here, as the hash_enricher runs before the WACZ extraction
        all_rated = images
        by_hash = {}
        for m in images:
            by_hash.setdefault(file_sha256(m.filename), []).append(m)
        images = [same[0] for same in by_hash.values()]
        if copies := len(all_rated) - len(images):
            logger.info(f"Not rating {copies} duplicate file(s)")
        if self.max_images:
            images = images[: self.max_images]
        if not images:
            return

        # keeps the row under the orchestrator's 30 minute hang dump (and well under its 3 hour kill)
        deadline = time.monotonic() + self.max_minutes * 60
        lines = []
        for i, m in enumerate(images, 1):
            kind = "video first frame" if m.is_video() else "image"
            start = time.monotonic()
            if start > deadline:
                logger.warning(f"Over {self.max_minutes} minutes, not rating {m.filename}")
                rating, reason = None, "not rated, over the time limit"
            else:
                try:
                    filename = self.first_frame(m.filename) if m.is_video() else m.filename
                    image, original, sent = self.image_base64(filename)
                    # "1 of 8" counts every item to rate, images and videos together
                    logger.info(
                        f"Rating {i} of {len(images)} ({kind}, {original[0]}x{original[1]} sent as "
                        f"{sent[0]}x{sent[1]}): {m.filename}"
                    )
                    rating, reason = parse(self.ask(RATE_PROMPT, image))
                except Exception as e:
                    logger.warning(f"Could not rate {m.filename}: {e}")
                    rating, reason = None, ""
            m.set("graphic_rating", rating or NEEDS_REVIEW)
            m.set("graphic_description", reason)
            m.set("graphic_seconds", round(time.monotonic() - start))
            # the rating only: a description can be distressing
            logger.info(f"Rated {i} of {len(images)} ({kind}): {rating or NEEDS_REVIEW} ({m.get('graphic_seconds')} s)")
            # the summary goes in the sheet, which can't be blurred, so a distressing item's description stays out
            if rating and rating < BLUR_FROM:
                note = reason or "no description"
            else:
                note = "distressing" if rating else "not checked"
            lines.append(f"{i}. {kind}, rating {rating or NEEDS_REVIEW}: {note}")

        for same in by_hash.values():
            for copy in same[1:]:
                if same[0].get("graphic_rating") is not None:  # not when max_images left it out
                    copy.set("graphic_rating", same[0].get("graphic_rating"))
                    copy.set("graphic_description", same[0].get("graphic_description"))
                    copy.set("graphic_seconds", 0)

        # for the sheet: the highest rating found, and how many items weren't checked (the true highest is unknown)
        ratings = [m.get("graphic_rating") for m in all_rated if m.get("graphic_rating") is not None]
        rated = [r for r in ratings if r != NEEDS_REVIEW]
        not_checked = len(ratings) - len(rated)
        if not rated:
            to_enrich.set("graphic_rating", NEEDS_REVIEW)
        elif not_checked:
            to_enrich.set("graphic_rating", f"{max(rated)} ({not_checked} not checked)")
        else:
            to_enrich.set("graphic_rating", max(rated))
        # the per-image lines stay as the summary if the summary call fails
        to_enrich.set("images_summary", "\n".join(lines))
        logger.info("Summarising images")
        start = time.monotonic()
        try:
            to_enrich.set("images_summary", self.ask(SUMMARY_PROMPT + "\n".join(lines)).strip())
            logger.info(f"Summarised images ({round(time.monotonic() - start)} s)")
        except Exception as e:
            logger.warning(f"Could not summarise images: {e}")

    def first_frame(self, filename: str) -> str:
        """The video's first frame saved as a jpg"""
        path = os.path.join(self.tmp_dir, f"first_frame_{random_str(12)}.jpg")
        ffmpeg.input(filename).output(path, vframes=1, loglevel="error").run(capture_stdout=True, capture_stderr=True)
        return path

    def image_base64(self, filename: str) -> tuple:
        """(the image scaled down to long_side pixels (never up) as base64 JPEG, original size, sent size)"""
        with Image.open(filename) as img:
            original = img.size
            img = img.convert("RGB")
            img.thumbnail((self.long_side, self.long_side))
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=90)
            return base64.b64encode(buffer.getvalue()).decode(), original, img.size

    def ask(self, prompt: str, image: str = None) -> str:
        body = {
            "model": self.model,
            "prompt": prompt,
            "images": [image] if image else [],
            "stream": False,
            "options": {"num_ctx": 32768, "num_predict": 6000, "temperature": 0, "num_thread": self.threads},
        }
        if self.think:
            body["think"] = self.think
        request = urllib.request.Request(
            f"{self.ollama_url}/api/generate",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)["response"]


def file_sha256(filename: str) -> str:
    with open(filename, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def image_size(filename: str) -> tuple | None:
    """(width, height), or None if Pillow can't read it (eg an SVG)"""
    try:
        with Image.open(filename) as img:
            return img.size
    except Exception:
        return None


def has_video_stream(filename: str) -> bool:
    """False for an audio-only video file, or one ffmpeg can't read, as there is no picture to show"""
    try:
        return any(s["codec_type"] == "video" for s in ffmpeg.probe(filename)["streams"])
    except Exception:
        return False


def parse(response: str) -> tuple:
    """(rating as int or None, reason) from the model's two-line reply"""
    rating = re.search(r"^\s*RATING:\s*([1-5])\s*$", response, re.MULTILINE)
    reason = re.search(r"^\s*REASON:\s*(.+)$", response, re.MULTILINE)
    return (int(rating.group(1)) if rating else None, reason.group(1).strip() if reason else "")
