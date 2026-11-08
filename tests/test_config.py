import copy

import pytest
import yaml

from seo_reviewer.config import ConfigError, load_config, validate_config

from conftest import REPO_ROOT


def test_repo_config_is_valid():
    cfg = load_config(REPO_ROOT / "config.yaml")
    assert cfg["site"]["base_url"].startswith("https://")
    assert not cfg["site"]["base_url"].endswith("/")


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("site: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(p)


def test_empty_file(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="empty"):
        load_config(p)


def test_missing_keys_are_all_listed(cfg):
    del cfg["site"]["sitemap_url"]
    del cfg["crawler"]["user_agent"]
    del cfg["runtime"]
    with pytest.raises(ConfigError) as e:
        validate_config(cfg)
    msg = str(e.value)
    assert "site.sitemap_url: missing" in msg
    assert "crawler.user_agent: missing" in msg
    assert "runtime: missing" in msg


@pytest.mark.parametrize("path,value,expected", [
    (("site", "base_url"), "blog.example.com", "site.base_url: must be a http(s) URL"),
    (("crawler", "requests_per_second"), 0, "crawler.requests_per_second: must be a number > 0"),
    (("crawler", "max_retries"), -1, "crawler.max_retries: must be a integer >= 0"),
    (("crawler", "respect_robots_txt"), "yes", "crawler.respect_robots_txt: must be a true/false"),
    (("crawler", "max_pages"), True, "crawler.max_pages: must be a integer > 0"),
    (("crawler", "user_agent"), "  ", "crawler.user_agent: must be a non-empty string"),
    (("psi", "strategies"), ["mobile", "tablet"], "psi.strategies: only 'mobile' and 'desktop'"),
    (("links", "method"), "POST", "links.method: must be HEAD or GET"),
])
def test_bad_values(cfg, path, value, expected):
    section, key = path
    cfg[section][key] = value
    with pytest.raises(ConfigError, match=expected.replace("(", r"\(").replace(")", r"\)").replace("+", r"\+")):
        validate_config(cfg)


def test_section_must_be_mapping(cfg):
    cfg["crawler"] = "fast"
    with pytest.raises(ConfigError, match="crawler: must be a mapping"):
        validate_config(cfg)


def test_sitemap_must_be_same_host(cfg):
    cfg["site"]["sitemap_url"] = "https://other.test/sitemap.xml"
    with pytest.raises(ConfigError, match="same host"):
        validate_config(cfg)


def test_min_greater_than_max(cfg):
    cfg["thresholds"]["title_length"] = {"min": 70, "max": 60}
    with pytest.raises(ConfigError, match="title_length: min must be <= max"):
        validate_config(cfg)


def test_trailing_slash_stripped(cfg):
    cfg["site"]["base_url"] = "https://example.test/"
    assert validate_config(cfg)["site"]["base_url"] == "https://example.test"


def test_fallback_site_needs_only_config_change(cfg):
    cfg["site"].update(name="Smashing Magazine", base_url="https://www.smashingmagazine.com",
                       sitemap_url="https://www.smashingmagazine.com/sitemap.xml")
    assert validate_config(copy.deepcopy(cfg))["site"]["name"] == "Smashing Magazine"


def test_sitemap_patterns_must_be_valid_regex(cfg):
    cfg["crawler"]["sitemap_exclude"] = ["^/tag/", "(unclosed"]
    with pytest.raises(ConfigError, match=r"crawler.sitemap_exclude: invalid regular expression '\(unclosed'"):
        validate_config(cfg)


def test_psi_timeout_must_fit_budget(cfg):
    cfg["psi"]["timeout_seconds"] = 300
    with pytest.raises(ConfigError, match="psi.timeout_seconds: must be smaller than runtime.budget_seconds"):
        validate_config(cfg)
