"""Smoke tests: the app boots against an isolated library and responds."""


def test_client_boots_with_isolated_library(client, test_config):
    # Lifespan ran: the isolated stores exist and the real library was untouched
    assert test_config.db_path.exists()
    assert test_config.chroma_dir.exists()
    assert "tiro-library" not in str(test_config.library)
