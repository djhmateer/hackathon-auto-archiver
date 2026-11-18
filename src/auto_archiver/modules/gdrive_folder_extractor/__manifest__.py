{
    "name": "Google Drive Folder Extractor",
    "type": ["extractor"],
    "entry_point": "gdrive_folder_extractor::GDriveFolderExtractor",
    "requires_setup": True,
    "dependencies": {
        "python": ["loguru", "googleapiclient", "google"],
    },
    "configs": {
        "service_account": {
            "default": "secrets/service_account.json",
            "help": "service account JSON file path. The Drive API must be enabled in its Google Cloud project.",
        },
        "max_files": {
            "default": 5000,
            "type": "int",
            "help": "stop downloading after this many files; what was downloaded is kept and the archive is marked partial.",
        },
        "max_bytes": {
            "default": 5_000_000_000,
            "type": "int",
            "help": "stop downloading once this many bytes are downloaded; what was downloaded is kept and the archive is marked partial.",
        },
    },
    "description": """
    Archives a public (anyone with the link) Google Drive folder, eg https://drive.google.com/drive/folders/<folder_id>,
    using the Drive API v3 with a service account.

    ### Features
    - Walks the folder and all its subfolders, following shortcuts.
    - Downloads every file as one Media, with all the metadata the Drive API gives (link, name, path within the
      folder, owners, created/modified times, size, Drive's checksums, image/video metadata...) as its properties.
    - Google Docs, Sheets and Slides are exported as .docx, .xlsx and .pptx, Drawings as .svg.
    - Checks the downloaded file against Drive's sha256 checksum where Drive has one (uploaded files only).
    - Files that can't be downloaded (eg Forms, or an error) are listed in `drive_not_downloaded`.

    Only matches folder links: links to single files (/file/d/...) are left to the generic extractor (yt-dlp).
    Put this extractor before generic_extractor, as yt-dlp also claims folder links.
    """,
}
