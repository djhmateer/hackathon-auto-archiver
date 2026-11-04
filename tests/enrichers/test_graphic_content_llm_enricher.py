import ffmpeg
import pytest
from PIL import Image

from auto_archiver.core import Media, Metadata
from auto_archiver.modules.graphic_content_llm_enricher.graphic_content_llm_enricher import NEEDS_REVIEW, parse
from auto_archiver.modules.html_formatter import HtmlFormatter


@pytest.fixture
def enricher(setup_module):
    return setup_module("graphic_content_llm_enricher", {})


def make_item(tmp_path, n_images):
    item = Metadata().set_url("https://example.com/post")
    for i in range(n_images):
        path = tmp_path / f"{i}.jpg"
        Image.new("RGB", (3000, 2000), (i * 50, 0, 0)).save(path)  # different, as duplicates are rated once
        item.add_media(Media(filename=str(path), urls=[f"https://example.com/{i}.jpg"]), id=f"image{i}")
    return item


def add_video(item, path, audio_only=False):
    source = "anullsrc" if audio_only else "color=c=red:s=64x48"
    ffmpeg.input(source, f="lavfi", t=1).output(str(path), loglevel="error").run()
    item.add_media(Media(filename=str(path), urls=[f"https://example.com/{path.name}"]), id=path.stem)


def test_parse():
    assert parse("RATING: 4\nREASON: A covered body.") == (4, "A covered body.")
    assert parse("I can't tell") == (None, "")


def test_rates_each_image_and_summarises(enricher, tmp_path, mocker):
    item = make_item(tmp_path, 2)
    screenshot = tmp_path / "s.png"
    Image.new("RGB", (10, 10)).save(screenshot)
    item.add_media(Media(filename=str(screenshot)), id="screenshot")
    ask = mocker.patch.object(
        enricher,
        "ask",
        side_effect=["RATING: 1\nREASON: A street.", "RATING: 4\nREASON: A body on a road.", " The summary. "],
    )

    enricher.enrich(item)

    assert [m.get("graphic_rating") for m in item.media[:2]] == [1, 4]
    assert item.media[1].get("graphic_description") == "A body on a road."
    assert item.media[1].get("graphic_seconds") == 0
    assert item.media[2].get("graphic_rating") is None  # screenshot skipped
    assert item.get("graphic_rating") == 4
    assert item.get("images_summary") == "The summary."
    assert ask.call_count == 3
    assert "1. image, rating 1: A street.\n2. image, rating 4: distressing" in ask.call_args_list[2].args[0]
    assert "A body on a road." not in ask.call_args_list[2].args[0]  # kept out of the sheet


def test_max_images(setup_module, tmp_path, mocker):
    enricher = setup_module("graphic_content_llm_enricher", {"max_images": 1})
    item = make_item(tmp_path, 3)
    ask = mocker.patch.object(enricher, "ask", side_effect=["RATING: 3\nREASON: A body bag.", "The summary."])

    enricher.enrich(item)

    assert [m.get("graphic_rating") for m in item.media] == [3, None, None]
    assert item.get("graphic_rating") == 3
    assert ask.call_count == 2


def test_image_is_scaled_down(enricher, tmp_path):
    import base64
    import io

    item = make_item(tmp_path, 1)
    image, original, sent = enricher.image_base64(item.media[0].filename)
    assert (original, sent) == ((3000, 2000), (1366, 911))
    data = base64.b64decode(image)
    assert Image.open(io.BytesIO(data)).size == (1366, 911)


def test_unrated_image_needs_review(enricher, tmp_path, mocker):
    item = make_item(tmp_path, 2)
    mocker.patch.object(enricher, "ask", side_effect=["RATING: 2\nREASON: Grief.", OSError("down"), OSError("down")])

    enricher.enrich(item)

    assert item.media[1].get("graphic_rating") == NEEDS_REVIEW
    assert item.get("graphic_rating") == "2 (1 not checked)"
    assert item.get("images_summary") == "1. image, rating 2: Grief.\n2. image, rating needs review: not checked"


def test_html_blurs_rated_3_and_above(setup_module, tmp_path):
    item = make_item(tmp_path, 4)
    for m, rating in zip(item.media, [1, 2, 3, NEEDS_REVIEW]):
        m.set("graphic_rating", rating)
    formatter = setup_module(HtmlFormatter)

    with open(formatter.format(item).filename, encoding="utf-8") as f:
        html = f.read()

    assert html.count('class="graphic-hidden"') == 2
    assert "Graphic content (rating 3)" in html
    assert "Graphic content (rating needs review)" in html


def test_about_column_ends_with_description_rating_and_seconds(setup_module, tmp_path):
    item = make_item(tmp_path, 1)
    m = item.media[0]
    m.set("graphic_rating", 1).set("graphic_description", "A timer.").set("graphic_seconds", 172)
    m.set("bytes", 44900)
    formatter = setup_module(HtmlFormatter)

    with open(formatter.format(item).filename, encoding="utf-8") as f:
        html = f.read()

    assert (
        html.index("<b>bytes:</b>")
        < html.index("<b>graphic_description:</b>")
        < html.index("<b>graphic_rating:</b>")
        < html.index("<b>graphic_seconds:</b>")
    )
    assert '<span class="copy">1</span>' in html


def test_about_column_has_no_graphic_lines_for_unrated_media(setup_module, tmp_path):
    item = make_item(tmp_path, 1)
    formatter = setup_module(HtmlFormatter)

    with open(formatter.format(item).filename, encoding="utf-8") as f:
        assert "graphic_" not in f.read()


def test_video_is_rated_on_its_first_frame(enricher, tmp_path, mocker):
    item = make_item(tmp_path, 0)
    add_video(item, tmp_path / "v.mp4")
    ask = mocker.patch.object(enricher, "ask", side_effect=["RATING: 4\nREASON: A body by a wall.", "The summary."])

    enricher.enrich(item)

    assert ask.call_args_list[0].args[1]  # the frame was sent
    assert item.media[0].get("graphic_rating") == 4
    assert "1. video first frame, rating 4: distressing" in ask.call_args_list[1].args[0]


def test_first_frame_is_extracted(enricher, tmp_path):
    item = make_item(tmp_path, 0)
    add_video(item, tmp_path / "v.mp4")

    with Image.open(enricher.first_frame(item.media[0].filename)) as frame:
        assert frame.size == (64, 48)


def test_html_blurs_video_rated_3_and_above(setup_module, tmp_path):
    item = make_item(tmp_path, 0)
    video = Media(filename="v.mp4", urls=["https://example.com/v.mp4"], _mimetype="video/mp4")
    video.set("graphic_rating", 4)
    item.add_media(video, id="video")
    formatter = setup_module(HtmlFormatter)

    with open(formatter.format(item).filename, encoding="utf-8") as f:
        html = f.read()

    assert 'class="graphic-hidden"' in html
    assert "Graphic content (first frame rating 4)" in html


def test_images_over_the_time_limit_need_review(setup_module, tmp_path, mocker):
    enricher = setup_module("graphic_content_llm_enricher", {"max_minutes": 20})
    item = make_item(tmp_path, 2)
    # start, rating image 1, then image 2 starts after the 20 minutes, then the summary
    mocker.patch("time.monotonic", side_effect=[0, 10, 100, 1300, 1300, 1300, 1400])
    ask = mocker.patch.object(enricher, "ask", side_effect=["RATING: 1\nREASON: A street.", "The summary."])

    enricher.enrich(item)

    assert item.media[0].get("graphic_rating") == 1
    assert item.media[1].get("graphic_rating") == NEEDS_REVIEW
    assert item.media[1].get("graphic_description") == "not rated, over the time limit"
    assert item.get("graphic_rating") == "1 (1 not checked)"
    assert ask.call_count == 2  # image 1 and the summary


def test_videos_first_then_images_largest_first(enricher, tmp_path, mocker):
    item = make_item(tmp_path, 0)
    for name, size in [("small.jpg", (400, 300)), ("big.jpg", (2000, 1500))]:
        Image.new("RGB", size).save(tmp_path / name)
        item.add_media(Media(filename=str(tmp_path / name)), id=name)
    add_video(item, tmp_path / "v.mp4")
    mocker.patch.object(enricher, "ask", side_effect=["RATING: 1\nREASON: a."] * 3 + ["The summary."])
    rated = mocker.spy(enricher, "image_base64")

    enricher.enrich(item)

    assert rated.call_args_list[1].args[0].endswith("big.jpg")
    assert rated.call_args_list[2].args[0].endswith("small.jpg")
    assert "1. video first frame" in enricher.ask.call_args_list[3].args[0]


def test_small_unreadable_and_audio_only_files_are_skipped(enricher, tmp_path, mocker):
    item = make_item(tmp_path, 1)
    Image.new("RGB", (64, 64)).save(tmp_path / "icon.png")
    item.add_media(Media(filename=str(tmp_path / "icon.png")), id="icon")
    (tmp_path / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    item.add_media(Media(filename=str(tmp_path / "logo.svg")), id="logo")
    add_video(item, tmp_path / "audio.mp4", audio_only=True)
    ask = mocker.patch.object(enricher, "ask", side_effect=["RATING: 2\nREASON: Grief.", "The summary."])

    enricher.enrich(item)

    assert ask.call_count == 2  # only the real image, and the summary
    assert [m.get("graphic_rating") for m in item.media] == [2, None, None, None]
    assert item.get("graphic_rating") == 2


def test_threads_sent_on_every_call(enricher, mocker):
    urlopen = mocker.patch("urllib.request.urlopen")
    urlopen.return_value.__enter__.return_value.read.return_value = b'{"response": "RATING: 1"}'

    enricher.ask("prompt", "aW1hZ2U=")

    body = __import__("json").loads(urlopen.call_args.args[0].data)
    assert body["options"]["num_thread"] == 6
    assert body["keep_alive"] == "30m"


def test_ollama_timings_logged(enricher, mocker):
    urlopen = mocker.patch("urllib.request.urlopen")
    urlopen.return_value.__enter__.return_value.read.return_value = (
        b'{"response": "RATING: 1", "load_duration": 2500000000, "prompt_eval_count": 1200, '
        b'"prompt_eval_duration": 30000000000, "eval_count": 800, "eval_duration": 100000000000}'
    )
    debug = mocker.patch("auto_archiver.modules.graphic_content_llm_enricher.graphic_content_llm_enricher.logger.debug")

    assert enricher.ask("prompt", "aW1hZ2U=") == "RATING: 1"
    debug.assert_called_once_with(
        "Ollama: load 2.5 s, prompt 1200 tokens in 30.0 s, output 800 tokens in 100.0 s (8.0 tokens/s)"
    )


def test_html_blurs_descriptions_of_graphic_images(setup_module, tmp_path):
    item = make_item(tmp_path, 2)
    item.media[0].set("graphic_rating", 2).set("graphic_description", "People grieving.")
    item.media[1].set("graphic_rating", 4).set("graphic_description", "A body by a road.")
    item.set("graphic_rating", 4).set("images_summary", "One image is distressing.")
    formatter = setup_module(HtmlFormatter)

    with open(formatter.format(item).filename, encoding="utf-8") as f:
        html = f.read()

    hidden = html.split('class="graphic-text-hidden"')
    assert len(hidden) == 2  # only the rating 4 description; the summary is non-specific
    assert "A body by a road." in hidden[1]
    assert "People grieving." in hidden[0]


def test_nothing_rated_needs_review(enricher, tmp_path, mocker):
    item = make_item(tmp_path, 2)
    mocker.patch.object(enricher, "ask", side_effect=OSError("down"))

    enricher.enrich(item)

    assert item.get("graphic_rating") == NEEDS_REVIEW


def test_duplicate_files_are_rated_once(enricher, tmp_path, mocker):
    item = make_item(tmp_path, 1)
    # the same file twice in the media list, and a copy of it under another name
    item.add_media(Media(filename=item.media[0].filename), id="again")
    (tmp_path / "copy.jpg").write_bytes((tmp_path / "0.jpg").read_bytes())
    item.add_media(Media(filename=str(tmp_path / "copy.jpg")), id="copy")
    ask = mocker.patch.object(enricher, "ask", side_effect=["RATING: 4\nREASON: A body.", "The summary."])

    enricher.enrich(item)

    assert ask.call_count == 2  # one rating, and the summary
    assert [m.get("graphic_rating") for m in item.media] == [4, 4, 4]
    assert item.media[2].get("graphic_description") == "A body."
    assert item.media[2].get("graphic_seconds") == 0
    assert item.get("graphic_rating") == 4
    assert ask.call_args_list[1].args[0].count("rating 4") == 1


@pytest.mark.parametrize(
    "kernel, expected",
    [("5.15.167.4-microsoft-standard-WSL2", "http://dev:11434"), ("6.8.0-48-generic", "http://prod:11434")],
)
def test_dev_machine_uses_dev_url(setup_module, mocker, kernel, expected):
    mocker.patch("platform.release", return_value=kernel)
    config = {"ollama_url": "http://prod:11434", "ollama_url_dev": "http://dev:11434"}

    assert setup_module("graphic_content_llm_enricher", config).ollama_url == expected
