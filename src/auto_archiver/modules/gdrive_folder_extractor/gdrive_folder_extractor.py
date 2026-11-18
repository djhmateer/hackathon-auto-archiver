import hashlib
import mimetypes
import os
import re
import time

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from auto_archiver.core import Extractor, Media, Metadata
from auto_archiver.utils.custom_logger import logger

# execute(num_retries=...) retries only transient failures (5xx, 429, rate limits) with exponential backoff,
# as in gdrive_storage
NUM_RETRIES = 7

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"

# Google-native files (Docs, Sheets...) have no bytes of their own, so they are exported: mimetype, extension
EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("image/svg+xml", ".svg"),
}

# Drive fields not worth keeping: UI details, a big dict of what we may do with the file, and links that expire
SKIP_FIELDS = {"kind", "capabilities", "iconLink", "thumbnailLink", "hasThumbnail", "thumbnailVersion", "exportLinks"}


class GDriveFolderExtractor(Extractor):
    # folders only: single files (/file/d/...) are left to yt-dlp
    valid_url = re.compile(r"https?://drive\.google\.com/drive/(?:u/\d+/)?folders/([\w-]+)")

    def setup(self) -> None:
        self.creds = service_account.Credentials.from_service_account_file(
            self.service_account, scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        self.service = build("drive", "v3", credentials=self.creds, cache_discovery=False)

    def download(self, item: Metadata) -> Metadata:
        match = self.match_link(item.get_url())
        if not match:
            return False

        started = time.monotonic()
        root = self._get(match.group(1))
        logger.info(f"Walking Drive folder '{root['name']}' ({root['id']})")
        result = Metadata()
        result.set_title(root["name"])
        result.set("drive_folder", self._properties(root))

        not_downloaded = []
        files = total_bytes = 0
        by_sha256 = {}  # sha256 -> the Media kept for that content
        # every item listed ends up in exactly one of these, so they add up to what Drive has
        counts = {"folders": 0, "archived": 0, "same_content": 0, "already_seen": 0, "not_downloaded": 0}
        seen = {root["id"]}
        to_walk = [(root["id"], root["name"] + "/")]
        while to_walk:
            folder_id, folder_path = to_walk.pop()
            children = self._list_children(folder_id)
            logger.debug(f"Listed {len(children)} items in {folder_path}")
            for f in children:
                path = folder_path + f["name"]
                try:
                    if f["mimeType"] == SHORTCUT:
                        f = self._get(f["shortcutDetails"]["targetId"]) | {"drive_shortcut_path": path}
                    if f["id"] in seen:
                        # a file in more than one folder, or a shortcut to something already archived
                        logger.debug(f"Already seen {path} ({f['id']})")
                        counts["already_seen"] += 1
                        continue
                    seen.add(f["id"])

                    if f["mimeType"] == FOLDER:
                        counts["folders"] += 1
                        to_walk.append((f["id"], path + "/"))
                        continue

                    if files >= self.max_files or total_bytes + int(f.get("size", 0)) > self.max_bytes:
                        logger.warning(f"Stopped at {files} files, {total_bytes} bytes: over max_files or max_bytes")
                        item.add_status_note("partial: too many files")
                        result.set("drive_partial", f"stopped at {files} files, {total_bytes} bytes")
                        to_walk = []
                        break

                    if f["mimeType"].startswith("application/vnd.google-apps.") and f["mimeType"] not in EXPORTS:
                        not_downloaded.append(self._not_downloaded(f, path, "this Google file type can't be exported"))
                        continue

                    file_started = time.monotonic()
                    filename, mimetype = self._download_file(f)
                except (HttpError, requests.RequestException) as e:
                    logger.warning(f"Could not download {path}: {e}")
                    not_downloaded.append(self._not_downloaded(f, path, str(e)))
                    continue

                files += 1
                size = os.path.getsize(filename)
                total_bytes += size
                logger.debug(
                    f"Downloaded {files}: {path} ({size / 1024 / 1024:.1f} MB) in {time.monotonic() - file_started:.1f}s, "
                    f"{total_bytes / 1024 / 1024:.0f} MB so far"
                )

                sha256 = self._sha256(filename)
                properties = {"drive_path": path} | self._properties(f)
                if "sha256Checksum" in f:
                    properties["drive_sha256_matches"] = sha256 == f["sha256Checksum"]

                # Metadata.store() silently drops media with the same hash, so keep a duplicate's Drive details on the
                # first copy rather than losing them
                if first := by_sha256.get(sha256):
                    logger.debug(f"{path} has the same content as {first.get('drive_path')}")
                    first.properties.setdefault("drive_duplicates", []).append(properties)
                    os.remove(filename)
                    counts["same_content"] += 1
                    continue

                media = Media(filename)
                media.mimetype = mimetype
                for k, v in properties.items():
                    media.set(k, v)
                result.add_media(media)
                by_sha256[sha256] = media
                counts["archived"] += 1

        counts["not_downloaded"] = len(not_downloaded)
        logger.info(
            f"Downloaded {files} files, {total_bytes / 1024 / 1024:.0f} MB in {time.monotonic() - started:.0f}s: {counts}"
        )
        result.set("drive_counts", counts)
        if not_downloaded:
            result.set("drive_not_downloaded", not_downloaded)
        return result.success("gdrive-folder")

    def _get(self, file_id: str) -> dict:
        return (
            self.service.files()
            .get(fileId=file_id, fields="*", supportsAllDrives=True)
            .execute(num_retries=NUM_RETRIES)
        )

    def _list_children(self, folder_id: str) -> list[dict]:
        children, page_token = [], None
        while True:
            page = (
                self.service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(*)",
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute(num_retries=NUM_RETRIES)
            )
            children += page.get("files", [])
            page_token = page.get("nextPageToken")
            if not page_token:
                return children

    def _download_file(self, f: dict) -> tuple[str, str]:
        """Saves the file in tmp_dir, named by its Drive id. Returns the filename and the mimetype it was saved as."""
        if f["mimeType"] in EXPORTS:
            mimetype, ext = EXPORTS[f["mimeType"]]
            filename = os.path.join(self.tmp_dir, f["id"] + ext)
            try:
                self._save(self.service.files().export_media(fileId=f["id"], mimeType=mimetype), filename)
            except HttpError as e:
                # export_media is limited to 10 MB of exported content, the file's exportLinks aren't
                if b"exportSizeLimitExceeded" not in (e.content or b"") or mimetype not in f.get("exportLinks", {}):
                    raise
                logger.info(f"{f['name']} is too big for export_media, using its export link")
                self._save_url(f["exportLinks"][mimetype], filename)
            return filename, mimetype

        ext = os.path.splitext(f["name"])[1] or mimetypes.guess_extension(f["mimeType"]) or ""
        filename = os.path.join(self.tmp_dir, f["id"] + ext)
        self._save(self.service.files().get_media(fileId=f["id"], supportsAllDrives=True), filename)
        return filename, f["mimeType"]

    def _save(self, request, filename: str) -> None:
        with open(filename, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=NUM_RETRIES)

    def _save_url(self, url: str, filename: str) -> None:
        with AuthorizedSession(self.creds).get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(filename, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    fh.write(chunk)

    def _sha256(self, filename: str) -> str:
        h = hashlib.sha256()
        with open(filename, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _properties(self, f: dict) -> dict:
        """Drive's fields, prefixed with drive_ so they don't clash with properties other modules set (eg id)"""
        return {k if k.startswith("drive_") else f"drive_{k}": v for k, v in f.items() if k not in SKIP_FIELDS}

    def _not_downloaded(self, f: dict, path: str, reason: str) -> dict:
        return {
            "drive_path": path,
            "drive_id": f.get("id"),
            "drive_mimeType": f.get("mimeType"),
            "drive_webViewLink": f.get("webViewLink"),
            "reason": reason,
        }
