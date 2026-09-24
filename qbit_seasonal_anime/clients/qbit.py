from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
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

    def get_client(self, max_retries: int = 2, backoff_factor: float = 0.5) -> qbittorrentapi.Client:
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

    def ensure_category_exists(self, category: str) -> bool:
        """Ensure a category exists in qBittorrent, creating it if needed."""
        if not category or not category.strip():
            return True
        try:
            client = self.get_client()
            cats = client.torrent_categories.categories
            if category not in cats:
                client.torrent_categories.create_category(name=category)
                logger.info(f"Created category '{category}' in qBittorrent.")
            return True
        except Exception as e:
            logger.debug(f"Could not verify/create category '{category}': {e}")
            return False

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

    def get_rss_feed_paths(self) -> Dict[str, str]:
        items = self.get_rss_items(with_data=False)
        paths: Dict[str, str] = {}

        def extract(tree: Dict[str, Any], prefix: str = ""):
            for key, value in tree.items():
                if not isinstance(value, dict):
                    continue
                current = f"{prefix}\\{key}" if prefix else key
                if "url" in value:
                    paths[value["url"]] = current
                else:
                    extract(value, current)

        extract(items)
        return paths

    def mark_rss_article_read(self, item_path: str, article_id: str) -> None:
        client = self.get_client()
        try:
            client.rss_mark_as_read(item_path=item_path, article_id=article_id)
        except Exception as e:
            raise QbitClientError(f"Failed to mark RSS article read: {e}") from e

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

    def add_torrent(
        self,
        urls: Any,
        save_path: str = "",
        category: str = "",
        tags: Optional[Any] = None,
        is_paused: bool = False,
        ratio_limit: Optional[float] = None,
    ) -> bool:
        client = self.get_client()
        try:
            kwargs: Dict[str, Any] = {"urls": urls}
            if save_path:
                kwargs["save_path"] = save_path
            if category:
                kwargs["category"] = category
            if tags:
                kwargs["tags"] = tags if isinstance(tags, str) else ",".join(tags)
            if is_paused:
                kwargs["is_paused"] = True
            if ratio_limit is not None and ratio_limit >= 0:
                kwargs["ratio_limit"] = ratio_limit
            result = client.torrents_add(**kwargs)
            logger.info(f"Added torrent {urls} (result: {result})")
            return True
        except Exception as e:
            raise QbitClientError(f"Failed to add torrent: {e}") from e

    def get_torrents(
        self,
        hashes: Optional[List[str]] = None,
        category: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> List[Any]:
        client = self.get_client()
        try:
            kwargs: Dict[str, Any] = {}
            if hashes:
                kwargs["torrent_hashes"] = hashes
            if category:
                kwargs["category"] = category
            if tag:
                kwargs["tag"] = tag
            return list(client.torrents_info(**kwargs))
        except Exception as e:
            raise QbitClientError(f"Failed to query torrents: {e}") from e

    def delete_torrents(self, torrent_hashes: List[str], delete_files: bool = True) -> None:
        if not torrent_hashes:
            return
        client = self.get_client()
        try:
            client.torrents_delete(delete_files=delete_files, torrent_hashes=torrent_hashes)
        except Exception as e:
            raise QbitClientError(f"Failed to delete torrents: {e}") from e

    def pause_torrents(self, torrent_hashes: List[str]) -> None:
        if not torrent_hashes:
            return
        client = self.get_client()
        try:
            client.torrents_pause(torrent_hashes=torrent_hashes)
        except Exception as e:
            raise QbitClientError(f"Failed to pause torrents: {e}") from e

    def resume_torrents(self, torrent_hashes: List[str]) -> None:
        if not torrent_hashes:
            return
        client = self.get_client()
        try:
            client.torrents_resume(torrent_hashes=torrent_hashes)
        except Exception as e:
            raise QbitClientError(f"Failed to resume torrents: {e}") from e

    def recheck_torrents(self, torrent_hashes: List[str]) -> None:
        if not torrent_hashes:
            return
        client = self.get_client()
        try:
            client.torrents_recheck(torrent_hashes=torrent_hashes)
        except Exception as e:
            raise QbitClientError(f"Failed to recheck torrents: {e}") from e

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

    def get_rule_match_times(
        self,
        pairs: List[Tuple[str, str]],
    ) -> Optional[Dict[Tuple[str, str], Optional[datetime]]]:
        unique_pairs = list(dict.fromkeys(pairs))
        result: Dict[Tuple[str, str], Optional[datetime]] = {pair: None for pair in unique_pairs}
        if not unique_pairs:
            return result

        log_lookup_succeeded = False
        try:
            client = self.get_client()
            logs = client.log_main(last_known_id=-1)
            log_lookup_succeeded = True
        except Exception as e:
            logger.debug(f"Could not search qBittorrent log for match events: {e}")
            logs = []

        try:
            reversed_logs = list(reversed(logs))
        except Exception as e:
            logger.debug(f"Could not iterate qBittorrent match events: {e}")
            reversed_logs = []
            log_lookup_succeeded = False

        for rule_name, release_title in unique_pairs:
            for entry in reversed_logs:
                try:
                    msg = entry.message
                except Exception as e:
                    logger.debug(f"Could not read qBittorrent match event: {e}")
                    continue
                if not isinstance(msg, str) or "is accepted by rule" not in msg or release_title not in msg:
                    continue
                try:
                    result[(rule_name, release_title)] = datetime.fromtimestamp(
                        entry.timestamp,
                        tz=timezone.utc,
                    )
                except Exception as e:
                    logger.debug(f"Could not parse qBittorrent match timestamp: {e}")
                else:
                    break

        unresolved = [pair for pair in unique_pairs if result[pair] is None]
        if not unresolved:
            return result

        rule_lookup_succeeded = False
        try:
            rules = self.get_rss_rules()
            rule_lookup_succeeded = True
        except Exception as e:
            logger.debug(f"Could not read rule lastMatch values: {e}")
            rules = {}

        for rule_name, release_title in unresolved:
            rule_def = rules.get(rule_name)
            if rule_def and rule_def.get("lastMatch"):
                try:
                    dt = parsedate_to_datetime(rule_def["lastMatch"])
                    result[(rule_name, release_title)] = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                except Exception as e:
                    logger.debug(f"Could not parse qBittorrent rule lastMatch: {e}")

        if not log_lookup_succeeded and not rule_lookup_succeeded:
            return None
        return result

    def get_rule_match_time(self, rule_name: str, release_title: str) -> Optional[datetime]:
        """
        Find the exact time when qBittorrent accepted/matched this release for this rule.
        1. Searches qBittorrent client application log for:
           'RSS article <release_title> is accepted by rule <rule_name>'
        2. If not in log (e.g. rolled over or client restarted), checks rule's lastMatch in qBittorrent.
        """
        try:
            client = self.get_client()
            logs = client.log_main(last_known_id=-1)
            for entry in reversed(logs):
                msg = entry.message
                if "is accepted by rule" in msg and release_title in msg:
                    return datetime.fromtimestamp(entry.timestamp, tz=timezone.utc)
        except Exception as e:
            logger.debug(f"Could not search qBittorrent log for match event: {e}")

        try:
            rules = self.get_rss_rules()
            rule_def = rules.get(rule_name)
            if rule_def and rule_def.get("lastMatch"):
                dt = parsedate_to_datetime(rule_def["lastMatch"])
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception as e:
            logger.debug(f"Could not read rule lastMatch: {e}")

        return None

