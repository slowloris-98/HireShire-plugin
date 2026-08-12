from __future__ import annotations

from pathlib import Path

import pdfplumber


def extract_resume_text(path: str | Path) -> str:
    path = Path(path)
    # is_file(), not exists(): an unset resume_path resolves to the data directory
    # itself, and handing a directory to pdfplumber raises PermissionError from
    # deep inside the library instead of something the user can act on.
    if not path.is_file():
        raise FileNotFoundError(
            f"No resume PDF at {path}. Run /hireshire:setup to point HireShire at your resume."
        )

    text_parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text.strip())

    text = "\n\n".join(text_parts)
    if not text.strip():
        raise ValueError(f"Could not extract any text from {path}. Is it a scanned PDF?")

    return text
