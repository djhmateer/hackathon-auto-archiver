{
    "name": "Graphic Content LLM Enricher",
    "type": ["enricher"],
    "requires_setup": False,
    "dependencies": {"python": ["loguru", "PIL", "ffmpeg"], "bin": ["ffmpeg"]},
    "configs": {
        "ollama_url": {"default": "http://localhost:11434", "help": "Ollama server URL"},
        "ollama_url_dev": {
            "default": "",
            "help": "Ollama server URL used instead of ollama_url on the dev machine (detected by a WSL2 kernel)",
        },
        "model": {"default": "muse-glimmer:30b-q4_K_M", "help": "Ollama vision model"},
        "think": {"default": "low", "help": "reasoning effort sent to Ollama, empty for the model's default"},
        "threads": {
            "default": 6,
            "type": "int",
            "help": "CPU threads Ollama uses (num_thread), sent on every call",
        },
        "long_side": {
            "default": 1366,
            "type": "int",
            "help": "images are scaled down to this many pixels on the long side before rating, for speed",
        },
        "min_side": {
            "default": 200,
            "type": "int",
            "help": "images under this many pixels wide or high (icons, avatars, logos) are not rated",
        },
        "max_minutes": {
            "default": 20,
            "type": "int",
            "help": "stop rating after this many minutes; the rest get 'needs review' (blurred). Keeps the row under "
            "the 30 minute hang dump",
        },
        "max_images": {
            "default": 0,
            "type": "int",
            "help": "rate only the first this many (videos, then images largest first), 0 means all. The rest are not rated or blurred",
        },
    },
    "description": """
    Asks a local LLM (through Ollama) how distressing each image is, from 1 to 5, with a short description.
    A video is rated on its first frame only, which is what the browser shows before it plays.

    - Sets `graphic_rating`, `graphic_description` and `graphic_seconds` (time taken) on each image and video (shown in the HTML 'about' column).
    - Sets `graphic_rating` (the highest) and `images_summary` (one non-specific paragraph from a second, text-only call, which is never given the
      descriptions of items rated 3 or more; the per-image lines if that call fails) on the item, for the sheet.
    - The HTML formatter blurs images and videos rated 3 or more until clicked.
    - An image that could not be rated gets 'needs review' and is blurred too.
    - Videos are rated first, then images largest first. Screenshots, small images, audio-only videos and
      files that can't be read are skipped (not rated or blurred). On a CPU each image takes a few minutes.
    """,
}
