import json

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_queue, get_session
from core.db import repo
from core.github.events import apply_installation_event, parse_review_request
from core.github.signature import verify_signature
from core.settings import get_settings

router = APIRouter()
log = structlog.get_logger()


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    queue: ArqRedis = Depends(get_queue),
    x_github_event: str = Header(),
    x_github_delivery: str = Header(),
    x_hub_signature_256: str | None = Header(None),
) -> JSONResponse:
    settings = get_settings()
    # Verify against the raw bytes: re-serialized JSON would not match the signature
    body = await request.body()
    secret = settings.github_webhook_secret.get_secret_value().encode()
    if not verify_signature(secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")

    payload = json.loads(body)
    action = payload.get("action")

    async with session.begin():
        is_new = await repo.record_webhook_event(session, x_github_delivery, x_github_event, action)
    if not is_new:
        return JSONResponse({"status": "duplicate"}, status_code=200)

    log.info("webhook.received", github_event=x_github_event, action=action, delivery=x_github_delivery)

    if x_github_event in ("installation", "installation_repositories"):
        async with session.begin():
            await apply_installation_event(session, x_github_event, payload)
        return JSONResponse({"status": "accepted"}, status_code=202)

    review = parse_review_request(x_github_event, payload, settings.bot_login)
    if review is None:
        return JSONResponse({"status": "ignored"}, status_code=202)
    if settings.kill_switch:
        log.warning("review.rejected.kill_switch", repo=review.repo_full_name, pr=review.pr_number)
        return JSONResponse({"status": "disabled"}, status_code=202)
    # TODO (Phase 4): per-installation daily quota and global spend cap (LLD §17)

    async with session.begin():
        # Upsert first: the App may have been installed before this API was running
        await repo.upsert_installation(session, review.installation_id, review.account_login, review.account_type)
        await repo.upsert_repo(session, review.repo_id, review.installation_id, review.repo_full_name, review.private)
        job, created = await repo.create_job(
            session, review.repo_id, review.pr_number, review.base_sha, review.head_sha, review.trigger
        )
        job_id = str(job.id)

    # Lets an older job for this PR notice it was superseded and cancel itself (LLD §6.2)
    await queue.set(f"pr:{review.repo_id}:{review.pr_number}:latest", review.head_sha)
    if created:
        await queue.enqueue_job(
            "run_review", job_id, _job_id=f"{review.repo_id}:{review.pr_number}:{review.head_sha}"
        )
    log.info("review.enqueued", job_id=job_id, repo=review.repo_full_name, pr=review.pr_number, new=created)
    return JSONResponse({"status": "queued", "job_id": job_id}, status_code=202)