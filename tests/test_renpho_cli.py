"""Tests for the `renpho-import` command line.

`connect` is replaced by a fake API that serves stored readings and records
every upload, so nothing reaches RENPHO. Argument parsing, the export reader,
the plan, the confirmation and the readback checks run for real.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from zepp2renpho import renpho_cli
from zepp2renpho.records import Member, Record, write_members, write_records
from zepp2renpho.renpho import STORE_SCALE, Profile, Row

FIXTURES = Path(__file__).parent / "fixtures" / "renpho"
OWNER = Profile("7001234", "Owner", is_baby=False, is_account_owner=True)
BABY = Profile("7005678", "Bimba Uno", is_baby=True, is_account_owner=False)
STORED_AT = 1763894400  # The timestamp of the stored fixture reading.
ENV_NAMES = ("RENPHO_EMAIL", "RENPHO_PASSWORD")


def stored_row(timestamp: int = STORED_AT, **changes) -> Row:
    """A stored adult reading, copied from the fixture with some fields changed."""
    data = json.loads((FIXTURES / "adult_row.json").read_text(encoding="utf-8"))
    data.update(timeStamp=timestamp, **changes)
    return Row(store=STORE_SCALE, table="measurements_info_6", timestamp=timestamp, data=data)


def owner_record(timestamp: int, weight: float = 81.35, **changes) -> Record:
    """An exported reading of the owner. The defaults equal the stored fixture."""
    metrics = dict(
        bmi=25.7, body_fat_pct=21.4, water_pct=55.2, muscle_kg=61.2, bone_kg=3.3,
        bmr_kcal=1778, visceral_fat=8.0, protein_pct=17.4, body_age=36, impedance=512,
    )
    metrics.update(changes)
    return Record("-1", timestamp, weight, **metrics)


class FakeApi:
    """The RENPHO account: stored readings in, every upload recorded."""

    def __init__(self, history: list[Row], account_id: str = "7001234") -> None:
        self.account_id = account_id
        self.stored = list(history)
        self.uploads: list[list[dict]] = []
        self.fail_upload = False

    def profiles(self) -> list[Profile]:
        return [OWNER, BABY]

    def history(self, profile_id: str) -> dict[int, list[Row]]:
        grouped: dict[int, list[Row]] = {}
        if profile_id == OWNER.profile_id:
            for row in self.stored:
                grouped.setdefault(row.timestamp, []).append(row)
        return grouped

    def upload(self, payloads, zone) -> None:
        self.uploads.append(list(payloads))
        if self.fail_upload:
            return  # The cloud accepts the call but never stores the reading.
        for payload in payloads:
            self.stored.append(stored_row(payload["timeStamp"], **{
                key: payload[key] for key in ("weight", "bmi", "bodyfat", "water", "bmr",
                                               "bodyage", "sinew", "bone", "visfat", "protein",
                                               "resistance") if key in payload
            }))


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No real credentials, no pauses between batches."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(renpho_cli.time, "sleep", lambda seconds: None)


def use_api(monkeypatch, api: FakeApi) -> FakeApi:
    """Make every command sign in to the fake account."""
    monkeypatch.setattr(renpho_cli, "connect", lambda args: api)
    return api


def type_into_prompt(monkeypatch, text: str | None) -> list[str]:
    """Answer the confirmation prompt, and record every prompt shown."""
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        if text is None:
            raise EOFError
        return text

    monkeypatch.setattr("builtins.input", answer)
    return prompts


def write_export(folder: Path, records: list[Record], members: list[Member] | None = None) -> Path:
    """Write `members.json` and `records.jsonl` the way zepp-export does."""
    folder.mkdir(parents=True, exist_ok=True)
    write_members(folder / "members.json", members or [Member("-1", "Carlo", is_baby=False)])
    write_records(folder / "records.jsonl", records)
    return folder


def write_mapping(path: Path, mapping: dict) -> Path:
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return path


@pytest.fixture
def export(tmp_path) -> Path:
    """One matching reading, two missing ones and one conflicting one."""
    return write_export(
        tmp_path / "export",
        [
            owner_record(STORED_AT),
            owner_record(STORED_AT + 100, weight=80.0),
            owner_record(STORED_AT + 200, weight=79.5),
            owner_record(STORED_AT + 300, weight=70.0),
        ],
    )


@pytest.fixture
def mapping(tmp_path) -> Path:
    return write_mapping(tmp_path / "mapping.json", {"-1": OWNER.profile_id})


def account_history() -> list[Row]:
    """The stored fixture reading plus a different one at STORED_AT + 300."""
    return [stored_row(), stored_row(STORED_AT + 300, weight=75.0)]


def command(name: str, export: Path, mapping: Path, *extra: str) -> list[str]:
    """A full command line for a command that signs in and reads the export."""
    return [name, "--email", "me@example.com", "--export", str(export), "--mapping", str(mapping), *extra]


# --- argument parsing -------------------------------------------------------


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit) as stop:
        renpho_cli.main([])

    assert stop.value.code == 2
    assert "command" in capsys.readouterr().err


def test_email_is_required_without_the_environment(capsys):
    with pytest.raises(SystemExit) as stop:
        renpho_cli.main(["members"])

    assert stop.value.code == 2
    assert "--email" in capsys.readouterr().err


def test_email_defaults_to_the_environment(monkeypatch):
    monkeypatch.setenv("RENPHO_EMAIL", "env@example.com")

    args = renpho_cli.build_parser().parse_args(["members"])

    assert args.email == "env@example.com"


def test_the_email_flag_beats_the_environment(monkeypatch):
    monkeypatch.setenv("RENPHO_EMAIL", "env@example.com")

    args = renpho_cli.build_parser().parse_args(["members", "--email", "flag@example.com"])

    assert args.email == "flag@example.com"


def test_data_commands_default_to_the_rome_zone(export, mapping):
    args = renpho_cli.build_parser().parse_args(command("plan", export, mapping))

    assert args.timezone == "Europe/Rome"
    assert args.borrow_owner_template is False
    assert args.handler is renpho_cli.run_plan


def test_export_and_mapping_are_required_for_plan(capsys):
    with pytest.raises(SystemExit) as stop:
        renpho_cli.main(["plan", "--email", "me@example.com"])

    assert stop.value.code == 2
    assert "--export" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--account-id", "abc"], "--account-id must be the numeric"),
        (["--account-id", "7001234", "--limit", "0"], "--limit must be a positive"),
    ],
)
def test_import_rejects_bad_numbers(export, mapping, capsys, extra, message):
    argv = command("import", export, mapping, *extra)
    if "--account-id" not in extra:
        argv += ["--account-id", "7001234"]

    with pytest.raises(SystemExit) as stop:
        renpho_cli.main(argv)

    assert stop.value.code == 2
    assert message in capsys.readouterr().err


def test_import_needs_the_account_id(export, mapping, capsys):
    with pytest.raises(SystemExit) as stop:
        renpho_cli.main(command("import", export, mapping))

    assert stop.value.code == 2
    assert "--account-id" in capsys.readouterr().err


def test_show_payload_must_be_positive(export, mapping, capsys):
    with pytest.raises(SystemExit) as stop:
        renpho_cli.main(command("plan", export, mapping, "--show-payload", "0"))

    assert stop.value.code == 2
    assert "--show-payload must be a positive" in capsys.readouterr().err


def test_the_password_is_asked_only_when_the_environment_has_none(monkeypatch):
    monkeypatch.setattr(renpho_cli.getpass, "getpass", lambda prompt: "typed")

    assert renpho_cli.read_password() == "typed"

    monkeypatch.setenv("RENPHO_PASSWORD", "from-env")
    assert renpho_cli.read_password() == "from-env"


# --- members ----------------------------------------------------------------


def test_members_lists_profiles_with_their_template_state(monkeypatch, capsys):
    use_api(monkeypatch, FakeApi(account_history()))

    assert renpho_cli.main(["members", "--email", "me@example.com"]) == 0

    out = capsys.readouterr().out
    assert "Account id: 7001234" in out
    owner_line = next(line for line in out.splitlines() if "Owner" in line and "readings" in line)
    assert "account owner" in owner_line
    assert "2 readings" in owner_line
    assert "template ok (deviceType 00038" in owner_line
    baby_line = next(line for line in out.splitlines() if "Bimba Uno" in line)
    assert "baby" in baby_line
    assert "0 readings" in baby_line
    assert "template MISSING" in baby_line
    assert "Use the account id above as --account-id" in out


# --- mapping-template -------------------------------------------------------


def test_mapping_template_writes_an_empty_entry_per_member(tmp_path, export, capsys):
    target = tmp_path / "mapping.json"

    code = renpho_cli.main(["mapping-template", "--export", str(export), "--mapping", str(target)])

    assert code == 0
    assert json.loads(target.read_text(encoding="utf-8")) == {"-1": ""}
    out = capsys.readouterr().out
    assert f"Wrote {target} with 1 empty entries." in out
    assert "Carlo" in out
    assert " 4" in out  # The reading count of the member.


def test_mapping_template_refuses_to_overwrite_without_force(tmp_path, export, capsys):
    target = write_mapping(tmp_path / "mapping.json", {"-1": "7001234"})

    code = renpho_cli.main(["mapping-template", "--export", str(export), "--mapping", str(target)])

    assert code == 1
    assert "exists. Pass --force" in capsys.readouterr().err
    assert json.loads(target.read_text(encoding="utf-8")) == {"-1": "7001234"}


def test_mapping_template_overwrites_with_force(tmp_path, export):
    target = write_mapping(tmp_path / "mapping.json", {"-1": "7001234"})

    code = renpho_cli.main(
        ["mapping-template", "--export", str(export), "--mapping", str(target), "--force"]
    )

    assert code == 0
    assert json.loads(target.read_text(encoding="utf-8")) == {"-1": ""}


def test_a_missing_export_is_reported(tmp_path, capsys):
    code = renpho_cli.main(
        ["mapping-template", "--export", str(tmp_path / "nope"), "--mapping", str(tmp_path / "m.json")]
    )

    assert code == 1
    assert "Run zepp-export first." in capsys.readouterr().err


# --- plan -------------------------------------------------------------------


def test_plan_counts_matching_missing_and_conflicting_readings(monkeypatch, export, mapping, capsys):
    api = use_api(monkeypatch, FakeApi(account_history()))

    assert renpho_cli.main(command("plan", export, mapping)) == 0

    out = capsys.readouterr().out
    assert "Carlo (Zepp -1) -> Owner (RENPHO 7001234, adult, deviceType 00038): " in out
    assert "1 matching, 2 missing, 1 conflicts." in out
    assert "conflict 2025-11-23 11:45:00 (" in out
    assert api.uploads == []


def test_plan_never_asks_for_confirmation(monkeypatch, export, mapping):
    use_api(monkeypatch, FakeApi(account_history()))
    prompts = type_into_prompt(monkeypatch, "UPLOAD")

    renpho_cli.main(command("plan", export, mapping))

    assert prompts == []


def test_show_payload_prints_the_first_missing_readings_as_json(monkeypatch, export, mapping, capsys):
    use_api(monkeypatch, FakeApi(account_history()))

    renpho_cli.main(command("plan", export, mapping, "--show-payload", "1"))

    out = capsys.readouterr().out
    assert "Next 1 payload(s) as they would be sent:" in out
    shown = json.loads(out[out.index("[") :])
    assert [p["timeStamp"] for p in shown] == [STORED_AT + 100]
    assert shown[0]["weight"] == 80.0


def test_show_payload_says_so_when_nothing_is_missing(monkeypatch, tmp_path, mapping, capsys):
    only_matching = write_export(tmp_path / "e", [owner_record(STORED_AT)])
    use_api(monkeypatch, FakeApi([stored_row()]))

    renpho_cli.main(command("plan", only_matching, mapping, "--show-payload", "2"))

    assert "No missing readings, so there is no payload to show." in capsys.readouterr().out


def test_plan_names_the_members_the_mapping_leaves_out(monkeypatch, tmp_path, capsys):
    members = [Member("-1", "Carlo", is_baby=False), Member("55", "Nonna"), Member("56", "Zio")]
    folder = write_export(tmp_path / "e", [owner_record(STORED_AT)], members)
    mapping = write_mapping(tmp_path / "m.json", {"-1": "7001234", "55": "", "56": "9999999"})
    use_api(monkeypatch, FakeApi([stored_row()]))

    assert renpho_cli.main(command("plan", folder, mapping)) == 0

    out = capsys.readouterr().out
    assert "Skipped Nonna (Zepp id 55): no RENPHO profile in the mapping file." in out
    assert "Skipped Zio (Zepp id 56): RENPHO profile 9999999 is not on this account." in out


def test_plan_reports_a_profile_without_a_reading(monkeypatch, tmp_path, capsys):
    members = [Member("-1", "Carlo", is_baby=False), Member("55", "Bimba", is_baby=True)]
    folder = write_export(tmp_path / "e", [owner_record(STORED_AT)], members)
    mapping = write_mapping(tmp_path / "m.json", {"-1": "7001234", "55": "7005678"})
    use_api(monkeypatch, FakeApi([stored_row()]))

    code = renpho_cli.main(command("plan", folder, mapping))

    assert code == 1
    err = capsys.readouterr().err
    assert "RENPHO profile 7005678 (Bimba Uno) has no reading to copy" in err
    assert "Baby Weighing mode" in err


def test_borrow_owner_template_lets_a_profile_without_readings_plan(monkeypatch, tmp_path, capsys):
    members = [Member("55", "Bimba", is_baby=True)]
    baby_record = Record("55", STORED_AT + 100, 9.6)
    folder = write_export(tmp_path / "e", [baby_record], members)
    mapping = write_mapping(tmp_path / "m.json", {"55": "7005678"})
    use_api(monkeypatch, FakeApi([stored_row()]))

    code = renpho_cli.main(command("plan", folder, mapping, "--borrow-owner-template"))

    assert code == 0
    out = capsys.readouterr().out
    assert "Bimba Uno: no reading yet, borrowing the scale identity of Owner." in out
    assert "0 matching, 1 missing, 0 conflicts." in out


def test_an_unknown_timezone_is_reported(monkeypatch, export, mapping, capsys):
    use_api(monkeypatch, FakeApi(account_history()))

    code = renpho_cli.main(command("plan", export, mapping, "--timezone", "Mars/Olympus"))

    assert code == 1
    assert "Unknown time zone: Mars/Olympus" in capsys.readouterr().err


def test_an_unreadable_mapping_is_reported(monkeypatch, export, tmp_path, capsys):
    use_api(monkeypatch, FakeApi(account_history()))
    mapping = tmp_path / "bad.json"
    mapping.write_text('["not", "an", "object"]', encoding="utf-8")

    code = renpho_cli.main(command("plan", export, mapping))

    assert code == 1
    assert "must contain a JSON object" in capsys.readouterr().err


# --- import and the UPLOAD confirmation ------------------------------------


def import_args(export: Path, mapping: Path, *extra: str) -> list[str]:
    return command("import", export, mapping, "--account-id", "7001234", *extra)


@pytest.fixture
def clean_export(tmp_path) -> Path:
    """One reading RENPHO already has, plus two it is missing."""
    return write_export(
        tmp_path / "clean",
        [
            owner_record(STORED_AT),
            owner_record(STORED_AT + 100, weight=80.0),
            owner_record(STORED_AT + 200, weight=79.5),
        ],
    )


def test_import_without_upload_sends_nothing(monkeypatch, clean_export, mapping, capsys):
    api = use_api(monkeypatch, FakeApi([stored_row()]))
    prompts = type_into_prompt(monkeypatch, "yes")

    code = renpho_cli.main(import_args(clean_export, mapping))

    assert code == 1
    assert prompts == ["Type UPLOAD to proceed: "]
    assert api.uploads == []
    assert "Stopped before uploading." in capsys.readouterr().err


@pytest.mark.parametrize("answer", ["", "upload", " UPLOAD", "UPLOAD ", "Y"])
def test_only_the_exact_word_confirms(monkeypatch, clean_export, mapping, answer):
    api = use_api(monkeypatch, FakeApi([stored_row()]))
    type_into_prompt(monkeypatch, answer)

    assert renpho_cli.main(import_args(clean_export, mapping)) == 1
    assert api.uploads == []


def test_a_closed_input_sends_nothing(monkeypatch, clean_export, mapping, capsys):
    api = use_api(monkeypatch, FakeApi([stored_row()]))
    type_into_prompt(monkeypatch, None)

    code = renpho_cli.main(import_args(clean_export, mapping))

    assert code == 1
    assert api.uploads == []
    assert "Stopped. Run plan before resuming." in capsys.readouterr().err


def test_import_with_upload_sends_the_missing_readings_newest_first(
    monkeypatch, clean_export, mapping, capsys
):
    api = use_api(monkeypatch, FakeApi([stored_row()]))
    type_into_prompt(monkeypatch, "UPLOAD")

    code = renpho_cli.main(import_args(clean_export, mapping))

    assert code == 0
    assert len(api.uploads) == 1
    assert [p["timeStamp"] for p in api.uploads[0]] == [STORED_AT + 200, STORED_AT + 100]
    out = capsys.readouterr().out
    assert "Owner (RENPHO 7001234): uploading 2 reading(s), newest first." in out
    assert "Verified 2 of 2 reading(s)." in out


def test_limit_caps_the_upload_to_the_newest_readings(monkeypatch, clean_export, mapping):
    api = use_api(monkeypatch, FakeApi([stored_row()]))
    type_into_prompt(monkeypatch, "UPLOAD")

    renpho_cli.main(import_args(clean_export, mapping, "--limit", "1"))

    assert [[p["timeStamp"] for p in batch] for batch in api.uploads] == [[STORED_AT + 200]]


def test_uploads_go_in_batches_of_ten_with_a_pause(monkeypatch, tmp_path, mapping):
    pauses: list[float] = []
    monkeypatch.setattr(renpho_cli.time, "sleep", pauses.append)
    records = [owner_record(STORED_AT)] + [
        owner_record(STORED_AT + 100 * n, weight=70.0 + n) for n in range(1, 26)
    ]
    folder = write_export(tmp_path / "big", records)
    api = use_api(monkeypatch, FakeApi([stored_row()]))
    type_into_prompt(monkeypatch, "UPLOAD")

    code = renpho_cli.main(import_args(folder, mapping))

    assert code == 0
    assert [len(batch) for batch in api.uploads] == [10, 10, 5]
    # One pause after every upload, plus one between batches.
    assert len(pauses) == 3 + 2


def test_import_refuses_a_member_whose_first_reading_was_never_verified(
    monkeypatch, tmp_path, mapping, capsys
):
    folder = write_export(tmp_path / "e", [owner_record(STORED_AT + 100, weight=80.0)])
    api = use_api(monkeypatch, FakeApi([stored_row()]))
    prompts = type_into_prompt(monkeypatch, "UPLOAD")

    code = renpho_cli.main(import_args(folder, mapping))

    assert code == 1
    assert prompts == []
    assert api.uploads == []
    assert "no imported reading has been verified yet" in capsys.readouterr().err


def test_import_stops_on_a_timestamp_conflict(monkeypatch, export, mapping, capsys):
    api = use_api(monkeypatch, FakeApi(account_history()))
    prompts = type_into_prompt(monkeypatch, "UPLOAD")

    code = renpho_cli.main(import_args(export, mapping))

    assert code == 1
    assert prompts == []
    assert api.uploads == []
    assert "1 timestamp conflict(s)" in capsys.readouterr().err


def test_import_with_nothing_missing_does_not_ask(monkeypatch, tmp_path, mapping, capsys):
    folder = write_export(tmp_path / "e", [owner_record(STORED_AT)])
    api = use_api(monkeypatch, FakeApi([stored_row()]))
    prompts = type_into_prompt(monkeypatch, "UPLOAD")

    assert renpho_cli.main(import_args(folder, mapping)) == 0

    assert prompts == []
    assert api.uploads == []
    assert "Owner (RENPHO 7001234): nothing to upload." in capsys.readouterr().out


def test_import_refuses_a_different_account_before_reading_anything(
    monkeypatch, clean_export, mapping, capsys
):
    api = use_api(monkeypatch, FakeApi([stored_row()], account_id="555"))
    prompts = type_into_prompt(monkeypatch, "UPLOAD")

    code = renpho_cli.main(import_args(clean_export, mapping))

    assert code == 1
    assert prompts == []
    assert api.uploads == []
    assert "Signed in as account 555, but --account-id says 7001234." in capsys.readouterr().err


def test_a_reading_the_cloud_does_not_return_stops_the_import(
    monkeypatch, clean_export, mapping, capsys
):
    api = use_api(monkeypatch, FakeApi([stored_row()]))
    api.fail_upload = True
    type_into_prompt(monkeypatch, "UPLOAD")

    code = renpho_cli.main(import_args(clean_export, mapping))

    assert code == 1
    assert len(api.uploads) == 1
    assert "Readback could not verify" in capsys.readouterr().err


def test_member_restricts_the_import(monkeypatch, clean_export, mapping, capsys):
    api = use_api(monkeypatch, FakeApi([stored_row()]))
    type_into_prompt(monkeypatch, "UPLOAD")

    code = renpho_cli.main(import_args(clean_export, mapping, "--member", "999"))

    assert code == 1
    assert api.uploads == []
    assert "Zepp member 999 is not mapped to a RENPHO profile." in capsys.readouterr().err


# --- test command -----------------------------------------------------------


def test_args_of_test(export, mapping):
    args = renpho_cli.build_parser().parse_args(
        command("test", export, mapping, "--account-id", "7001234", "--member", "-1", "--at", "x")
    )

    assert (args.member, args.at, args.handler) == ("-1", "x", renpho_cli.run_test)


def test_test_without_upload_sends_nothing(monkeypatch, clean_export, mapping, capsys):
    api = use_api(monkeypatch, FakeApi([stored_row()]))
    prompts = type_into_prompt(monkeypatch, "no")

    code = renpho_cli.main(
        command("test", clean_export, mapping, "--account-id", "7001234", "--member", "-1")
    )

    assert code == 1
    assert prompts == ["Type UPLOAD to proceed: "]
    assert api.uploads == []
    assert "Selected the reading of 2025-11-23 11:43:20." in capsys.readouterr().out


def test_test_with_upload_sends_only_the_newest_missing_reading(monkeypatch, clean_export, mapping):
    api = use_api(monkeypatch, FakeApi([stored_row()]))
    type_into_prompt(monkeypatch, "UPLOAD")

    code = renpho_cli.main(
        command("test", clean_export, mapping, "--account-id", "7001234", "--member", "-1")
    )

    assert code == 0
    assert [[p["timeStamp"] for p in batch] for batch in api.uploads] == [[STORED_AT + 200]]


def test_test_at_picks_the_named_reading(monkeypatch, clean_export, mapping):
    api = use_api(monkeypatch, FakeApi([stored_row()]))
    type_into_prompt(monkeypatch, "UPLOAD")

    renpho_cli.main(
        command(
            "test", clean_export, mapping, "--account-id", "7001234", "--member", "-1",
            "--at", "2025-11-23 11:41:40",
        )
    )

    assert [p["timeStamp"] for p in api.uploads[0]] == [STORED_AT + 100]


def test_test_at_that_matches_nothing_is_reported(monkeypatch, clean_export, mapping, capsys):
    api = use_api(monkeypatch, FakeApi([stored_row()]))
    prompts = type_into_prompt(monkeypatch, "UPLOAD")

    code = renpho_cli.main(
        command(
            "test", clean_export, mapping, "--account-id", "7001234", "--member", "-1",
            "--at", "1999-01-01 00:00:00",
        )
    )

    assert code == 1
    assert prompts == []
    assert api.uploads == []
    assert "does not match a missing reading" in capsys.readouterr().err


# --- failure reporting ------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


def test_a_rate_limit_asks_to_wait_and_replan():
    error = Exception("boom")
    error.response = FakeResponse(429, {"Retry-After": "30"})

    message = renpho_cli.describe_failure(error)

    assert "HTTP 429" in message
    assert "Wait at least 30 seconds." in message
    assert "Run plan before resuming." in message


def test_an_unexpected_error_never_prints_its_message():
    message = renpho_cli.describe_failure(RuntimeError("token=secret-abc"))

    assert "secret-abc" not in message
    assert "RuntimeError" in message


def test_main_hides_the_text_of_an_unexpected_error(monkeypatch, capsys):
    def explode(args):
        raise RuntimeError("token=secret-abc")

    monkeypatch.setattr(renpho_cli, "connect", explode)

    assert renpho_cli.main(["members", "--email", "me@example.com"]) == 1

    assert "secret-abc" not in capsys.readouterr().err
