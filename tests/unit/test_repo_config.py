import pytest

from core.analysis.config import RepoConfig, load_config
from core.errors import FatalError


def test_missing_or_empty_file_gives_defaults():
    assert load_config(None) == RepoConfig()
    assert load_config("   \n") == RepoConfig()
    assert load_config("# only a comment\n") == RepoConfig()


def test_valid_config():
    cfg = load_config("max_comments: 3\ncategories: [bug, security]\ninstall:\n  - pip install -e .\n")
    assert cfg.max_comments == 3
    assert cfg.categories == {"bug", "security"}
    assert cfg.install == ["pip install -e ."]


@pytest.mark.parametrize("text, where", [
    ("max_comment: 3", "max_comment"),        # typo'd key
    ("max_comments: 99", "max_comments"),     # above the limit
    ("test_timeout_s: 5000", "test_timeout_s"),
])
def test_invalid_config_names_the_field(text, where):
    with pytest.raises(FatalError) as e:
        load_config(text)
    assert f"`{where}`" in e.value.user_message


def test_broken_yaml_and_wrong_shape():
    with pytest.raises(FatalError):
        load_config("max_comments: [unclosed")
    with pytest.raises(FatalError):
        load_config("- just\n- a list\n")


def test_yaml_tags_cannot_create_objects():
    # yaml.load (unsafe) would try to build a Python object here; safe_load refuses
    with pytest.raises(FatalError):
        load_config("install: !!python/object/apply:os.system ['echo hacked']")
