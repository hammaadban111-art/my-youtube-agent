"""
Uploads the finished video to YouTube automatically using a stored
refresh token, so no browser login is needed once set up (see README
for the one-time OAuth step).
"""
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from . import config, gha, quota, resilience

import string

STOPWORDS = frozenset({
    "all", "and", "any", "are", "but", "for", "from", "how", "into", "its",
    "not", "off", "of", "out", "per", "that", "the", "this", "via", "was",
    "were", "what", "when", "where", "who", "why", "with", "you", "your",
})


def build_tags(script: dict, niche: str) -> list[str]:
    """Builds a deduped, lowercased list of up to 15 YouTube tags from script metadata
    and niche terms, enforcing YouTube's per-tag (<=100 chars) and total length
    (<=500 chars including separators) limits."""
    if not isinstance(script, dict):
        script = {}
    if not isinstance(niche, str):
        niche = ""

    raw_candidates = []

    # 1. Topic subject (if present)
    topic = script.get("topic_subject")
    if topic and isinstance(topic, str) and topic.strip():
        topic_str = topic.strip()
        match = re.search(r'\s*\(([^)]+)\)\s*$', topic_str)
        if match:
            main_part = topic_str[:match.start()]
            paren_part = match.group(1)
        else:
            main_part = topic_str
            paren_part = None

        cleaned_main = main_part.strip(string.punctuation + " ")
        if cleaned_main:
            raw_candidates.append(cleaned_main)

        if paren_part:
            cleaned_paren = paren_part.strip(string.punctuation + " ")
            if cleaned_paren:
                raw_candidates.append(cleaned_paren)

    # 2. Niche terms split into words
    if niche:
        for word in niche.split():
            cleaned = word.strip(string.punctuation + " ")
            if cleaned:
                raw_candidates.append(cleaned)

    # 3. Hashtags in description (without leading #)
    desc = script.get("description")
    if desc and isinstance(desc, str):
        for raw_ht in re.findall(r'#([^\s#]+)', desc):
            cleaned_ht = raw_ht.lstrip('#').rstrip('.,!?')
            cleaned_ht = cleaned_ht.strip(string.punctuation + " ")
            if cleaned_ht:
                raw_candidates.append(cleaned_ht)

    seen = set()
    tags = []
    current_budget = 0

    for raw in raw_candidates:
        tag = raw.strip().lower()
        if not tag or len(tag) < 3 or len(tag) > 100:
            continue
        if '(' in tag or ')' in tag:
            continue
        if tag in STOPWORDS:
            continue
        if tag in seen:
            continue

        cost = len(tag) if not tags else (len(tag) + 1)
        if current_budget + cost > 500:
            break

        seen.add(tag)
        tags.append(tag)
        current_budget += cost

        if len(tags) == 15:
            break

    return tags


# A rendered video represents ~6 minutes of CI time and a Gemini call that
# came out of a 20/day quota. If the upload itself fails, throwing it away
# means paying all of that again — it's parked here for the next run instead.
PENDING_DIR = os.path.join(os.path.dirname(__file__), "..", "workdir", "pending_upload")
# Quota exhaustion (403 quotaExceeded) does not recover within a run; the
# daily YouTube quota resets at midnight Pacific. Retrying it just burns time.
NON_RETRYABLE_STATUS = {400, 401, 403, 404}

# A 403 is not one thing. quotaExceeded and every permission failure below are
# permanent for this run and for every run after it until a human acts; only a
# rateLimitExceeded 403 is worth waiting out, and that one is explicitly
# excluded from the non-retryable set below. Treating a missing scope as
# "transient" parked the video, said nothing, and let the next three slots
# repeat the same failure — which is what happened to the delete scope before
# can_delete() existed.
PERMANENT_403_REASONS = (
    "quotaExceeded",
    "forbidden",
    "insufficientPermissions",
    "insufficient authentication scopes",
    "authorizationRequired",
    "youtubeSignupRequired",
    "accountSuspended",
    "accountClosed",
    "uploadLimitExceeded",
    "videoLimitExceeded",
)
# The one 403 that genuinely clears on its own within a few seconds.
RETRYABLE_403_REASONS = ("rateLimitExceeded", "userRateLimitExceeded", "backendError")


class AmbiguousUploadError(RuntimeError):
    """The insert may have created a live video, but YouTube did not confirm it.

    ``videos.insert`` has no client-supplied idempotency key. Retrying this
    state is worse than parking it because it can create a second video.
    """

UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
# Reading the channel back — used only to answer "did that ambiguous insert
# actually land?" before a retry can publish the same video twice.
READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
# Deleting a video needs a WRITE scope over the channel, which youtube.upload
# is not - an upload-only token gets 403 "insufficient authentication scopes"
# on videos.delete. Kept separate from UPLOAD_SCOPE, the same way
# youtube_stats.py keeps ANALYTICS_SCOPE separate, so a token minted before
# this line existed still uploads fine instead of failing wholesale.
DELETE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"


def _credentials(scopes: list[str]) -> Credentials:
    return Credentials(
        token=None,
        refresh_token=config.YT_REFRESH_TOKEN,
        client_id=config.YT_CLIENT_ID,
        client_secret=config.YT_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=scopes,
    )


def _get_service():
    return build("youtube", "v3", credentials=_credentials([UPLOAD_SCOPE]))


# Either of these permits videos.delete. Probed in order.
DELETE_CAPABLE_SCOPES = (DELETE_SCOPE, "https://www.googleapis.com/auth/youtube")


def holds_scope(scope: str) -> tuple[bool, str]:
    """Whether the stored refresh token actually carries `scope`.

    There is no endpoint that just lists a refresh token's grant, and the two
    obvious shortcuts are both wrong - each was tried against the live token
    on 2026-08-08:

      * Refreshing with a narrow scope list and reading `granted_scopes` only
        ever echoes back what the refresh ASKED for. A token holding both
        upload and force-ssl reports one scope when asked for one. The check
        answers its own question and always says yes.
      * Refreshing with a WIDER list than the grant does not degrade
        gracefully - Google rejects the whole request with
        `invalid_scope: Bad Request`, so an over-broad probe reads as a dead
        token rather than as a missing scope.

    What is left is a probe: ask for exactly this one scope. A grant that
    contains it refreshes fine; one that does not fails with invalid_scope.
    Costs one token request, no quota units."""
    try:
        _credentials([scope]).refresh(Request())
    except RefreshError as e:
        if "invalid_scope" in str(e):
            return False, "not in the token's grant"
        return False, f"{type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    return True, "granted"


def delete_capable_scope() -> tuple[str | None, str]:
    """(the scope that can actually delete, why not if none can).

    Returns the SCOPE, not just a yes, because the preflight and the delete
    have to agree on it. can_delete() used to accept either scope in
    DELETE_CAPABLE_SCOPES while delete_video() always requested force-ssl, so
    a token holding only .../auth/youtube passed the preflight and then failed
    the delete — after the replacement was already live on the channel."""
    reasons = []
    for scope in DELETE_CAPABLE_SCOPES:
        ok, why = holds_scope(scope)
        if ok:
            return scope, f"token holds {scope}"
        # A dead or revoked token fails every probe for the same reason, and
        # that is worth reporting as itself rather than as a missing scope.
        if "invalid_grant" in why:
            return None, f"the YouTube token is expired or revoked: {why}"
        reasons.append(f"{scope.rsplit('/', 1)[-1]}: {why}")
    return None, (
        "the stored refresh token has no delete scope ("
        + "; ".join(reasons)
        + "). Re-run get_refresh_token.py to mint one that does, then update "
        "the YT_REFRESH_TOKEN secret."
    )


def can_delete() -> tuple[bool, str]:
    """Whether the stored token can delete a video, and why not if it can't.
    Checked BEFORE a re-upload renders or uploads anything: finding out after
    the new video is live means 1,600 units spent on a replacement that cannot
    replace anything."""
    scope, why = delete_capable_scope()
    return scope is not None, why


def delete_video(video_id: str, scope: str = None) -> None:
    """Deletes one video from the channel. Costs 50 quota units, booked on the
    same ledger as everything else.

    `scope` is the one delete_capable_scope() proved the token holds. Passing
    it is how the caller guarantees the delete uses the same grant its
    preflight checked; without one this probes, and falls back to force-ssl
    only when the probe itself could not run."""
    if scope is None:
        scope = delete_capable_scope()[0] or DELETE_SCOPE
    youtube = build("youtube", "v3", credentials=_credentials([scope]))
    try:
        youtube.videos().delete(id=video_id).execute()
    except HttpError:
        # The request reached the API, so the units are spent whatever the
        # verdict was. Booking them only on success would let a run of 403s
        # spend real quota the ledger never sees.
        quota.record_units(quota.UNITS_PER_DELETE)
        raise
    quota.record_units(quota.UNITS_PER_DELETE)


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, AmbiguousUploadError):
        return False
    if isinstance(error, RefreshError):
        return False
    if isinstance(error, HttpError):
        status = getattr(error.resp, "status", 500)
        # str(error) extracts the reason/body where the API's own reason lives.
        text = str(error)
        if status == 403:
            # Split before the blanket status check: a rate-limit 403 is the
            # one that clears on its own, everything else is permanent and
            # retrying it burns the run's time to reach the same answer.
            return any(reason in text for reason in RETRYABLE_403_REASONS)
        if status in NON_RETRYABLE_STATUS:
            return False
        if "quotaExceeded" in text:
            return False
    return True


def _permission_failure(error: Exception) -> str | None:
    """The API's own reason when a 403 means "this token may not do that", or
    None. Named so the alert can tell a missing scope from a spent quota —
    they need opposite responses from a human."""
    if not isinstance(error, HttpError):
        return None
    if getattr(error.resp, "status", None) != 403:
        return None
    text = str(error)
    if "quotaExceeded" in text:
        return None
    for reason in PERMANENT_403_REASONS:
        if reason in text:
            return reason
    return None


def _landed_upload_id(title: str, since: datetime) -> str | None:
    """The id of a video this run's insert actually created, if one exists.

    THE FAILURE THIS EXISTS FOR. `videos.insert` is not idempotent. A 500, a
    503 or a dropped connection AFTER YouTube has created the video returns an
    error to us while the video is live, and the retry ladder then uploads the
    same render a second time. That is a duplicate on the channel, a second
    upload slot, and two records for one story.

    So before a retry is allowed, ask the channel. Two Data API units
    (channels.list + playlistItems.list) against a 10,000/day cap, and only on
    the failure path. Returns None on ANY doubt — a token without the read
    scope, a network failure, no match — because "cannot tell" must fall back
    to the existing behaviour rather than skip an upload that never happened.
    """
    if not config.YT_REFRESH_TOKEN:
        return None
    try:
        youtube = build("youtube", "v3", credentials=_credentials([READ_SCOPE]))
        channels = youtube.channels().list(part="contentDetails", mine=True).execute()
        quota.record_units(1)
        items = channels.get("items") or []
        uploads = ((items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}
                   ).get("uploads") if items else None
        if not uploads:
            return None
        recent = youtube.playlistItems().list(
            part="snippet,contentDetails", playlistId=uploads, maxResults=10).execute()
        quota.record_units(1)
        wanted = (title or "")[:100].strip()
        for item in recent.get("items") or []:
            snippet = item.get("snippet") or {}
            details = item.get("contentDetails") or {}
            if (snippet.get("title") or "").strip() != wanted:
                continue
            stamp = details.get("videoPublishedAt") or snippet.get("publishedAt") or ""
            try:
                published = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            # Only a video created after this attempt started can be ours.
            if published >= since - timedelta(minutes=1):
                return details.get("videoId") or snippet.get("resourceId", {}).get("videoId")
    except Exception as e:  # noqa: BLE001 - a probe that cannot run answers "unknown"
        print(f"[upload] could not check whether the failed insert landed "
              f"({type(e).__name__}: {e}) — assuming it did not")
    return None


def parked_uploads(pending_dir: str = None) -> list[dict]:
    """Every rendered-but-unuploaded video waiting in `pending_dir`, oldest
    first. Each entry carries `_meta_path` and `_video_path` so a recovery run
    can publish it without re-deriving either."""
    pending_dir = pending_dir or PENDING_DIR
    if not os.path.isdir(pending_dir):
        return []
    found = []
    for name in sorted(os.listdir(pending_dir)):
        if not name.endswith(".json") or name.endswith(".claimed.json"):
            continue
        meta_path = os.path.join(pending_dir, name)
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict) or not meta.get("title") or not meta.get("video_file"):
            continue
        video_path = os.path.join(pending_dir, meta["video_file"])
        if not os.path.exists(video_path):
            continue
        meta["_meta_path"] = meta_path
        meta["_video_path"] = video_path
        meta["_claim_path"] = meta_path[: -len(".json")] + ".claimed.json"
        found.append(meta)
    return sorted(found, key=lambda m: m.get("parked_at", ""))


def claim_parked(meta: dict) -> bool:
    """Marks one parked bundle as being published right now.

    False means an earlier attempt already claimed it and may have got as far
    as a live video before dying. Recovering a parked video is exactly the
    place a crash mid-publish turns into a duplicate on the channel, so the
    marker is written BEFORE the upload and is never cleared automatically —
    a second attempt has to be a deliberate one."""
    claim_path = meta.get("_claim_path")
    if not claim_path:
        return False
    try:
        # O_EXCL turns this into an atomic claim when a manual recovery and a
        # scheduled run both see the same cached parked bundle.
        fd = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"claimed_at": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                       "parked_at": meta.get("parked_at"),
                       "bundle_id": meta.get("bundle_id"),
                       "writer": quota.writer_id()}, f, indent=2)
    except Exception:
        try:
            os.unlink(claim_path)
        except OSError:
            pass
        raise
    return True


def release_parked_claim(meta: dict) -> None:
    """Removes a claim written by claim_parked().

    Only for an attempt that PROVABLY created no video: YouTube rejected the
    insert outright, or the attempt failed before an insert was ever sent.
    Leaving the claim behind in that case wedged the bundle forever — every
    later run reported "claimed by an earlier attempt, may be live" about a
    render that was never uploaded, and the recovery path never reached it."""
    claim_path = meta.get("_claim_path")
    if not claim_path:
        return
    try:
        os.remove(claim_path)
    except FileNotFoundError:
        pass


def flag_parked_for_reconciliation(meta: dict, reason: str) -> None:
    """Marks a parked bundle as possibly live (the insert's outcome is unknown).

    Written into the bundle's OWN metadata rather than into a second parked
    copy: park_for_next_run() used to duplicate the render on every failed
    recovery, and the untouched original — still claimed — then sat in front
    of the copy forever."""
    meta_path = meta.get("_meta_path")
    if not meta_path:
        return
    try:
        with open(meta_path) as f:
            stored = json.load(f)
        if not isinstance(stored, dict):
            return
        stored["requires_manual_reconciliation"] = True
        stored["reason"] = str(reason)[:300]
        tmp = meta_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(stored, f, indent=2)
        os.replace(tmp, meta_path)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[upload] could not flag {os.path.basename(meta_path)} for "
              f"reconciliation ({type(e).__name__}: {e})")


def park_for_next_run(video_path: str, title: str, description: str, reason: str,
                      script: dict = None, grounding: dict = None,
                      prediction: dict = None,
                      requires_manual_reconciliation: bool = False) -> str:
    """Saves a rendered-but-unuploaded video plus its metadata so a later run
    can publish it instead of the work being lost.

    `script` is the whole script dict, and leaving it out is expensive: when
    six videos were recovered on 2026-08-08 the parked files held only title,
    description and reason, so topic_subject, the segments and the grounding
    report were gone. topic_subject had to be scraped back out of the Actions
    log (the grounding step prints the article name), and without it a
    recovered video cannot be duplicate-checked, cannot be tagged properly and
    carries no signal for predict.py. Two of those six turned out to be the
    same subject precisely because nothing recorded the first one."""
    os.makedirs(PENDING_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Two failures can arrive in one second.  A random suffix prevents one
    # parked render from silently overwriting the other.
    identity = f"{stamp}-{uuid.uuid4().hex[:8]}"
    parked_video = os.path.join(PENDING_DIR, f"{identity}.mp4")
    shutil.copyfile(video_path, parked_video)
    payload = {"title": title, "description": description,
               "video_file": os.path.basename(parked_video),
               "parked_at": stamp, "bundle_id": identity,
               "reason": reason[:300],
               "requires_manual_reconciliation": bool(requires_manual_reconciliation)}
    if script:
        payload["topic_subject"] = script.get("topic_subject", "")
        payload["script"] = script
        # Provenance travels with the render or it is gone: a recovered video
        # with no story_id cannot be reconciled against the weekly packet's
        # ledger, and a re-park would lose it a second time.
        for field in ("story_id", "packet_id", "sources", "verification"):
            if script.get(field):
                payload[field] = script[field]
    if grounding:
        # The fact-check is part of the record the recovery run has to write.
        # Without it a recovered video shows blanks that read as a bug.
        payload["grounding"] = grounding
    if prediction:
        payload["prediction"] = prediction
    with open(os.path.join(PENDING_DIR, f"{identity}.json"), "w") as f:
        json.dump(payload, f, indent=2)
    return parked_video


def _book(what: str, fn, *args) -> None:
    """Runs one quota-ledger write without letting it raise.

    The ledger writes sit INSIDE the upload attempt that resilience.retry()
    wraps, so an exception from them looked exactly like a failed insert. A
    disk or JSON error while booking a SUCCESSFUL upload therefore sent the
    ladder round again and inserted the same render a second time — a
    duplicate video caused by bookkeeping. The ledger is self-tracked and
    reconciled from the records (quota.reconcile_uploads), so a missed write
    is recoverable; a second live copy is not."""
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001 - bookkeeping must not re-run an insert
        print(f"[upload] could not book {what} in the quota ledger "
              f"({type(e).__name__}: {e}) — continuing")


# YouTube's numeric ids for the categories a story's metadata can name. Every
# upload used to hardcode "22" (People & Blogs) while every packet story
# declared "Education", so the declared category was silently dropped.
CATEGORY_IDS = {
    "film & animation": "1",
    "travel & events": "19",
    "people & blogs": "22",
    "entertainment": "24",
    "news & politics": "25",
    "howto & style": "26",
    "education": "27",
    "science & technology": "28",
}
DEFAULT_CATEGORY_ID = "22"


def category_id(script: dict = None) -> str:
    """The YouTube categoryId for a story's declared metadata.category, or
    People & Blogs when it names nothing this table knows."""
    name = str(((script or {}).get("metadata") or {}).get("category") or "")
    return CATEGORY_IDS.get(name.strip().lower(), DEFAULT_CATEGORY_ID)


def upload_video(video_path: str, title: str, description: str, tags: list[str] = None,
                 script: dict = None, grounding: dict = None,
                 prediction: dict = None, park_on_failure: bool = True):
    """Uploads with backoff. Raises only after the video has been safely
    parked, so a failed upload costs a slot rather than the whole render.

    `park_on_failure=False` is for a render that is ALREADY parked (recovery):
    parking it again would copy the same video into a second bundle, and the
    caller settles the original bundle itself."""
    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags or [],
            "categoryId": category_id(script),
        },
        "status": {"privacyStatus": config.PRIVACY_STATUS, "selfDeclaredMadeForKids": False},
    }

    def _do_upload():
        started = datetime.now(timezone.utc)
        youtube = _get_service()
        # MediaFileUpload is rebuilt per attempt: a partially-consumed upload
        # object cannot be safely replayed after a failure.
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True,
                                mimetype="video/mp4")
        request = youtube.videos().insert(part="snippet,status", body=body,
                                          media_body=media)
        try:
            response = request.execute()
            video_id = response["id"]
        except Exception as e:  # noqa: BLE001 - execute can lose its response
            # An insert that reached the API may have consumed one of the
            # 100/day upload slots even though no video came back. Booked per
            # ATTEMPT, because three failed attempts would really cost three -
            # only the successful one is keyed by video id below, where
            # idempotency is what matters.
            _book("a failed insert", quota.record_failed_upload)
            # ...and it may also have CREATED THE VIDEO. An error that arrives
            # after YouTube accepted the insert is indistinguishable from one
            # that arrives before it, so the only safe thing to do before
            # retrying is ask the channel which of the two happened.
            if _is_retryable(e):
                landed = _landed_upload_id(title, started)
                if landed:
                    _book(f"upload {landed}", quota.record_upload, landed)
                    resilience.record_degradation(
                        "youtube-upload",
                        f"the insert reported {type(e).__name__} but the video "
                        f"is on the channel as {landed}",
                        "adopted the video that already exists instead of "
                        "uploading a duplicate",
                    )
                    return landed
                # No idempotency key exists for videos.insert. A read probe
                # that cannot prove the outcome is not permission to retry;
                # it is a human-reconciliation state. Mark the parked bundle
                # so automatic recovery cannot duplicate a possibly-live video.
                raise AmbiguousUploadError(
                    "YouTube did not confirm whether the upload landed. It was "
                    "not retried because videos.insert is not idempotent; check "
                    "the channel before publishing the parked render. Original "
                    f"error: {type(e).__name__}: {e}") from e
            raise

        _book(f"upload {video_id}", quota.record_upload, video_id)
        return video_id

    def _should_retry(error: Exception) -> bool:
        if not _is_retryable(error):
            return False
        # Every failed attempt books an upload slot, so the ladder can walk the
        # day's allowance down to nothing and then keep knocking. Re-read the
        # ledger between attempts rather than trusting the check made before
        # the first one.
        if not quota.can_upload():
            print(f"[upload] {quota.uploads_today()}/{quota.UPLOADS_PER_DAY_CAP} "
                  "upload slots are gone — not retrying")
            return False
        return True

    try:
        # Inside the try, so a refusal here parks the render like any other
        # failed upload. Raised before it, the ~7-minute render was simply
        # thrown away — the opposite of what this function promises.
        if not quota.can_upload():
            raise RuntimeError(
                f"No YouTube upload slots remain today: {quota.uploads_today()} of "
                f"{quota.UPLOADS_PER_DAY_CAP} are already booked.")
        return resilience.retry(
            _do_upload, label="youtube upload", attempts=3, base_delay=10.0,
            retry_if=_should_retry
        )
    except Exception as e:  # noqa: BLE001 - park before re-raising
        reason = f"{type(e).__name__}: {e}"
        retryable = _is_retryable(e)
        permission_reason = _permission_failure(e)

        # Only PERMANENT failures are annotated. A transient one is already
        # parked and the next scheduled run picks it up, so flagging it would
        # train the reader to ignore the annotation that actually matters.
        if not retryable:
            message = f"The upload of '{title}' failed permanently.\n\n{reason}"
            if isinstance(e, RefreshError) or "invalid_grant" in reason:
                message += (
                    "\n\nThis is the 7-day OAuth expiry: the YouTube login is "
                    "dead and no run will publish anything until it is "
                    "replaced. Re-run get_refresh_token.py and update the "
                    "YT_REFRESH_TOKEN secret. Switching the Google OAuth "
                    "consent screen from Testing to In production stops it "
                    "recurring."
                )
            elif permission_reason:
                # A permission 403 does not heal. Parking the video and saying
                # nothing let three more slots fail exactly the same way before
                # anybody looked.
                message += (
                    f"\n\nYouTube refused this upload with '{permission_reason}'. "
                    "That is a PERMISSION failure, not a busy server: every "
                    "scheduled run will fail the same way until a human acts. "
                    "Check that YT_REFRESH_TOKEN still carries "
                    f"{UPLOAD_SCOPE}, that the channel is in good standing, "
                    "and re-mint the token with scripts/remint_yt_token.py if "
                    "the grant is short. The rendered video is parked, not "
                    "lost — scripts/publish_parked.py publishes it once the "
                    "permission is back."
                )
            gha.error("Upload failed permanently", message)

        if not park_on_failure:
            resilience.record_degradation(
                "youtube-upload",
                f"upload failed after retries ({'transient' if retryable else 'permanent'}): {reason}",
                "left the already-parked render where it was",
            )
            raise
        parked = park_for_next_run(
            video_path, title, description, reason, script=script,
            grounding=grounding, prediction=prediction,
            requires_manual_reconciliation=isinstance(e, AmbiguousUploadError),
        )
        resilience.record_degradation(
            "youtube-upload",
            f"upload failed after retries ({'transient' if retryable else 'permanent'}): {reason}",
            f"video parked at {os.path.basename(parked)} for a later run",
        )
        raise


if __name__ == "__main__":
    with open(f"{config.WORKDIR}/script.json") as f:
        script = json.load(f)
    video_id = upload_video(
        f"{config.WORKDIR}/final_video.mp4",
        script["title"],
        script["description"],
    )
    print(f"Uploaded: https://youtube.com/watch?v={video_id}")
