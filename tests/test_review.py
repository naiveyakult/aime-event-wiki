from __future__ import annotations

from fastapi.testclient import TestClient

from event_wiki.review import create_app


class FakeReviewRepository:
    def __init__(self) -> None:
        self.patches = {
            "PATCH_1": {
                "patch_id": "PATCH_1",
                "status": "pending",
                "operation": "create_event",
                "event_id": "EVENT_1",
                "payload": {"title": "Synthetic quarterly earnings"},
                "audit": {"status": "WARN", "issues": [{"message": "Check number"}]},
                "evidence": [{"evidence_id": "DOC_1", "title": "Synthetic filing"}],
            }
        }
        self.actions: list[tuple] = []
        self.review_history = [{"decision": "edit", "reviewer": "alice"}]

    def list_pending_patches(self) -> list[dict]:
        return [patch for patch in self.patches.values() if patch["status"] == "pending"]

    def get_patch(self, patch_id: str) -> dict | None:
        return self.patches.get(patch_id)

    def get_review_context(self, patch_id: str) -> dict | None:
        patch = self.patches.get(patch_id)
        if patch is None:
            return None
        return {
            "patch": patch,
            "evidence": [
                {
                    "evidence_id": "DOC_1",
                    "title": "Synthetic filing",
                    "body": "Full synthetic evidence body.",
                }
            ],
            "current_event": {"event_title": "Existing event snapshot"},
            "review_history": self.review_history,
        }

    def review_patch(
        self, patch_id: str, decision: str, edited_payload=None, reviewer: str = "web"
    ) -> None:
        if self.patches[patch_id]["status"] != "pending":
            raise ValueError("patch is not pending")
        self.actions.append((decision, patch_id, edited_payload, reviewer))
        self.patches[patch_id]["status"] = {
            "approve": "approved",
            "reject": "rejected",
        }[decision]

    def reset_patch_for_rerun(self, patch_id: str, reviewer: str = "web") -> None:
        if self.patches[patch_id]["status"] != "pending":
            raise ValueError("patch is not pending")
        self.patches[patch_id]["status"] = "rerun_requested"
        self.actions.append(("rerun", patch_id, reviewer))


def test_review_pages_and_actions() -> None:
    repository = FakeReviewRepository()
    resumed: list[str] = []
    client = TestClient(create_app(repository, resume_callback=resumed.append))

    response = client.get("/reviews")
    assert response.status_code == 200
    assert "PATCH_1" in response.text

    response = client.get("/reviews/PATCH_1")
    assert response.status_code == 200
    assert "Synthetic filing" in response.text
    assert "Full synthetic evidence body" in response.text
    assert "Existing event snapshot" in response.text
    assert "alice" in response.text
    assert "Check number" in response.text

    response = client.post(
        "/reviews/PATCH_1/edit",
        data={"payload": '{"title":"Edited"}'},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert repository.actions[-1] == ("approve", "PATCH_1", {"title": "Edited"}, "web")
    assert resumed == ["PATCH_1"]


def test_reject_rerun_and_missing_patch() -> None:
    repository = FakeReviewRepository()
    resumed: list[str] = []
    client = TestClient(create_app(repository, resume_callback=resumed.append))

    response = client.post(
        "/reviews/PATCH_1/reject", data={"reason": "Unsupported"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert repository.actions[-1] == ("reject", "PATCH_1", {"review_reason": "Unsupported"}, "web")

    repository.patches["PATCH_1"]["status"] = "pending"
    response = client.post("/reviews/PATCH_1/rerun", follow_redirects=False)
    assert response.status_code == 303
    assert repository.actions[-1] == ("rerun", "PATCH_1", "web")
    assert resumed == ["PATCH_1", "PATCH_1"]
    assert repository.list_pending_patches() == []

    response = client.post("/reviews/PATCH_1/rerun", follow_redirects=False)
    assert response.status_code == 409

    assert client.get("/reviews/UNKNOWN").status_code == 404


def test_review_conflict_is_http_409() -> None:
    repository = FakeReviewRepository()
    repository.patches["PATCH_1"]["status"] = "approved"
    client = TestClient(create_app(repository))

    response = client.post("/reviews/PATCH_1/approve", follow_redirects=False)
    assert response.status_code == 409
    assert "not pending" in response.json()["detail"]


def test_non_loopback_access_requires_configured_review_token() -> None:
    repository = FakeReviewRepository()
    unprotected = TestClient(create_app(repository), base_url="http://review.example")
    assert unprotected.get("/reviews").status_code == 403

    protected = TestClient(
        create_app(repository, review_token="synthetic-review-token"),
        base_url="http://review.example",
    )
    assert protected.get("/reviews").status_code == 401
    response = protected.get("/reviews", headers={"Authorization": "Bearer synthetic-review-token"})
    assert response.status_code == 200
