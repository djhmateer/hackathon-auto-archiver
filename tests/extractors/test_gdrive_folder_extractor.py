import hashlib

import httplib2
import pytest
from googleapiclient.errors import HttpError

from auto_archiver.core import Metadata

MODULE = "auto_archiver.modules.gdrive_folder_extractor.gdrive_folder_extractor"
ROOT_URL = "https://drive.google.com/drive/folders/root1"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def http_error(status: int, content: bytes = b"{}") -> HttpError:
    return HttpError(httplib2.Response({"status": status}), content)


class FakeRequest:
    def __init__(self, value):
        self.value = value

    def execute(self, num_retries=0):
        return self.value


class FakeFiles:
    """files() of the Drive service, over a dict of folder id -> children. Lists in pages of 2 to test paging."""

    def __init__(self, files: dict, children: dict):
        self.files, self.children = files, children

    def get(self, fileId, **kwargs):
        return FakeRequest(self.files[fileId])

    def list(self, q, pageToken=None, **kwargs):
        children = self.children.get(q.split("'")[1], [])
        start = int(pageToken or 0)
        page = {"files": children[start : start + 2]}
        if start + 2 < len(children):
            page["nextPageToken"] = str(start + 2)
        return FakeRequest(page)

    def export_media(self, fileId, mimeType):
        return ("export", fileId, mimeType)

    def get_media(self, fileId, **kwargs):
        return ("media", fileId)


class FakeService:
    def __init__(self, files, children):
        self._files = FakeFiles(files, children)

    def files(self):
        return self._files


def drive_file(id, name, mimeType, **extra):
    return {"id": id, "name": name, "mimeType": mimeType, "webViewLink": f"https://drive.google.com/{id}", **extra}


@pytest.fixture
def make_extractor(setup_module, mocker):
    """Returns a function making the extractor over a fake Drive: files by id, children by folder id, and the
    bytes each download gives (or an exception to raise)"""

    def _make(files: dict, children: dict, contents: dict = None, config: dict = None):
        mocker.patch(f"{MODULE}.service_account.Credentials.from_service_account_file")
        mocker.patch(f"{MODULE}.build", return_value=FakeService(files, children))
        extractor = setup_module("gdrive_folder_extractor", {"service_account": "sa.json", **(config or {})})
        extractor.saved_requests = []

        def fake_save(request, filename):
            extractor.saved_requests.append(request)
            # different content per file unless a test says otherwise, as same-content files are merged
            content = (contents or {}).get(request[1], f"data {request[1]}".encode())
            if isinstance(content, Exception):
                raise content
            with open(filename, "wb") as fh:
                fh.write(content)

        mocker.patch.object(extractor, "_save", side_effect=fake_save)
        return extractor

    return _make


def item(url=ROOT_URL) -> Metadata:
    return Metadata().set_url(url)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://drive.google.com/drive/folders/1AbC-d_9", "1AbC-d_9"),
        ("https://drive.google.com/drive/folders/1AbC-d_9?usp=sharing", "1AbC-d_9"),
        ("https://drive.google.com/drive/u/0/folders/1AbC-d_9", "1AbC-d_9"),
        ("https://drive.google.com/file/d/1AbC-d_9/view", None),
        ("https://drive.google.com/drive/my-drive", None),
        ("https://drive.google.com/open?id=1AbC-d_9", None),
        ("https://docs.google.com/document/d/1AbC-d_9/edit", None),
    ],
)
def test_url_matching(make_extractor, url, expected):
    extractor = make_extractor({}, {})
    match = extractor.valid_url.match(url)
    assert (match.group(1) if match else None) == expected


def test_walks_nested_folders(make_extractor):
    files = {
        "root1": drive_file("root1", "Evidence", "application/vnd.google-apps.folder"),
        "sub1": drive_file("sub1", "Sub", "application/vnd.google-apps.folder"),
    }
    children = {
        "root1": [
            drive_file("img1", "photo.jpg", "image/jpeg", size="4"),
            drive_file("doc1", "Notes", "application/vnd.google-apps.document"),
            files["sub1"],
            drive_file("form1", "Survey", "application/vnd.google-apps.form"),
        ],
        "sub1": [
            drive_file("vid1", "clip.mp4", "video/mp4", size="4"),
            # a shortcut back to the parent folder mustn't loop forever
            drive_file("sc1", "back", "application/vnd.google-apps.shortcut", shortcutDetails={"targetId": "root1"}),
        ],
    }
    extractor = make_extractor(files, children)

    result = extractor.download(item())

    assert result.is_success()
    assert result.get_title() == "Evidence"
    assert result.get("drive_folder")["drive_id"] == "root1"
    by_path = {m.get("drive_path"): m for m in result.media}
    assert set(by_path) == {"Evidence/photo.jpg", "Evidence/Notes", "Evidence/Sub/clip.mp4"}
    assert by_path["Evidence/photo.jpg"].mimetype == "image/jpeg"
    assert by_path["Evidence/photo.jpg"].filename.endswith("img1.jpg")
    assert by_path["Evidence/photo.jpg"].get("drive_webViewLink") == "https://drive.google.com/img1"
    assert by_path["Evidence/Notes"].mimetype == DOCX
    assert by_path["Evidence/Notes"].filename.endswith("doc1.docx")
    assert ("export", "doc1", DOCX) in extractor.saved_requests
    assert [n["drive_path"] for n in result.get("drive_not_downloaded")] == ["Evidence/Survey"]
    assert result.get("drive_counts") == {
        "folders": 1,
        "archived": 3,
        "same_content": 0,
        "already_seen": 1,  # the shortcut back to the root
        "not_downloaded": 1,
    }


def test_same_content_kept_once_with_every_path(make_extractor):
    files = {"root1": drive_file("root1", "Evidence", "application/vnd.google-apps.folder")}
    children = {
        "root1": [
            drive_file("a", "a.jpg", "image/jpeg"),
            drive_file("b", "copy of a.jpg", "image/jpeg"),
            drive_file("c", "c.jpg", "image/jpeg"),
        ]
    }
    extractor = make_extractor(files, children, contents={"a": b"same", "b": b"same"})

    result = extractor.download(item())

    assert [m.get("drive_id") for m in result.media] == ["a", "c"]
    duplicates = result.media[0].get("drive_duplicates")
    assert [(d["drive_id"], d["drive_path"]) for d in duplicates] == [("b", "Evidence/copy of a.jpg")]
    assert result.get("drive_counts")["same_content"] == 1


def test_follows_shortcut_to_file(make_extractor):
    target = drive_file("img1", "photo.jpg", "image/jpeg")
    files = {"root1": drive_file("root1", "Evidence", "application/vnd.google-apps.folder"), "img1": target}
    children = {
        "root1": [
            drive_file("sc1", "link", "application/vnd.google-apps.shortcut", shortcutDetails={"targetId": "img1"})
        ]
    }

    result = make_extractor(files, children).download(item())

    assert len(result.media) == 1
    assert result.media[0].get("drive_id") == "img1"
    assert result.media[0].get("drive_shortcut_path") == "Evidence/link"


def test_checks_drive_sha256(make_extractor):
    good = hashlib.sha256(b"data a").hexdigest()
    files = {"root1": drive_file("root1", "Evidence", "application/vnd.google-apps.folder")}
    children = {
        "root1": [
            drive_file("a", "a.jpg", "image/jpeg", sha256Checksum=good),
            drive_file("b", "b.jpg", "image/jpeg", sha256Checksum="0" * 64),
            drive_file("c", "c.jpg", "image/jpeg"),
        ]
    }

    result = make_extractor(files, children).download(item())

    matches = {m.get("drive_id"): m.get("drive_sha256_matches") for m in result.media}
    assert matches == {"a": True, "b": False, "c": None}


def test_one_failed_file_does_not_stop_the_rest(make_extractor):
    files = {"root1": drive_file("root1", "Evidence", "application/vnd.google-apps.folder")}
    children = {"root1": [drive_file("a", "a.jpg", "image/jpeg"), drive_file("b", "b.jpg", "image/jpeg")]}
    extractor = make_extractor(files, children, contents={"a": http_error(403)})

    result = extractor.download(item())

    assert [m.get("drive_id") for m in result.media] == ["b"]
    assert result.get("drive_not_downloaded")[0]["drive_id"] == "a"


def test_big_export_uses_export_link(make_extractor, mocker):
    sheet = drive_file(
        "s1",
        "Big",
        "application/vnd.google-apps.spreadsheet",
        exportLinks={"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "https://export/s1"},
    )
    files = {"root1": drive_file("root1", "Evidence", "application/vnd.google-apps.folder")}
    extractor = make_extractor(
        files, {"root1": [sheet]}, contents={"s1": http_error(403, b'{"reason": "exportSizeLimitExceeded"}')}
    )
    save_url = mocker.patch.object(extractor, "_save_url", side_effect=lambda url, fn: open(fn, "wb").close())

    result = extractor.download(item())

    save_url.assert_called_once()
    assert save_url.call_args.args[0] == "https://export/s1"
    assert len(result.media) == 1


def test_stops_at_max_files(make_extractor):
    files = {"root1": drive_file("root1", "Evidence", "application/vnd.google-apps.folder")}
    children = {"root1": [drive_file(str(i), f"{i}.jpg", "image/jpeg") for i in range(5)]}
    the_item = item()

    result = make_extractor(files, children, config={"max_files": 3}).download(the_item)

    assert result.is_success()
    assert len(result.media) == 3
    assert "stopped at 3 files" in result.get("drive_partial")
    assert the_item.get_status_notes() == ["partial: too many files"]


def test_stops_before_going_over_max_bytes(make_extractor):
    files = {"root1": drive_file("root1", "Evidence", "application/vnd.google-apps.folder")}
    children = {
        "root1": [drive_file("a", "a.jpg", "image/jpeg", size="4"), drive_file("b", "b.mp4", "video/mp4", size="100")]
    }

    result = make_extractor(files, children, config={"max_bytes": 50}).download(item())

    assert [m.get("drive_id") for m in result.media] == ["a"]
    assert result.get("drive_partial")


def test_ignores_other_urls(make_extractor):
    assert make_extractor({}, {}).download(item("https://drive.google.com/file/d/abc/view")) is False
