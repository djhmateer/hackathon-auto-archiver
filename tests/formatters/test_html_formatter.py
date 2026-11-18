from auto_archiver.modules.html_formatter import HtmlFormatter
from auto_archiver.core import Metadata, Media


def test_format(setup_module):
    formatter = setup_module(HtmlFormatter)

    metadata = Metadata().set("content", "Hello, world!").set_url("https://example.com")

    final_media = formatter.format(metadata)
    assert isinstance(final_media, Media)
    assert ".html" in final_media.filename
    with open(final_media.filename, "r", encoding="utf-8") as f:
        content = f.read()
        assert "Hello, world!" in content
    assert final_media.mimetype == "text/html"
    assert "SHA-256:" in final_media.get("hash")


def test_media_properties_after_first_three_are_collapsed(setup_module, tmp_path):
    formatter = setup_module(HtmlFormatter)
    long = Media(str(tmp_path / "long.jpg"))
    for i in range(6):
        long.set(f"prop{i}", f"value{i}")
    long.set("size", "11.5 MB")
    short = Media(str(tmp_path / "short.jpg")).set("only", "value")
    # 4 properties would collapse just one, so all are shown
    four = Media(str(tmp_path / "four.jpg"))
    for i in range(4):
        four.set(f"four{i}", f"value{i}")
    metadata = Metadata().set_url("https://example.com")
    metadata.add_media(long)
    metadata.add_media(short)
    metadata.add_media(four)

    with open(formatter.format(metadata).filename, encoding="utf-8") as f:
        content = f.read()

    assert content.count("<details>") == 1
    assert content.count("</details>") == 1
    assert "3 more</summary>" in content
    assert "1 more</summary>" not in content
    # size is shown once, before the collapsed part, and doesn't count towards it
    assert content.count("<b>size:</b> 11.5 MB") == 1
    assert content.index("<b>size:</b> 11.5 MB") < content.index("<details>")
    # the first 3 are before the collapsed part, the rest inside it
    details = content.index("<details>")
    assert content.index("prop2:") < details < content.index("prop3:") < content.index("</details>")
