from wanda.config import LEGACY_ENV_NAMES, Config, legacy_env_names_in_use


def test_email_settings_read_from_prefixed_names(monkeypatch):
    monkeypatch.setenv("WANDA_EMAIL_ENFORCEMENT", "live")
    monkeypatch.setenv("WANDA_EMAIL_NEVER_TRASH", "a.example, b@c.example")
    monkeypatch.setenv("WANDA_EMAIL_TRIAGE_BATCH_SIZE", "1")
    cfg = Config(_env_file=None)
    assert cfg.email_enforcement == "live"
    assert cfg.email_never_trash == ["a.example", "b@c.example"]
    assert cfg.email_triage_batch_size == 1


def test_bare_names_no_longer_bind(monkeypatch):
    """The rename must not leave a bare name half-working."""
    monkeypatch.setenv("WANDA_ENFORCEMENT", "live")
    assert Config(_env_file=None).email_enforcement == "shadow"


def test_legacy_names_are_reported_from_environ_and_dotenv(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("WANDA_NEVER_TRASH=x.example\nWANDA_EMAIL_TRIAGE_MODEL=m\n")
    found = legacy_env_names_in_use(
        environ={"wanda_enforcement": "live", "WANDA_SLACK_BOT_TOKEN": "t"},
        env_files=[env_file, tmp_path / "missing.env"],
    )
    assert found == {
        "WANDA_ENFORCEMENT": "WANDA_EMAIL_ENFORCEMENT",
        "WANDA_NEVER_TRASH": "WANDA_EMAIL_NEVER_TRASH",
    }
    assert legacy_env_names_in_use(environ={}, env_files=[]) == {}


def test_every_renamed_field_has_a_legacy_entry():
    """Each email_* setting the old bare name could have set is guarded."""
    prefixed = {n for n in Config.model_fields if n.startswith("email_")}
    guarded = {new.removeprefix("WANDA_").lower() for new in LEGACY_ENV_NAMES.values()}
    # These two were already prefixed before the rename and had no bare form.
    assert prefixed - guarded == {"email_triage_model", "email_triage_slack_channel_id"}
