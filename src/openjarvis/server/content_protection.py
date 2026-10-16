"""Age-bound, two-stage human review for generated images.

Review bytes never enter the user's artifact store before approval. No model is
trusted as a content classifier. Unknown ages receive the protected policy.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
import uuid

from openjarvis.core.correlation import current_identity


class ContentReviewRequired(ValueError):
    def __init__(self, review_id):
        self.review_id = review_id
        super().__init__("Image is awaiting administrator content review")


class ContentProtection:
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS content_reviews (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, run_id TEXT NOT NULL,
                    digest TEXT NOT NULL, revision INTEGER NOT NULL,
                    request TEXT NOT NULL, state TEXT NOT NULL, image BLOB,
                    created REAL NOT NULL,
                    UNIQUE(owner, run_id, digest, revision));
                CREATE TABLE IF NOT EXISTS content_review_audit (
                    actor TEXT NOT NULL, review_id TEXT NOT NULL,
                    decision TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS reviewed_images (
                    owner TEXT NOT NULL, digest TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    PRIMARY KEY(owner, digest, revision));
            """)

    def expire(self):
        with self.store._connect() as db:
            db.execute(
                "UPDATE content_reviews SET image=NULL,state='expired' "
                "WHERE created<? AND state NOT IN ('expired','closed','rejected')",
                (time.time() - 7 * 86400,),
            )

    def policy(self, owner):
        user = self.store.get_user(owner)
        if not user or user["disabled"]:
            raise ValueError("Account is unavailable")
        return self.store.user_age(user) is None or self.store.user_age(
            user
        ) < 18, user["content_revision"]

    def image(self, owner, description, produce):
        protected, revision = self.policy(owner)
        if not protected:
            data = produce()
            # Age can change while the GPU is busy. Never release unchecked bytes.
            if self.policy(owner) != (False, revision):
                raise ValueError("Age settings changed; submit the image request again")
            return data
        identity = current_identity()
        if not identity or identity.user_id != owner:
            raise ValueError("Content review requires an authenticated workflow")
        request = json.dumps(description, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(request.encode()).hexdigest()
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE content_reviews SET image=NULL,state='expired' "
                "WHERE created<? AND state!='expired'",
                (time.time() - 7 * 86400,),
            )
            row = db.execute(
                "SELECT * FROM content_reviews WHERE owner=? AND "
                "run_id=? AND digest=? AND revision=?",
                (owner, identity.trace_id, digest, revision),
            ).fetchone()
            if row is None:
                count = db.execute(
                    "SELECT count(*) FROM content_reviews WHERE "
                    "state NOT IN ('rejected','expired','closed')"
                ).fetchone()[0]
                if count >= 32:
                    raise ValueError(
                        "Content review storage is full; contact an administrator"
                    )
                review_id = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO content_reviews "
                    "VALUES(?,?,?,?,?,?,'pending_request',NULL,?)",
                    (
                        review_id,
                        owner,
                        identity.trace_id,
                        digest,
                        revision,
                        request,
                        time.time(),
                    ),
                )
                state, data = "pending_request", None
            else:
                review_id, state, data = row["id"], row["state"], row["image"]
            if state == "request_approved":
                db.execute(
                    "UPDATE content_reviews SET state='generating' WHERE id=?",
                    (review_id,),
                )
        if state in {"pending_request", "pending_image"}:
            raise ContentReviewRequired(review_id)
        if state == "approved" and data is not None:
            if self.policy(owner) != (True, revision):
                raise ValueError("Age settings changed; submit the image request again")
            return data
        if state != "request_approved":
            raise ValueError(
                "Image review rejected, expired or interrupted; submit a new request"
            )
        try:
            data = produce()
            if len(data) > 20 * 1024**2:
                raise ValueError("Image exceeds review size limit")
            # Decode and re-encode the preview to prevent active/invalid content.
            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 16_777_216:
                    raise ValueError("Image exceeds review dimensions")
                output = io.BytesIO()
                image.convert("RGB").save(output, format="PNG")
                data = output.getvalue()
            if len(data) > 20 * 1024**2:
                raise ValueError("Image exceeds review size limit")
            if self.policy(owner) != (True, revision):
                raise ValueError("Age settings changed; submit the image request again")
            with self.store._connect() as db:
                db.execute(
                    "UPDATE content_reviews SET "
                    "image=?,state='pending_image' WHERE id=? AND state='generating'",
                    (data, review_id),
                )
        except Exception:
            with self.store._connect() as db:
                db.execute(
                    "UPDATE content_reviews SET image=NULL,state='rejected' WHERE id=?",
                    (review_id,),
                )
            raise
        raise ContentReviewRequired(review_id)

    def listing(self):
        self.expire()
        with self.store._connect() as db:
            return [
                {
                    **{
                        key: row[key]
                        for key in ("id", "owner", "run_id", "request", "state")
                    },
                    "age": self.store.user_age(row),
                }
                for row in db.execute(
                    "SELECT "
                    "r.id,r.owner,r.run_id,r.request,r.state,"
                    "u.birthdate,u.birthdate_timezone FROM content_reviews r "
                    "JOIN users u ON u.user_id=r.owner WHERE r.state IN "
                    "('pending_request','pending_image','generating') ORDER "
                    "BY r.created LIMIT 32"
                )
            ]

    def preview(self, review_id):
        self.expire()
        with self.store._connect() as db:
            row = db.execute(
                "SELECT image FROM content_reviews WHERE id=? AND "
                "state='pending_image'",
                (review_id,),
            ).fetchone()
        if not row or row[0] is None:
            raise ValueError("Review image is unavailable")
        return row[0]

    def decide(self, actor, review_id, stage, approve):
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store._require_admin(db, actor)
            reviewer = db.execute(
                "SELECT * FROM users WHERE user_id=?", (actor,)
            ).fetchone()
            if (
                self.store.user_age(reviewer) is None
                or self.store.user_age(reviewer) < 18
            ):
                raise PermissionError(
                    "Content reviewers must have an administrator-recorded adult age"
                )
            row = db.execute(
                "SELECT r.*,u.content_revision FROM content_reviews r JOIN users u "
                "ON u.user_id=r.owner WHERE r.id=?",
                (review_id,),
            ).fetchone()
            if (
                not row
                or row["state"] != stage
                or row["revision"] != row["content_revision"]
            ):
                raise ValueError("Review changed; refresh before deciding")
            if row["created"] < time.time() - 7 * 86400:
                raise ValueError("Review expired; submit a new request")
            if stage not in {"pending_request", "pending_image"}:
                raise ValueError("Invalid review stage")
            state = (
                ("request_approved" if stage == "pending_request" else "approved")
                if approve
                else "rejected"
            )
            db.execute(
                "UPDATE content_reviews SET state=?,image=? WHERE id=?",
                (state, row["image"] if approve else None, review_id),
            )
            if state == "approved":
                db.execute(
                    "INSERT OR IGNORE INTO reviewed_images VALUES (?,?,?)",
                    (
                        row["owner"],
                        hashlib.sha256(row["image"]).hexdigest(),
                        row["revision"],
                    ),
                )
            db.execute(
                "INSERT INTO content_review_audit VALUES (?,?,?,?)",
                (actor, review_id, state, time.time()),
            )
            return row["owner"], row["run_id"]

    def ready(self, review_id):
        self.expire()
        with self.store._connect() as db:
            row = db.execute(
                "SELECT state FROM content_reviews WHERE id=?", (review_id,)
            ).fetchone()
        return bool(
            row and row[0] in {"request_approved", "approved", "rejected", "expired"}
        )

    def finish(self, owner, run_id):
        with self.store._connect() as db:
            db.execute(
                "UPDATE content_reviews SET state='closed',image=NULL WHERE "
                "owner=? AND run_id=? AND state NOT IN ('rejected','expired')",
                (owner, run_id),
            )

    def check_3d(self, owner):
        if self.policy(owner)[0]:
            raise ValueError(
                "3D generation is unavailable for protected accounts until "
                "3D content review is supported"
            )

    def check_artifact(self, owner, metadata, data):
        image_file = (
            metadata["filename"]
            .lower()
            .endswith(
                (
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".gif",
                    ".webp",
                    ".bmp",
                    ".tif",
                    ".tiff",
                    ".svg",
                    ".ico",
                    ".avif",
                )
            )
        )
        self.check_download(owner, data, image_file=image_file)
        if (
            metadata["filename"].lower().endswith((".glb", ".gltf", ".stl", ".ply"))
            or data[:4] == b"glTF"
        ):
            self.check_3d(owner)

    def check_download(self, owner, data, *, image_file=False):
        if not self.policy(owner)[0]:
            return
        # Known image headers catch renamed generated files without making Pillow
        # a dependency of ordinary text downloads. Generation itself emits PNG.
        image_header = data.startswith(
            (
                b"\x89PNG",
                b"\xff\xd8\xff",
                b"GIF8",
                b"BM",
                b"II*\x00",
                b"MM\x00*",
                b"\x00\x00\x01\x00",
            )
        )
        image_header = image_header or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        image_header = image_header or b"<svg" in data[:512].lower()
        if not image_file and not image_header:
            return
        with self.store._connect() as db:
            row = db.execute(
                "SELECT 1 FROM reviewed_images WHERE owner=? AND digest=? "
                "AND revision=?",
                (owner, hashlib.sha256(data).hexdigest(), self.policy(owner)[1]),
            ).fetchone()
        if not row:
            raise ValueError(
                "This image has not been approved for this protected account"
            )
