"""The test copy's own Slack app: its manifest is wanda's but for its names,
and the scratch overlay takes its Slack values only from wanda-scratch's
variables."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# the only values the two manifests may differ in
NAMES = ("  name: ", "  description: ", "    display_name: ")


def _lines(path: Path) -> list[str]:
    """A manifest's lines that are not comments or blank."""
    return [ln for ln in path.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_the_scratch_manifest_is_wandas_but_for_its_names():
    """Scopes, events, Socket Mode and the Messages tab are wanda's, so a
    check runs on what the household runs on."""
    ours, scratch = _lines(ROOT / "slack/manifest.yaml"), _lines(ROOT / "slack/manifest-scratch.yaml")
    assert len(ours) == len(scratch)
    differ = [(a, b) for a, b in zip(ours, scratch) if a != b]
    assert [a.split(":")[0] for a, _ in differ] == [n.rstrip(": ") for n in NAMES]
    assert all(a.startswith(n) and b.startswith(n) for (a, b), n in zip(differ, NAMES))
    # by field name, so that wanda's description may be reworded alone; her
    # names stay wanda, the name the household's doctor is read for
    field = {a.split(":")[0].strip(): (a, b) for a, b in differ}
    assert field["name"] == ("  name: wanda", "  name: wanda-scratch")
    assert field["display_name"] == ("    display_name: wanda", "    display_name: wanda-scratch")
    assert "answers only while fan runs a check" in field["description"][1]


def test_the_scratch_takes_its_slack_values_only_from_its_own_variables():
    """Each of wanda's Slack values is overridden by wanda-scratch's, which
    compose refuses unset or empty: the scratch connects only with the
    values set for wanda-scratch, so it never takes wanda's connection, or
    posts to her alerts channel, from her own variables."""
    overlay = (ROOT / "compose.foldin-check.yaml").read_text()
    for ours, scratch in (("WANDA_SLACK_BOT_TOKEN", "WANDA_SCRATCH_SLACK_BOT_TOKEN"),
                          ("WANDA_SLACK_APP_TOKEN", "WANDA_SCRATCH_SLACK_APP_TOKEN"),
                          ("WANDA_ALERT_CHANNEL", "WANDA_SCRATCH_ALERT_CHANNEL")):
        assert re.search(rf"\n      {ours}: \$\{{{scratch}:\?set it in \.env, [^:{{}}\n]+\}}\n", overlay), ours
        assert re.search(rf"\n{scratch}=\n", (ROOT / ".env.example").read_text()), scratch
    assert "compose.wanda.yaml stop" not in overlay and "compose.wanda.yaml start" not in overlay
