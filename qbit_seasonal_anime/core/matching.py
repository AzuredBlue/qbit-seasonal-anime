import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from guessit import guessit
from rapidfuzz import fuzz
from qbit_seasonal_anime.config import FUZZY_MATCH_THRESHOLD

logger = logging.getLogger("qbit_seasonal_anime.core.matching")


_CHARS_TO_REPLACE: str = r'\/:!*?"<>|._-'
VERSION_REGEX: re.Pattern[str] = re.compile(r"(E\d+|\b\d+)v\d+", re.IGNORECASE)
GUESSIT_OPTIONS: Dict[str, Any] = {"excludes": ["country", "language"]}


STOPWORDS: set = {"the", "a", "an", "no", "wa", "ga", "to", "de", "ni", "la", "le", "el"}


def normalize_title(title: str) -> str:
    """Normalize anime title for comparison by standardizing seasons, roman numerals, and symbols."""
    if not title:
        return ""
    # Lowercase
    t = title.lower()
    # Remove bracketed content like [1080p], (TV), etc.
    t = re.sub(r"\[.*?\]|\(.*?\)", " ", t)

    # Standardize roman numerals and seasons
    t = re.sub(r"\b(x)\b", "10", t)
    t = re.sub(r"\b(ix)\b", "9", t)
    t = re.sub(r"\b(viii)\b", "8", t)
    t = re.sub(r"\b(vii)\b", "7", t)
    t = re.sub(r"\b(vi)\b", "6", t)
    t = re.sub(r"\b(v)\b", "5", t)
    t = re.sub(r"\b(iv)\b", "4", t)
    t = re.sub(r"\b(iii)\b", "3", t)
    t = re.sub(r"\b(ii)\b", "2", t)
    t = re.sub(r"\b(\d+)(?:st|nd|rd|th)\s+season\b", r"season \1", t)
    t = re.sub(r"\bs(\d+)\b", r"season \1", t)

    # Replace separators, quotes, and punctuation with space
    t = re.sub(r"[:\-_/\\.|+~'\"`]", " ", t)
    # Collapse multiple whitespaces
    t = re.sub(r"\s+", " ", t).strip()
    return t


NON_GROUP_TAGS = {
    "1080p", "720p", "480p", "540p", "576p", "2160p", "4k",
    "hevc", "x264", "x265", "avc", "h264", "h265", "aac", "flac", "opus",
    "web-dl", "webrip", "hdtv", "dvd", "bd", "bluray", "cr",
    "multisub", "multi-sub", "multi-subs", "multisubs", "eng", "english",
    "raw", "raws", "sub", "subs", "dub", "dual-audio", "batch",
}


def extract_release_group_tag(raw_title: str) -> Optional[str]:
    """Extract release group tag whether at start [SubsPlease], at end [Varyg], or scene hyphen -VARYG."""
    # 1. Check start bracket
    m_start = re.match(r"^\[(.*?)\]", raw_title.strip())
    if m_start:
        cand = m_start.group(1).strip()
        if cand.lower() not in NON_GROUP_TAGS and not cand.isdigit():
            return cand

    # 2. Check scene hyphen group (e.g. H.264-VARYG, -VARYG, -Judgement)
    m_scene = re.search(r"-([A-Za-z0-9_]+)(?:\s*\(|\s*\[|\s*\.|\s*$)", raw_title)
    if m_scene:
        cand = m_scene.group(1).strip()
        if cand.lower() not in NON_GROUP_TAGS and len(cand) >= 2 and not cand.isdigit():
            return cand

    # 3. Check trailing brackets at the end
    brackets = re.findall(r"\[(.*?)\]", raw_title)
    if brackets:
        for b in reversed(brackets):
            val = b.strip()
            # Skip 8-char/4-char hex CRCs
            if re.fullmatch(r"[0-9A-Fa-f]{8}", val) or re.fullmatch(r"[0-9A-Fa-f]{4}", val):
                continue
            # Skip non-group tags and digits
            if val.lower() in NON_GROUP_TAGS or val.isdigit():
                continue
            return val
    return None


def parse_release_title(raw_title: str) -> Dict[str, Any]:
    """Parse torrent release filename/title into structured metadata."""
    if not raw_title:
        return {
            "raw_title": "",
            "title": "",
            "episode": None,
            "season": None,
            "release_group": None,
        }

    release_group = extract_release_group_tag(raw_title)
    cleaned = raw_title.strip()

    # Strip release group tag from title candidate
    if release_group:
        cleaned = re.sub(rf"^\[{re.escape(release_group)}\]\s*", "", cleaned)
        cleaned = re.sub(rf"\[{re.escape(release_group)}\]\s*", "", cleaned)
        cleaned = re.sub(rf"-{re.escape(release_group)}\b\s*", "", cleaned)

    # Clean version suffix e.g. E04v2 -> E04
    cleaned = VERSION_REGEX.sub(r"\1", cleaned)

    # Strip container extensions at the end
    cleaned = re.sub(r"\.(?:mkv|mp4|avi|webm|mov|m4v|ts)$", "", cleaned, flags=re.IGNORECASE).strip()

    # Pre-strip technical tags that contain numbers to prevent false episode extraction:
    # 1) Audio layouts: DDP 2.0, AAC 2.0, FLAC 2.0, 5.1, 7.1, 2.0, etc.
    cleaned = re.sub(
        r"\b(?:DDP?|EAC-?3|AC3|AAC|FLAC|OPUS|DTS(?:-HD)?|TrueHD|PCM|DD\+?|Dolby|Atmos)?\s*(?:[1-7]\.[0-2]|2\.0|5\.1|7\.1)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    # 2) Framerates: 143.8561fps, 60fps, 24fps
    cleaned = re.sub(r"\b\d+(?:\.\d+)?\s*fps\b", " ", cleaned, flags=re.IGNORECASE)
    # 3) Bit depths: 10-bit, 8-bit, 10bit
    cleaned = re.sub(r"\b\d{1,2}\s*-?\s*bits?\b", " ", cleaned, flags=re.IGNORECASE)
    # 4) Common video/source tags
    cleaned = re.sub(
        r"\b(?:2160p|1080p|1080i|720p|576p|540p|480p|4k|8k|UHD|FHD|HD|WEB-?DL|WEBRip|BDRip|BluRay|BD|HDTV|DVD(?:Rip)?|REMUX|Dual-Audio|Multi-Audio|Multi-Subs?|MultiSub|Uncensored|Batch|Complete)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )

    pipe_split = cleaned.split("|")
    primary_segment = pipe_split[0].strip()

    episode_num = None
    season_num = None

    # Step A: Check dual episode notation: e.g. "DIGIMON BEATBREAK - 38 (S01E38)" or "Title - 50 (S04E14)"
    m_dual = re.search(r"-\s*(\d{1,4})\s*\(\s*S(\d{1,2})E(\d{1,4})\s*\)", primary_segment, re.IGNORECASE)
    if m_dual:
        episode_num = int(m_dual.group(1))
        season_num = int(m_dual.group(2))
        title_candidate = primary_segment[:m_dual.start()].strip()
    else:
        # Step B: Check standard S01E08 or S1E8
        m_se = re.search(r"\bS(\d{1,2})\s*[-_.]?\s*E(\d{1,4})\b", primary_segment, re.IGNORECASE)
        if m_se:
            season_num = int(m_se.group(1))
            episode_num = int(m_se.group(2))
            title_candidate = primary_segment[:m_se.start()].strip()
        else:
            # Step C: Check "S2 - 08" or "Season 2 - 08"
            m_s_ep = re.search(r"\b(?:S|Season\s*)(\d{1,2})\s*-\s*(\d{1,4})\b", primary_segment, re.IGNORECASE)
            if m_s_ep:
                season_num = int(m_s_ep.group(1))
                episode_num = int(m_s_ep.group(2))
                title_candidate = primary_segment[:m_s_ep.start()].strip()
            else:
                # Step D: Check standalone episode notation e.g. " - 08" or " EP08" or " #08"
                # Exclude 4-digit years (1950..2050) from standalone episode matching unless prefixed by EP/E/#
                ep_matches = list(re.finditer(
                    r"(?:\s*-\s*|\s+)(?:EP?|#)?(\d{1,4})(?:\s*\(|\s*\[|\s*\.|\s*$)",
                    primary_segment,
                    re.IGNORECASE,
                ))
                valid_ep_matches = []
                for m in ep_matches:
                    val = int(m.group(1))
                    is_prefixed = bool(re.match(r"(?:EP?|#)", m.group(0).strip(" -_")))
                    if 1950 <= val <= 2050 and not is_prefixed:
                        continue
                    valid_ep_matches.append(m)

                if valid_ep_matches:
                    m_ep = valid_ep_matches[-1]
                    episode_num = int(m_ep.group(1))
                    title_candidate = primary_segment[:m_ep.start()].strip()
                else:
                    # Step E: Check standalone season notation without episode e.g. "Title S02" or "Title Season 2"
                    m_s_only = re.search(r"\b(?:S|Season\s*)(\d{1,2})\b", primary_segment, re.IGNORECASE)
                    if m_s_only:
                        season_num = int(m_s_only.group(1))
                    title_candidate = primary_segment

    # Clean title candidate:
    # 1. Remove bracketed content [tag] (tag)
    title_candidate = re.sub(r"\[.*?\]|\(.*?\)", "", title_candidate)
    # 2. Clean leftover years e.g. " 2026"
    title_candidate = re.sub(r"\b(?:19|20)\d{2}\b", "", title_candidate)
    # 3. Clean any trailing technical tags
    trail_tags = r"\b(?:2160p|1080p|1080i|720p|576p|540p|480p|4k|8k|UHD|FHD|HD|WEB-?DL|WEBRip|BDRip|BluRay|BD|HDTV|DVD(?:Rip)?|REMUX|AVC|HEVC|x264|x265|H\.?264|H\.?265|AV1|VP9|AAC|FLAC|OPUS|DDP?|EAC3|AC3|TrueHD|PCM|Dual-Audio|Multi-Audio|Multi-Subs?|MultiSub|DUAL|NF|CR|AMZN|HIDIVE|BILI|REPACK\d?|PROPER|Uncensored|Batch|Complete)\b"
    for _ in range(5):
        title_candidate = re.sub(trail_tags + r"[\s\-_|/:\(\[\{.]*$", "", title_candidate, flags=re.IGNORECASE).strip()
        title_candidate = re.sub(r"[\s\-_|/:\(\[\{.]+$", "", title_candidate).strip()

    title_candidate = re.sub(r"^[\s\-_|/:\)\]\}\.]+", "", title_candidate).strip()
    title_candidate = re.sub(r"\s+", " ", title_candidate).strip()

    if title_candidate:
        return {
            "raw_title": raw_title,
            "title": title_candidate,
            "episode": episode_num,
            "season": season_num,
            "release_group": release_group,
        }

    # Fallback to guessit pipeline if heuristics produced empty title
    try:
        clean_for_guessit = re.sub(r"(?: - Movie)|[\\/:!*?\"<>|._-](?!\s*\d)", " ", raw_title)
        clean_for_guessit = " ".join(clean_for_guessit.split())
        v_match = VERSION_REGEX.search(clean_for_guessit)
        if v_match:
            clean_for_guessit = clean_for_guessit.replace(v_match.group(0), v_match.group(1))

        guess = dict(guessit(clean_for_guessit, options=GUESSIT_OPTIONS))
    except Exception as e:
        logger.debug(f"Guessit failed to parse '{raw_title}': {e}")
        guess = {}

    g_episode = guess.get("episode")
    g_season = guess.get("season", "")
    g_part = str(guess.get("part", ""))
    remaining: List[int] = []

    # Handle digit in episode_title (e.g. 'episode_title': '02')
    if guess.get("episode_title", "").isdigit() and "episode" not in guess:
        g_episode = int(guess.get("episode_title"))

    # Handle multiple episode numbers (e.g. [86, 13] for Eighty-Six, [1, 2, 3] for Ranma 1/2)
    if isinstance(g_episode, list):
        remaining = g_episode[:-1]
        g_episode = g_episode[-1]

    # Handle multiple seasons (e.g. [2, 3] in S2 03)
    if isinstance(g_season, list):
        if g_episode is None and len(g_season) > 1:
            g_episode = g_season[-1]
        g_season = g_season[0]

    guessed_name = str(guess.get("title") or title_candidate or raw_title)
    if remaining:
        guessed_name += " " + " ".join(str(ep) for ep in remaining)
    if g_season and str(g_season).isdigit() and int(g_season) > 1 and f"Season {g_season}" not in guessed_name:
        guessed_name += f" Season {g_season}"
    if g_part and f"Part {g_part}" not in guessed_name:
        guessed_name += f" Part {g_part}"

    return {
        "raw_title": raw_title,
        "title": guessed_name,
        "episode": episode_num or g_episode,
        "season": season_num or (int(g_season) if str(g_season).isdigit() else None),
        "release_group": release_group or guess.get("release_group"),
    }


def calculate_match_score(parsed_title: str, aliases: List[str]) -> Tuple[float, Optional[str]]:
    """
    Calculate maximum fuzzy match score against a list of aliases.
    Returns (highest_score, best_matching_alias).
    """
    if not parsed_title or not aliases:
        return 0.0, None

    norm_parsed = normalize_title(parsed_title)
    if not norm_parsed:
        return 0.0, None

    tokens_parsed = [w for w in norm_parsed.split() if w not in STOPWORDS]
    if not tokens_parsed:
        return 0.0, None

    best_score = 0.0
    best_alias = None

    for alias in aliases:
        if not alias:
            continue
        norm_alias = normalize_title(alias)
        if not norm_alias:
            continue

        # Exact match
        if norm_parsed == norm_alias:
            return 100.0, alias

        # Token set ratio handles reordered words, season abbreviations, and subtitles
        score = float(fuzz.token_set_ratio(norm_parsed, norm_alias))

        # Guard against single short word triggers (e.g. "The" matching "The Apothecary Diaries")
        if len(tokens_parsed) == 1 and len(tokens_parsed[0]) < 5:
            score = float(fuzz.token_sort_ratio(norm_parsed, norm_alias))

        if score > best_score:
            best_score = score
            best_alias = alias

    return best_score, best_alias


UNWANTED_RESOLUTION_REGEX: re.Pattern[str] = re.compile(r"\b(720p|480p|540p|360p|576p)\b", re.IGNORECASE)
BATCH_REGEX: re.Pattern[str] = re.compile(
    r"\b(batch|complete\s+series|complete\s+season|collection)\b|\(\d+\s*[-~]\s*\d+\)|\[\d+\s*[-~]\s*\d+\]|\b\d{1,4}\s*~\s*\d{1,4}\b",
    re.IGNORECASE,
)


def is_valid_release(raw_title: str) -> bool:
    """Check if release title is 1080p and not a batch or unwanted lower resolution."""
    if UNWANTED_RESOLUTION_REGEX.search(raw_title):
        return False
    if BATCH_REGEX.search(raw_title):
        return False
    return True


def match_release_to_show(
    raw_title: str,
    aliases: List[str],
    threshold: float = FUZZY_MATCH_THRESHOLD,
) -> Tuple[bool, float, Dict[str, Any]]:
    """
    Evaluate if an RSS item or torrent matches an anime show.
    Rejects batches and lower resolutions (480p, 720p).
    Strictly matches against the show's Testing Regex pattern to ensure only true releases match.
    Returns (is_match, score, parsed_metadata).
    """
    parsed = parse_release_title(raw_title)
    if not is_valid_release(raw_title):
        return False, 0.0, parsed

    # 1. Primary: Match against the show's Testing Regex
    from qbit_seasonal_anime.core.rules import build_regex_pattern
    test_pattern = build_regex_pattern(aliases)
    try:
        m = re.search(test_pattern, raw_title, flags=re.IGNORECASE)
        if m:
            matched_token = m.group(0).strip()
            if matched_token:
                parsed["title"] = matched_token
            return True, 100.0, parsed
    except Exception as e:
        logger.debug(f"Testing regex match error: {e}")

    # 2. Secondary: Fuzzy fallback for minor spelling / punctuation / continuous season numbering
    parsed_title = parsed.get("title", "")
    score, best_alias = calculate_match_score(parsed_title, aliases)
    is_match = score >= threshold
    return is_match, score, parsed
