import logging
import math
import time

from fastapi import Request, Response, HTTPException

from mediaflow_proxy.drm.decrypter import decrypt_segment
from mediaflow_proxy.utils.crypto_utils import encryption_handler
from mediaflow_proxy.utils.http_utils import encode_mediaflow_proxy_url, get_original_scheme, ProxyRequestHeaders

logger = logging.getLogger(__name__)


async def process_manifest(
    request: Request, mpd_dict: dict, proxy_headers: ProxyRequestHeaders, key_id: str = None, key: str = None
) -> Response:
    """
    Processes the MPD manifest and converts it to an HLS manifest.

    Args:
        request (Request): The incoming HTTP request.
        mpd_dict (dict): The MPD manifest data.
        proxy_headers (ProxyRequestHeaders): The headers to include in the request.
        key_id (str, optional): The DRM key ID. Defaults to None.
        key (str, optional): The DRM key. Defaults to None.

    Returns:
        Response: The HLS manifest as an HTTP response.
    """
    hls_content = build_hls(mpd_dict, request, key_id, key)
    return Response(content=hls_content, media_type="application/vnd.apple.mpegurl", headers=proxy_headers.response)


async def process_playlist(
    request: Request, mpd_dict: dict, profile_id: str, proxy_headers: ProxyRequestHeaders
) -> Response:
    """
    Processes the MPD manifest and converts it to an HLS playlist for a specific profile.

    Args:
        request (Request): The incoming HTTP request.
        mpd_dict (dict): The MPD manifest data.
        profile_id (str): The profile ID to generate the playlist for.
        proxy_headers (ProxyRequestHeaders): The headers to include in the request.

    Returns:
        Response: The HLS playlist as an HTTP response.

    Raises:
        HTTPException: If the profile is not found in the MPD manifest.
    """
    matching_profiles = [p for p in mpd_dict["profiles"] if p["id"] == profile_id]
    if not matching_profiles:
        raise HTTPException(status_code=404, detail="Profile not found")

    hls_content = build_hls_playlist(mpd_dict, matching_profiles, request)
    return Response(content=hls_content, media_type="application/vnd.apple.mpegurl", headers=proxy_headers.response)


async def process_segment(
    init_content: bytes,
    segment_content: bytes,
    mimetype: str,
    proxy_headers: ProxyRequestHeaders,
    key_id: str = None,
    key: str = None,
) -> Response:
    """
    Processes and decrypts a media segment.

    Args:
        init_content (bytes): The initialization segment content.
        segment_content (bytes): The media segment content.
        mimetype (str): The MIME type of the segment.
        proxy_headers (ProxyRequestHeaders): The headers to include in the request.
        key_id (str, optional): The DRM key ID. Defaults to None.
        key (str, optional): The DRM key. Defaults to None.

    Returns:
        Response: The decrypted segment as an HTTP response.
    """
    if key_id and key:
        # For DRM protected content
        now = time.time()
        decrypted_content = decrypt_segment(init_content, segment_content, key_id, key)
        logger.info(f"Decryption of {mimetype} segment took {time.time() - now:.4f} seconds")
    else:
        # For non-DRM protected content, we just concatenate init and segment content
        decrypted_content = init_content + segment_content

    return Response(content=decrypted_content, media_type=mimetype, headers=proxy_headers.response)


def build_hls(mpd_dict: dict, request: Request, key_id: str = None, key: str = None) -> str:
    """
    Builds an HLS manifest from the MPD manifest.

    Args:
        mpd_dict (dict): The MPD manifest data.
        request (Request): The incoming HTTP request.
        key_id (str, optional): The DRM key ID. Defaults to None.
        key (str, optional): The DRM key. Defaults to None.

    Returns:
        str: The HLS manifest as a string.
    """
    hls = ["#EXTM3U", "#EXT-X-VERSION:6"]
    query_params = dict(request.query_params)
    has_encrypted = query_params.pop("has_encrypted", False)

    video_profiles = {}
    audio_profiles = {}

    # Get the base URL for the playlist_endpoint endpoint
    proxy_url = request.url_for("playlist_endpoint")
    proxy_url = str(proxy_url.replace(scheme=get_original_scheme(request)))

    for profile in mpd_dict["profiles"]:
        query_params.update({"profile_id": profile["id"], "key_id": key_id or "", "key": key or ""})
        playlist_url = encode_mediaflow_proxy_url(
            proxy_url,
            query_params=query_params,
            encryption_handler=encryption_handler if has_encrypted else None,
        )

        if "video" in profile["mimeType"]:
            video_profiles[profile["id"]] = (profile, playlist_url)
        elif "audio" in profile["mimeType"]:
            audio_profiles[profile["id"]] = (profile, playlist_url)

    # Add audio streams
    for i, (profile, playlist_url) in enumerate(audio_profiles.values()):
        is_default = "YES" if i == 0 else "NO"  # Set the first audio track as default
        hls.append(
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="{profile["id"]}",DEFAULT={is_default},AUTOSELECT={is_default},LANGUAGE="{profile.get("lang", "und")}",URI="{playlist_url}"'
        )

    # Add video streams
    for profile, playlist_url in video_profiles.values():
        hls.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={profile["bandwidth"]},RESOLUTION={profile["width"]}x{profile["height"]},CODECS="{profile["codecs"]}",FRAME-RATE={profile["frameRate"]},AUDIO="audio"'
        )
        hls.append(playlist_url)

    return "\n".join(hls)


def build_hls_playlist(mpd_dict: dict, profiles: list[dict], request: Request) -> str:
    """
    Builds an HLS playlist from the MPD manifest for specific profiles.

    Args:
        mpd_dict (dict): The MPD manifest data.
        profiles (list[dict]): The profiles to include in the playlist.
        request (Request): The incoming HTTP request.

    Returns:
        str: The HLS playlist as a string.
    """
    hls = ["#EXTM3U", "#EXT-X-VERSION:6"]

    added_segments = 0

    proxy_url = request.url_for("segment_endpoint")
    segment_proxy_endpoint_url = str(proxy_url.replace(scheme=get_original_scheme(request)))

    for index, profile in enumerate(profiles):
        segments = profile["segments"]
        if not segments:
            logger.warning(f"No segments found for profile {profile['id']}")
            continue

        # Add headers for only the first profile
        if index == 0:  # Headers for the first profile
            if not segments:  # Check if segments list is empty
                logger.warning(f"Profile {profile['id']} has no segments to build HLS playlist.")
                # Potresti voler sollevare un'eccezione o ritornare una playlist HLS vuota/errore
                # Invece di continuare e fallire con un KeyError sotto.
                # Per ora, lasciamo che fallisca se non ci sono segmenti e si tenta di accedervi.
                # Oppure, imposta valori di default:
                sequence = 0
                target_duration = 5  # Default target duration
            else:
                # Usa hls_media_sequence_num se esiste, altrimenti fallback a "number"
                sequence = segments[0].get("hls_media_sequence_num", segments[0].get("number", 0))

                extinf_values = [
                    f["extinf"] for f in segments if "extinf" in f and isinstance(f["extinf"], (float, int))
                ]
                if not extinf_values:  # Handle case where no valid extinf found
                    logger.warning(
                        f"No valid extinf values found for profile {profile['id']}. Using default target duration."
                    )
                    target_duration = 5  # Default target duration
                else:
                    target_duration = (
                        math.ceil(max(extinf_values)) if extinf_values else 5
                    )  # Default if list becomes empty

            hls.extend(
                [
                    f"#EXT-X-TARGETDURATION:{target_duration}",
                    f"#EXT-X-MEDIA-SEQUENCE:{sequence}",
                ]
            )
            if mpd_dict["isLive"]:
                hls.append("#EXT-X-PLAYLIST-TYPE:EVENT")
            else:
                hls.append("#EXT-X-PLAYLIST-TYPE:VOD")

        init_url = profile["initUrl"]

        query_params = dict(request.query_params)
        query_params.pop("profile_id", None)
        query_params.pop("d", None)
        has_encrypted = query_params.pop("has_encrypted", False)

        for segment in segments:
            # Add PROGRAM-DATE-TIME tag if available and it's a live stream
            if mpd_dict.get("isLive", False) and segment.get("program_date_time"):
                hls.append(f'#EXT-X-PROGRAM-DATE-TIME:{segment["program_date_time"]}')

            hls.append(f'#EXTINF:{segment["extinf"]:.3f},')

            # Prepare query parameters specifically for this segment's URL
            # Carry forward key_id, key, and api_password from the original playlist request's query parameters
            segment_url_query_params = {
                "init_url": init_url,
                "segment_url": segment["media"],
                "mime_type": profile["mimeType"],
                "key_id": request.query_params.get("key_id"),
                "key": request.query_params.get("key"),
                "api_password": request.query_params.get("api_password"),
            }
            # Filter out None values to keep the URL clean and avoid empty params
            segment_url_query_params = {k: v for k, v in segment_url_query_params.items() if v is not None}

            # Determine if the segment URL itself should be generated as an encrypted tokenized URL
            # The 'has_encrypted' flag should pertain to whether the incoming playlist URL was encrypted,
            # and if so, subsequent generated URLs (like segment URLs) should also be.
            # This depends on how `has_encrypted` is set and intended to be used by `encode_mediaflow_proxy_url`.
            # Assuming `has_encrypted` was correctly determined from the playlist request:
            current_has_encrypted_flag = request.query_params.get(
                "has_encrypted", False
            )  # Re-fetch from original request for clarity if needed

            hls.append(
                encode_mediaflow_proxy_url(
                    proxy_url,  # This is the base URL for the segment_endpoint (e.g., http://.../proxy/mpd/segment.mp4)
                    query_params=segment_url_query_params,
                    encryption_handler=encryption_handler if current_has_encrypted_flag else None,
                )
            )
            added_segments += 1

    if not mpd_dict["isLive"]:
        hls.append("#EXT-X-ENDLIST")

    logger.info(f"Added {added_segments} segments to HLS playlist")
    return "\n".join(hls)
