import asyncio
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union, Any

import aiofiles
import aiofiles.os
from pydantic import ValidationError

from mediaflow_proxy.speedtest.models import SpeedTestTask
from mediaflow_proxy.utils.http_utils import download_file_with_retry, DownloadError
from mediaflow_proxy.utils.mpd_utils import parse_mpd, parse_mpd_dict

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """Represents a cache entry with metadata."""

    data: bytes
    expires_at: float
    access_count: int = 0
    last_access: float = 0.0
    size: int = 0


class LRUMemoryCache:
    """Thread-safe LRU memory cache with support."""

    def __init__(self, maxsize: int):
        self.maxsize = maxsize
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._current_size = 0

    def get(self, key: str) -> Optional[CacheEntry]:
        with self._lock:
            if key in self._cache:
                entry = self._cache.pop(key)  # Remove and re-insert for LRU
                if time.time() < entry.expires_at:
                    entry.access_count += 1
                    entry.last_access = time.time()
                    self._cache[key] = entry
                    return entry
                else:
                    # Remove expired entry
                    self._current_size -= entry.size
                    self._cache.pop(key, None)
            return None

    def set(self, key: str, entry: CacheEntry) -> None:
        with self._lock:
            if key in self._cache:
                old_entry = self._cache[key]
                self._current_size -= old_entry.size

            # Check if we need to make space
            while self._current_size + entry.size > self.maxsize and self._cache:
                _, removed_entry = self._cache.popitem(last=False)
                self._current_size -= removed_entry.size

            self._cache[key] = entry
            self._current_size += entry.size

    def remove(self, key: str) -> None:
        with self._lock:
            if key in self._cache:
                entry = self._cache.pop(key)
                self._current_size -= entry.size


class HybridCache:
    """High-performance hybrid cache combining memory and file storage."""

    def __init__(
        self,
        cache_dir_name: str,
        ttl: int,
        max_memory_size: int = 100 * 1024 * 1024,  # 100MB default
        executor_workers: int = 4,
    ):
        self.cache_dir = Path(tempfile.gettempdir()) / cache_dir_name
        self.ttl = ttl
        self.memory_cache = LRUMemoryCache(maxsize=max_memory_size)
        self._executor = ThreadPoolExecutor(max_workers=executor_workers)
        self._lock = asyncio.Lock()

        # Initialize cache directories
        self._init_cache_dirs()

    def _init_cache_dirs(self):
        """Initialize sharded cache directories."""
        os.makedirs(self.cache_dir, exist_ok=True)

    def _get_md5_hash(self, key: str) -> str:
        """Get the MD5 hash of a cache key."""
        return hashlib.md5(key.encode()).hexdigest()

    def _get_file_path(self, key: str) -> Path:
        """Get the file path for a cache key."""
        return self.cache_dir / key

    async def get(self, key: str, default: Any = None) -> Optional[bytes]:
        """
        Get value from cache, trying memory first then file.

        Args:
            key: Cache key
            default: Default value if key not found

        Returns:
            Cached value or default if not found
        """
        key = self._get_md5_hash(key)
        # Try memory cache first
        entry = self.memory_cache.get(key)
        if entry is not None:
            return entry.data

        # Try file cache
        try:
            file_path = self._get_file_path(key)
            async with aiofiles.open(file_path, "rb") as f:
                metadata_size = await f.read(8)
                metadata_length = int.from_bytes(metadata_size, "big")
                metadata_bytes = await f.read(metadata_length)
                metadata = json.loads(metadata_bytes.decode())

                # Check expiration
                if metadata["expires_at"] < time.time():
                    await self.delete(key)
                    return default

                # Read data
                data = await f.read()

                # Update memory cache in background
                entry = CacheEntry(
                    data=data,
                    expires_at=metadata["expires_at"],
                    access_count=metadata["access_count"] + 1,
                    last_access=time.time(),
                    size=len(data),
                )
                self.memory_cache.set(key, entry)

                return data

        except FileNotFoundError:
            return default
        except Exception as e:
            logger.error(f"Error reading from cache: {e}")
            return default

    async def set(self, key: str, data: Union[bytes, bytearray, memoryview], ttl: Optional[int] = None) -> bool:
        """
        Set value in both memory and file cache.

        Args:
            key: Cache key
            data: Data to cache
            ttl: Optional TTL override

        Returns:
            bool: Success status
        """
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ValueError("Data must be bytes, bytearray, or memoryview")

        expires_at = time.time() + (ttl or self.ttl)

        # Create cache entry
        entry = CacheEntry(data=data, expires_at=expires_at, access_count=0, last_access=time.time(), size=len(data))

        key = self._get_md5_hash(key)
        # Update memory cache
        self.memory_cache.set(key, entry)
        file_path = self._get_file_path(key)
        temp_path = file_path.with_suffix(".tmp")

        # Update file cache
        try:
            metadata = {"expires_at": expires_at, "access_count": 0, "last_access": time.time()}
            metadata_bytes = json.dumps(metadata).encode()
            metadata_size = len(metadata_bytes).to_bytes(8, "big")

            async with aiofiles.open(temp_path, "wb") as f:
                await f.write(metadata_size)
                await f.write(metadata_bytes)
                await f.write(data)

            await aiofiles.os.rename(temp_path, file_path)
            return True

        except Exception as e:
            logger.error(f"Error writing to cache: {e}")
            try:
                await aiofiles.os.remove(temp_path)
            except:
                pass
            return False

    async def delete(self, key: str) -> bool:
        """Delete item from both caches."""
        self.memory_cache.remove(key)

        try:
            file_path = self._get_file_path(key)
            await aiofiles.os.remove(file_path)
            return True
        except FileNotFoundError:
            return True
        except Exception as e:
            logger.error(f"Error deleting from cache: {e}")
            return False


class AsyncMemoryCache:
    """Async wrapper around LRUMemoryCache."""

    def __init__(self, max_memory_size: int):
        self.memory_cache = LRUMemoryCache(maxsize=max_memory_size)

    async def get(self, key: str, default: Any = None) -> Optional[bytes]:
        """Get value from cache."""
        entry = self.memory_cache.get(key)
        return entry.data if entry is not None else default

    async def set(self, key: str, data: Union[bytes, bytearray, memoryview], ttl: Optional[int] = None) -> bool:
        """Set value in cache."""
        try:
            expires_at = time.time() + (ttl or 3600)  # Default 1 hour TTL if not specified
            entry = CacheEntry(
                data=data, expires_at=expires_at, access_count=0, last_access=time.time(), size=len(data)
            )
            self.memory_cache.set(key, entry)
            return True
        except Exception as e:
            logger.error(f"Error setting cache value: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """Delete item from cache."""
        try:
            self.memory_cache.remove(key)
            return True
        except Exception as e:
            logger.error(f"Error deleting from cache: {e}")
            return False


# Create cache instances
INIT_SEGMENT_CACHE = HybridCache(
    cache_dir_name="init_segment_cache",
    ttl=3600,  # 1 hour
    max_memory_size=500 * 1024 * 1024,  # 500MB for init segments
)

MPD_CACHE = AsyncMemoryCache(
    max_memory_size=100 * 1024 * 1024,  # 100MB for MPD files
)

SPEEDTEST_CACHE = HybridCache(
    cache_dir_name="speedtest_cache",
    ttl=3600,  # 1 hour
    max_memory_size=50 * 1024 * 1024,
)

EXTRACTOR_CACHE = HybridCache(
    cache_dir_name="extractor_cache",
    ttl=5 * 60,  # 5 minutes
    max_memory_size=50 * 1024 * 1024,
)


# Specific cache implementations
async def get_cached_init_segment(init_url: str, headers: dict) -> Optional[bytes]:
    """Get initialization segment from cache or download it."""
    # Try cache first
    cached_data = await INIT_SEGMENT_CACHE.get(init_url)
    if cached_data is not None:
        return cached_data

    # Download if not cached
    try:
        init_content = await download_file_with_retry(init_url, headers)
        if init_content:
            await INIT_SEGMENT_CACHE.set(init_url, init_content)
        return init_content
    except Exception as e:
        logger.error(f"Error downloading init segment: {e}")
        return None


async def get_cached_mpd(
    mpd_url: str,
    headers: dict,
    parse_drm: bool,
    parse_segment_profile_id: Optional[str] = None,
) -> dict:
    """Get MPD from cache or download and parse it."""
    # Try cache first
    cached_data = await MPD_CACHE.get(mpd_url)
    if cached_data is not None:
        try:
            mpd_dict_from_cache = json.loads(cached_data)
            # Re-process the cached MPD dictionary. This is important because
            # parse_segment_profile_id might be different, or for live streams,
            # the segment list needs to be regenerated based on current time.
            return parse_mpd_dict(mpd_dict_from_cache, mpd_url, parse_drm, parse_segment_profile_id)
        except json.JSONDecodeError:
            logger.warning(f"Failed to decode cached MPD for {mpd_url}. Deleting and refetching.")
            await MPD_CACHE.delete(mpd_url)
        except Exception as e:
            logger.error(f"Error processing cached MPD for {mpd_url}: {e}. Refetching.")
            # Potentially delete if the error is persistent with cached data
            await MPD_CACHE.delete(mpd_url)

    # Download and parse if not cached or if cached version was problematic
    try:
        mpd_content = await download_file_with_retry(mpd_url, headers)
        mpd_dict_fresh = parse_mpd(mpd_content)  # This is the raw dict from xmltodict

        # This processes mpd_dict_fresh and adds keys like 'isLive', 'minimumUpdatePeriod', etc.
        # and generates segment lists if parse_segment_profile_id is provided.
        parsed_dict_fresh = parse_mpd_dict(mpd_dict_fresh, mpd_url, parse_drm, parse_segment_profile_id)

        # Determine effective TTL for caching the raw manifest text (mpd_dict_fresh)
        mup = parsed_dict_fresh.get("minimumUpdatePeriod")  # float (e.g., 5.0 or 0.0) or None for VOD

        cache_ttl: Optional[float]
        if mup is not None and mup > 0:  # Live stream with a specific MUP
            cache_ttl = mup
        elif mup is not None and mup <= 0:  # Live stream with MUP <= 0 (e.g., "PT0S" from MPD)
            cache_ttl = 1.0  # Cache for a very short period (1 second) to force frequent updates
        else:  # mup is None (typically for VOD content where MUP is not applicable)
            cache_ttl = 3600.0  # Default to 1 hour for VOD or if MUP is unexpectedly None

        # Cache the raw mpd_dict_fresh (output of xmltodict) as a JSON string
        await MPD_CACHE.set(mpd_url, json.dumps(mpd_dict_fresh).encode(), ttl=cache_ttl)

        # Return the fully processed dictionary (parsed_dict_fresh), which might include
        # dynamically generated segment lists for live streams based on the fresh MPD.
        return parsed_dict_fresh
    except DownloadError as error:
        logger.error(f"Error downloading MPD from {mpd_url}: {error}")
        raise error  # Re-raise to be handled by the caller in handlers.py
    except Exception as error:
        logger.exception(f"Error processing MPD from {mpd_url}: {error}")
        # Consider not raising for general errors to allow graceful fallback if possible,
        # or re-raise if the error is critical. For now, re-raising.
        raise error


async def get_cached_speedtest(task_id: str) -> Optional[SpeedTestTask]:
    """Get speed test results from cache."""
    cached_data = await SPEEDTEST_CACHE.get(task_id)
    if cached_data is not None:
        try:
            return SpeedTestTask.model_validate_json(cached_data.decode())
        except ValidationError as e:
            logger.error(f"Error parsing cached speed test data: {e}")
            await SPEEDTEST_CACHE.delete(task_id)
    return None


async def set_cache_speedtest(task_id: str, task: SpeedTestTask) -> bool:
    """Cache speed test results."""
    try:
        return await SPEEDTEST_CACHE.set(task_id, task.model_dump_json().encode())
    except Exception as e:
        logger.error(f"Error caching speed test data: {e}")
        return False


async def get_cached_extractor_result(key: str) -> Optional[dict]:
    """Get extractor result from cache."""
    cached_data = await EXTRACTOR_CACHE.get(key)
    if cached_data is not None:
        try:
            return json.loads(cached_data)
        except json.JSONDecodeError:
            await EXTRACTOR_CACHE.delete(key)
    return None


async def set_cache_extractor_result(key: str, result: dict) -> bool:
    """Cache extractor result."""
    try:
        return await EXTRACTOR_CACHE.set(key, json.dumps(result).encode())
    except Exception as e:
        logger.error(f"Error caching extractor result: {e}")
        return False
