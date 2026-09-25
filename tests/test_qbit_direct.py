from unittest.mock import MagicMock, patch

import pytest
import qbittorrentapi

from qbit_seasonal_anime.clients.qbit import QBitClient, QbitAuthenticationError, QbitConnectionError


def _client():
    client = QBitClient(host="http://localhost:8080")
    underlying = MagicMock()
    client.get_client = MagicMock(return_value=underlying)
    return client, underlying


def test_add_torrent_passes_direct_ownership_options():
    client, underlying = _client()
    underlying.torrents_add.return_value = "Ok."

    assert client.add_torrent(
        urls="magnet:test",
        save_path="/anime",
        category="Anime",
        tags=["qsa-managed", "qsa-op-1"],
        is_paused=True,
        ratio_limit=1.5,
    )

    underlying.torrents_add.assert_called_once_with(
        urls="magnet:test",
        save_path="/anime",
        category="Anime",
        tags="qsa-managed,qsa-op-1",
        is_paused=True,
        ratio_limit=1.5,
    )


def test_torrent_lookup_and_lifecycle_methods_delegate_to_client():
    client, underlying = _client()
    underlying.torrents_info.return_value = [MagicMock(hash="abc")]

    torrents = client.get_torrents(tag="qsa-managed")
    client.pause_torrents(["abc"])
    client.resume_torrents(["abc"])
    client.recheck_torrents(["abc"])
    client.delete_torrents(["abc"], delete_files=True)

    assert torrents[0].hash == "abc"
    underlying.torrents_info.assert_called_once_with(tag="qsa-managed")
    underlying.torrents_pause.assert_called_once_with(torrent_hashes=["abc"])
    underlying.torrents_resume.assert_called_once_with(torrent_hashes=["abc"])
    underlying.torrents_recheck.assert_called_once_with(torrent_hashes=["abc"])
    underlying.torrents_delete.assert_called_once_with(delete_files=True, torrent_hashes=["abc"])


def test_refresh_rss_reports_request_acceptance():
    client, underlying = _client()

    assert client.refresh_rss_feeds() is True

    underlying.rss_refresh_item.assert_called_once_with(item_path="")


def test_refresh_rss_reports_request_failure():
    client, underlying = _client()
    underlying.rss_refresh_item.side_effect = RuntimeError("refresh failed")

    assert client.refresh_rss_feeds() is False


def test_get_client_retries_transient_connection_failure():
    connected_client = MagicMock()
    client_factory = MagicMock(side_effect=[ConnectionError("starting"), connected_client])
    sleep = MagicMock()

    with patch("qbit_seasonal_anime.clients.qbit.qbittorrentapi.Client", client_factory), patch(
        "qbit_seasonal_anime.clients.qbit.time.sleep", sleep
    ):
        client = QBitClient(host="http://qbit:8080")
        result = client.get_client(max_attempts=3, backoff_factor=1.0)

    assert result is connected_client
    assert client_factory.call_count == 2
    sleep.assert_called_once_with(1.0)


def test_get_client_raises_connection_error_after_bounded_attempts():
    client_factory = MagicMock(side_effect=ConnectionError("not ready"))
    sleep = MagicMock()

    with patch("qbit_seasonal_anime.clients.qbit.qbittorrentapi.Client", client_factory), patch(
        "qbit_seasonal_anime.clients.qbit.time.sleep", sleep
    ):
        client = QBitClient(host="http://qbit:8080")
        with pytest.raises(QbitConnectionError):
            client.get_client(max_attempts=3, backoff_factor=1.0)

    assert client_factory.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 2.0]


def test_get_client_does_not_retry_authentication_failure():
    client_factory = MagicMock()
    client_factory.return_value.auth_log_in.side_effect = qbittorrentapi.LoginFailed("invalid credentials")
    sleep = MagicMock()

    with patch("qbit_seasonal_anime.clients.qbit.qbittorrentapi.Client", client_factory), patch(
        "qbit_seasonal_anime.clients.qbit.time.sleep", sleep
    ):
        client = QBitClient(host="http://qbit:8080")
        with pytest.raises(QbitAuthenticationError):
            client.get_client(max_attempts=3, backoff_factor=1.0)

    assert client_factory.call_count == 1
    sleep.assert_not_called()
