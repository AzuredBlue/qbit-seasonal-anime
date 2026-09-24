from unittest.mock import MagicMock

from qbit_seasonal_anime.clients.qbit import QBitClient


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
