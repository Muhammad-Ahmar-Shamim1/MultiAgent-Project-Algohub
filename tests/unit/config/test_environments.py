"""Tests for web_department.config.environments."""

from web_department.config.environments import PROFILES, Environment, get_profile


def test_every_environment_has_a_profile() -> None:
    assert set(PROFILES) == set(Environment)


def test_prod_forbids_debug_and_requires_deploy_approval() -> None:
    profile = get_profile(Environment.PROD)

    assert profile.allow_debug is False
    assert profile.require_deploy_approval is True


def test_dev_and_staging_allow_debug_without_deploy_approval() -> None:
    for env in (Environment.DEV, Environment.STAGING):
        profile = get_profile(env)

        assert profile.allow_debug is True
        assert profile.require_deploy_approval is False
