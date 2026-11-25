"""Tests for the `zepp-export` command line.

`ZeppClient` and `login` are replaced, so nothing reaches the network. The
parsing, the file writing and the report run for real on the fixtures in
`tests/fixtures/zepp/`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest

from zepp2renpho import zepp_cli
from zepp2renpho.records import read_members, read_records
from zepp2renpho.zepp import DEFAULT_API_HOST, ZeppApiError, ZeppAuth

FIXTURES = Path(__file__).parent / "fixtures" / "zepp"
ENV_NAMES = ("ZEPP_APP_TOKEN", "ZEPP_USER_ID", "ZEPP_EMAIL", "ZEPP_PASSWORD", "ZEPP_API_HOST")


def load(name: str) -> dict:
    """Read one fixture file."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeZeppClient:
    """Serves the fixture answers instead of calling the Zepp API."""

    instances: ClassVar[list[FakeZeppClient]] = []
    token_error: ClassVar[Exception | None] = None

    def __init__(self, auth: ZeppAuth, *, debug=None) -> None:
        self.auth = auth
        self.debug = debug
        self.verified = False
        FakeZeppClient.instances.append(self)

    def verify_token(self) -> None:
        if FakeZeppClient.token_error:
            raise FakeZeppClient.token_error
        self.verified = True

    def fetch_user_info(self) -> dict:
        return {"data": {"nickname": "Carlo"}}

    def fetch_family_members(self) -> list[dict]:
        return load("family_members.json")["data"]["list"]

    def iter_weight_records(self, member_id: str):
        """The owner call returns the whole history, as `members/-1` does."""
        if member_id != "-1":
            return iter(())
        return iter(load("weight_page_1.json")["items"] + load("weight_page_2.json")["items"])


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No credentials from the real environment, a fake client, a fake login."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    FakeZeppClient.instances = []
    FakeZeppClient.token_error = None
    monkeypatch.setattr(zepp_cli, "ZeppClient", FakeZeppClient)
    monkeypatch.setattr(
        zepp_cli,
        "login",
        lambda *a, **k: pytest.fail("the token path must not log in"),
    )


def token_args(out: Path, *extra: str) -> list[str]:
    """The arguments of a token based run."""
    return ["--app-token", "tok", "--user-id", "1234567890", "--out", str(out), *extra]


# --- argument parsing -------------------------------------------------------


def test_the_command_defaults_to_export_into_the_export_folder():
    args = zepp_cli.build_parser().parse_args([])

    assert args.command == "export"
    assert args.out == "export"
    assert args.baby_member == []
    assert args.debug is False


def test_baby_member_can_be_repeated():
    args = zepp_cli.build_parser().parse_args(["--baby-member", "1", "--baby-member", "2"])

    assert args.baby_member == ["1", "2"]


def test_an_unknown_command_is_refused(capsys):
    with pytest.raises(SystemExit) as stop:
        zepp_cli.build_parser().parse_args(["delete"])

    assert stop.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


# --- credentials ------------------------------------------------------------


def test_token_flags_build_the_auth_without_a_login():
    args = zepp_cli.build_parser().parse_args(["--app-token", "tok", "--user-id", "42"])

    auth = zepp_cli.build_auth(args, None)

    assert auth == ZeppAuth(app_token="tok", user_id="42", api_host=DEFAULT_API_HOST)


def test_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("ZEPP_APP_TOKEN", "env-token")
    monkeypatch.setenv("ZEPP_USER_ID", "99")
    monkeypatch.setenv("ZEPP_API_HOST", "api-mifit-de2.zepp.com")
    args = zepp_cli.build_parser().parse_args([])

    auth = zepp_cli.build_auth(args, None)

    assert (auth.app_token, auth.user_id, auth.api_host) == ("env-token", "99", "api-mifit-de2.zepp.com")


def test_the_api_host_flag_beats_the_environment(monkeypatch):
    monkeypatch.setenv("ZEPP_API_HOST", "from-env.zepp.com")
    args = zepp_cli.build_parser().parse_args(
        ["--app-token", "tok", "--user-id", "42", "--api-host", "from-flag.zepp.com"]
    )

    assert zepp_cli.build_auth(args, None).api_host == "from-flag.zepp.com"


def test_a_token_without_a_user_id_does_not_count_as_credentials():
    args = zepp_cli.build_parser().parse_args(["--app-token", "tok"])

    with pytest.raises(SystemExit, match="--app-token and --user-id"):
        zepp_cli.build_auth(args, None)


def test_email_login_reads_the_password_from_the_environment(monkeypatch):
    calls = []
    auth = ZeppAuth(app_token="t", user_id="1", api_host="h")
    monkeypatch.setattr(zepp_cli, "login", lambda *a, **k: calls.append((a, k)) or auth)
    monkeypatch.setenv("ZEPP_EMAIL", "me@example.com")
    monkeypatch.setenv("ZEPP_PASSWORD", "s3cret")
    args = zepp_cli.build_parser().parse_args(["--api-host", "host.zepp.com"])

    assert zepp_cli.build_auth(args, None) is auth
    assert calls == [(("me@example.com", "s3cret"), {"api_host": "host.zepp.com", "debug": None})]


def test_the_password_is_asked_only_when_the_environment_has_none(monkeypatch):
    monkeypatch.setattr(zepp_cli, "getpass", lambda prompt: "typed")

    assert zepp_cli.read_password() == "typed"

    monkeypatch.setenv("ZEPP_PASSWORD", "from-env")
    assert zepp_cli.read_password() == "from-env"


# --- export -----------------------------------------------------------------


def test_export_writes_members_and_records(tmp_path, capsys):
    out = tmp_path / "out"

    assert zepp_cli.main(token_args(out)) == 0

    members = read_members(out / "members.json")
    assert [(m.member_id, m.name) for m in members] == [
        ("-1", "Carlo"),
        ("87654321", "Bimbo"),
        ("87654322", "Partner"),
    ]
    records = list(read_records(out / "records.jsonl"))
    assert records == sorted(records, key=lambda r: (r.member_id, r.timestamp))
    assert {r.member_id for r in records} == {"-1", "87654321"}
    assert all(r.weight_kg > 0 for r in records)
    report = capsys.readouterr().out
    assert f"{len(records)} records written to {out / 'records.jsonl'}" in report
    assert "without a usable weight" in report


def test_export_verifies_the_token_before_reading(tmp_path):
    zepp_cli.main(token_args(tmp_path / "out"))

    assert [client.verified for client in FakeZeppClient.instances] == [True]


def test_an_invalid_token_stops_before_any_file_is_written(tmp_path):
    FakeZeppClient.token_error = ZeppApiError("the app token is not valid any more (HTTP 401)")
    out = tmp_path / "out"

    with pytest.raises(ZeppApiError):
        zepp_cli.main(token_args(out))

    assert not out.exists()


def test_baby_member_flag_marks_the_member_in_the_export(tmp_path):
    out = tmp_path / "out"

    zepp_cli.main(token_args(out, "--baby-member", "87654322"))

    flags = {m.member_id: m.is_baby for m in read_members(out / "members.json")}
    assert flags["87654322"] is True


def test_export_creates_nested_output_folders(tmp_path):
    out = tmp_path / "a" / "b"

    zepp_cli.main(token_args(out))

    assert (out / "members.json").is_file()
    assert (out / "records.jsonl").is_file()


def test_debug_flag_hands_a_dump_folder_to_the_client(tmp_path):
    out = tmp_path / "out"

    zepp_cli.main(token_args(out, "--debug"))

    assert FakeZeppClient.instances[0].debug is not None
    assert FakeZeppClient.instances[0].debug.directory == out / "debug"


def test_no_debug_flag_means_no_dump(tmp_path):
    zepp_cli.main(token_args(tmp_path / "out"))

    assert FakeZeppClient.instances[0].debug is None


# --- members command --------------------------------------------------------


def test_members_prints_the_table_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "out"

    assert zepp_cli.main(token_args(out) + ["members"]) == 0

    printed = capsys.readouterr().out
    assert "member id" in printed
    for name in ("Carlo", "Bimbo", "Partner"):
        assert name in printed
    assert "no records" in printed
    assert not out.exists()


def test_members_applies_the_baby_flag(tmp_path, capsys):
    zepp_cli.main(token_args(tmp_path) + ["members", "--baby-member", "87654322"])

    partner = next(line for line in capsys.readouterr().out.splitlines() if "Partner" in line)
    assert "baby" in partner


# --- formatting -------------------------------------------------------------


def test_format_day_is_utc():
    assert zepp_cli.format_day(1731067200) == "2024-11-08"


def test_member_line_shows_count_and_date_span():
    from zepp2renpho.records import Member, Record

    member = Member("7", "Anna", is_baby=False)
    records = [
        Record("7", 1731067200, 60.0),
        Record("7", 1730980800, 61.0),
        Record("8", 1700000000, 5.0),
    ]

    line = zepp_cli.member_line(member, records)

    assert "Anna" in line
    assert "adult" in line
    assert "2024-11-07 .. 2024-11-08" in line
    assert " 2 " in line
