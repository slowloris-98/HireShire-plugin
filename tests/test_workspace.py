"""The user's job-search folder.

Setup copies the resume into the workspace so the folder is self-contained. That
copy runs against files the user already cares about, so the rules are: validate
before writing anything, and never overwrite.
"""
from __future__ import annotations

import pytest

from hireshire import paths, workspace


@pytest.fixture
def readable_pdf(monkeypatch):
    """Skip the real PDF parse. Two tests below want the failure path instead, so
    they override this — everything else is about copy semantics, not pdfplumber."""
    monkeypatch.setattr(workspace, "extract_resume_text", lambda p: "resume text")


def _pdf(path, content=b"%PDF-1.4 fake"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_init_creates_the_skeleton_in_an_empty_folder(tmp_path):
    ws = workspace.init_workspace(tmp_path / "hireshire_job_search")

    assert (ws / "resume" / "original").is_dir()
    assert (ws / "hireshire_run_results").is_dir()


def test_init_refuses_the_plugin_install_directory(tmp_path):
    """ROOT is replaced wholesale on every update, and cwd *is* ROOT for anyone
    running Claude Code from a plugin checkout — which is what setup offers as the
    default. This guard is the difference between that and silent data loss."""
    with pytest.raises(workspace.WorkspaceError, match="update"):
        workspace.init_workspace(paths.ROOT / "hireshire_job_search")


def test_init_refuses_the_plugin_data_directory(tmp_path, monkeypatch):
    """In a real install DATA sits outside ROOT (it is the directory that survives
    updates), so it needs its own guard. From a bare checkout the two overlap and
    the ROOT check fires first — point DATA elsewhere to test it on its own."""
    monkeypatch.setattr(paths, "DATA", tmp_path / "plugin-data")

    with pytest.raises(workspace.WorkspaceError, match="data directory"):
        workspace.init_workspace(tmp_path / "plugin-data" / "hireshire_job_search")


def test_init_strips_the_quotes_windows_explorer_adds(tmp_path):
    ws = workspace.init_workspace(f'"{tmp_path / "My Job Search"}"')

    assert ws == (tmp_path / "My Job Search").resolve()
    assert (ws / "resume" / "original").is_dir()


def test_install_copies_an_outside_resume_in(tmp_path, readable_pdf):
    ws = workspace.init_workspace(tmp_path / "ws")
    src = _pdf(tmp_path / "Downloads" / "resume.pdf")

    dest = workspace.install_resume(src, ws)

    assert dest == ws / "resume" / "original" / "resume.pdf"
    assert dest.read_bytes() == src.read_bytes()
    assert src.exists(), "the user's original must be left alone"


def test_a_resume_already_in_place_is_left_where_it_is(tmp_path, readable_pdf):
    """The documented flow — drop the PDF in resume/original/ before launching —
    must not produce a second copy of the same file."""
    ws = workspace.init_workspace(tmp_path / "ws")
    src = _pdf(ws / "resume" / "original" / "resume.pdf")

    dest = workspace.install_resume(src, ws)

    assert dest == src
    assert len(list((ws / "resume" / "original").glob("*.pdf"))) == 1


def test_installing_the_same_file_twice_is_a_no_op(tmp_path, readable_pdf):
    ws = workspace.init_workspace(tmp_path / "ws")
    src = _pdf(tmp_path / "resume.pdf")

    first = workspace.install_resume(src, ws)
    second = workspace.install_resume(src, ws)

    assert first == second
    assert len(list((ws / "resume" / "original").glob("*.pdf"))) == 1


def test_same_name_different_bytes_is_suffixed_never_overwritten(tmp_path, readable_pdf):
    """An older resume the user put there themselves is exactly what would be
    destroyed here."""
    ws = workspace.init_workspace(tmp_path / "ws")
    existing = _pdf(ws / "resume" / "original" / "resume.pdf", b"%PDF old version")
    src = _pdf(tmp_path / "Downloads" / "resume.pdf", b"%PDF new version")

    dest = workspace.install_resume(src, ws)

    assert dest.name == "resume_2.pdf"
    assert existing.read_bytes() == b"%PDF old version"


def test_an_unreadable_resume_fails_before_anything_lands_in_the_workspace(tmp_path, monkeypatch):
    """A scanned PDF must not leave a file behind that the pipeline will reject.
    Order of operations, asserted directly."""
    def _scanned(path):
        raise ValueError(f"Could not extract any text from {path}. Is it a scanned PDF?")

    monkeypatch.setattr(workspace, "extract_resume_text", _scanned)

    ws = workspace.init_workspace(tmp_path / "ws")
    src = _pdf(tmp_path / "scan.pdf")

    with pytest.raises(ValueError, match="scanned"):
        workspace.install_resume(src, ws)

    assert list((ws / "resume" / "original").iterdir()) == []


def test_find_resumes_returns_newest_first_and_ignores_other_files(tmp_path):
    import os
    import time

    ws = workspace.init_workspace(tmp_path / "ws")
    original = ws / "resume" / "original"
    old = _pdf(original / "old.pdf")
    new = _pdf(original / "new.pdf")
    (original / "notes.txt").write_text("not a resume", encoding="utf-8")

    # mtime resolution is coarse enough on some filesystems to make two writes a
    # second apart look simultaneous; set them explicitly.
    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))

    assert workspace.find_resumes(ws) == [new, old]


def test_find_resumes_on_a_folder_that_was_never_set_up(tmp_path):
    assert workspace.find_resumes(tmp_path / "nothing here") == []
