import logging
import time
from typing import Any, Dict, List, Optional
import qbittorrentapi

logger = logging.getLogger("qbit_seasonal_anime.clients.qbit")


class QbitClientError(Exception):
    """Base exception for qBittorrent client errors."""
    pass


class QBitClient:
    def __init__(self, host: str, username: str = "", password: str = "", timeout: int = 10):
        self.host = host
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client: Optional[qbittorrentapi.Client] = None

    def get_client(self, max_retries: int = 5, backoff_factor: float = 2.0) -> qbittorrentapi.Client:
        if self._client is not None:
            return self._client

        last_err = None
        for attempt in range(max_retries):
            try:
                client = qbittorrentapi.Client(
                    host=self.host,
                    username=self.username,
                    password=self.password,
                    VERIFY_WEBUI_CERTIFICATE=False,
                    FORCE_SCHEME_FROM_HOST=True,
                    REQUESTS_ARGS={"timeout": self.timeout},
                )
                client.auth_log_in()
                self._client = client
                return self._client
            except qbittorrentapi.LoginFailed as e:
                self._client = None
                raise QbitClientError(f"qBittorrent login failed: {e}") from e
            except Exception as e:
                last_err = e
                self._client = None
                if attempt < max_retries - 1:
                    sleep_time = min(backoff_factor * (2 ** attempt), 15.0)
                    logger.info(f"qBittorrent connection attempt {attempt + 1}/{max_retries} failed ({e}), waiting {sleep_time:.1f}s for WebUI/Docker...")
                    time.sleep(sleep_time)

        raise QbitClientError(f"Cannot connect to qBittorrent at {self.host} after {max_retries} attempts: {last_err}")

    def ensure_category_exists(self, category: str) -> None:
        """Ensure a category exists in qBittorrent, creating it if needed."""
        if not category or not category.strip():
            return
        try:
            client = self.get_client()
            cats = client.torrent_categories.categories
            if category not in cats:
                client.torrent_categories.create_category(name=category)
                logger.info(f"Created category '{category}' in qBittorrent.")
        except Exception as e:
            logger.debug(f"Could not verify/create category '{category}': {e}")

    def test_connection(self) -> Dict[str, str]:
        """Verify credentials and return application and API versions."""
        try:
            client = self.get_client()
            app_version = client.app.version
            api_version = client.app.web_api_version
            return {"app_version": app_version, "api_version": api_version}
        except Exception as e:
            self._client = None
            raise QbitClientError(f"Failed to query qBittorrent version: {e}") from e

    def get_rss_items(self, with_data: bool = True) -> Dict[str, Any]:
        """Fetch all RSS feeds and their cached articles."""
        try:
            client = self.get_client()
            return client.rss_items(include_feed_data=with_data)
        except Exception as e:
            self._client = None
            logger.warning(f"Error fetching RSS items: {e}")
            raise QbitClientError(f"Failed to fetch RSS items: {e}") from e

    def get_rss_refresh_interval_seconds(self) -> int:
        """
        Fetch the configured RSS refresh interval from qBittorrent in seconds
        and add a +15 second buffer so RSS feeds have time to fetch articles.
        """
        try:
            client = self.get_client()
            prefs = client.app_preferences()
            interval_min = prefs.get("rss_refresh_interval") or 5
            return int(interval_min * 60) + 15
        except Exception as e:
            logger.debug(f"Could not fetch qBittorrent RSS refresh interval: {e}")
            return 315

    def get_rss_feeds_flat(self) -> List[Dict[str, str]]:
        """
        Return a flat list of all RSS feeds:
        [{"name": feed_name, "url": feed_url}, ...]
        """
        items = self.get_rss_items(with_data=False)
        feeds = []

        def extract_feeds(tree: dict):
            for key, val in tree.items():
                if isinstance(val, dict):
                    if "url" in val:
                        feeds.append({"name": key, "url": val["url"]})
                    else:
                        extract_feeds(val)

        extract_feeds(items)
        return feeds

    def get_rss_rules(self) -> Dict[str, Any]:
        """Fetch all RSS auto-downloading rules."""
        client = self.get_client()
        try:
            return client.rss_rules()
        except Exception as e:
            raise QbitClientError(f"Failed to get RSS rules: {e}") from e

    def set_rss_rule(self, rule_name: str, rule_def: Dict[str, Any]) -> None:
        """Create or update an RSS auto-downloading rule."""
        client = self.get_client()
        try:
            client.rss_set_rule(rule_name=rule_name, rule_def=rule_def)
            logger.info(f"Successfully set RSS rule '{rule_name}'")
        except Exception as e:
            raise QbitClientError(f"Failed to set RSS rule '{rule_name}': {e}") from e

    def remove_rss_rule(self, rule_name: str) -> None:
        """Delete an RSS auto-downloading rule."""
        client = self.get_client()
        try:
            client.rss_remove_rule(rule_name=rule_name)
            logger.info(f"Successfully removed RSS rule '{rule_name}'")
        except Exception as e:
            logger.warning(f"Failed to remove RSS rule '{rule_name}': {e}")
            raise QbitClientError(f"Failed to remove RSS rule '{rule_name}': {e}") from e

    def get_torrents_by_category(self, category: str) -> List[Any]:
        """Fetch torrents belonging to a specific category."""
        client = self.get_client()
        try:
            return client.torrents_info(category=category)
        except Exception as e:
            raise QbitClientError(f"Failed to get torrents for category '{category}': {e}") from e

    def get_matching_articles(self, rule_name: str) -> Dict[str, List[str]]:
        """Return articles currently matching a given rule."""
        client = self.get_client()
        try:
            return client.rss_matching_articles(rule_name=rule_name)
        except Exception as e:
            logger.debug(f"Could not fetch matching articles for {rule_name}: {e}")
            return {}

    def refresh_rss_feeds(self, feed_name: str = "") -> None:
        """Trigger an immediate background refresh of all RSS feeds (or a specific feed) in qBittorrent."""
        client = self.get_client()
        try:
            client.rss_refresh_item(item_path=feed_name)
            logger.debug("Triggered immediate RSS feeds refresh in qBittorrent.")
        except Exception as e:
            logger.debug(f"Could not trigger RSS refresh in qBittorrent: {e}")

