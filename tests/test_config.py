import pytest

from youtube_downloader.config import DEFAULT_TEMP_DIR, Settings

ENV_VARS = (
    "PORT",
    "TEMP_ROOT",
    "YTDLP_COOKIES_FILE",
    "MAX_CONCURRENT_JOBS",
    "MAX_QUEUED_JOBS",
    "JOB_TIMEOUT_SECONDS",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_defaults(clean_env):
    settings = Settings.from_env()

    assert settings.port == 8000
    assert settings.temp_root == DEFAULT_TEMP_DIR
    assert settings.cookies_file is None
    assert settings.max_concurrent_jobs == 2
    assert settings.max_queued_jobs == 5
    assert settings.job_timeout_seconds == 900


def test_env_overrides(clean_env, monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.txt"
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("TEMP_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setenv("YTDLP_COOKIES_FILE", str(cookies))
    monkeypatch.setenv("MAX_CONCURRENT_JOBS", "1")
    monkeypatch.setenv("MAX_QUEUED_JOBS", "0")
    monkeypatch.setenv("JOB_TIMEOUT_SECONDS", "60")

    settings = Settings.from_env()

    assert settings.port == 9000
    assert settings.temp_root == tmp_path / "jobs"
    assert settings.cookies_file == cookies
    assert settings.max_concurrent_jobs == 1
    assert settings.max_queued_jobs == 0
    assert settings.job_timeout_seconds == 60


def test_empty_values_fall_back_to_defaults(clean_env, monkeypatch):
    monkeypatch.setenv("PORT", "")
    monkeypatch.setenv("YTDLP_COOKIES_FILE", "   ")

    settings = Settings.from_env()

    assert settings.port == 8000
    assert settings.cookies_file is None


def test_non_integer_port_is_rejected(clean_env, monkeypatch):
    monkeypatch.setenv("PORT", "eight thousand")

    with pytest.raises(ValueError, match="PORT must be an integer"):
        Settings.from_env()


@pytest.mark.parametrize(
    "overrides",
    [
        {"port": 0},
        {"port": 70000},
        {"max_concurrent_jobs": 0},
        {"max_queued_jobs": -1},
        {"job_timeout_seconds": 0},
    ],
)
def test_invalid_values_are_rejected(overrides):
    with pytest.raises(ValueError):
        Settings(**overrides)
