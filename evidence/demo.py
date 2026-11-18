import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from evidence.api import create_app


def run():
    with tempfile.TemporaryDirectory() as directory:
        with TestClient(create_app(Path(directory) / "demo.sqlite3")) as client:
            subject = {"subject_id": "synthetic-01", "center": "synthetic-center",
                       "implanted_at": "2026-10-01T00:00:00Z", "ended_at": None,
                       "expected_version": 0, "reason": "本地合成样本"}
            response = client.put("/subjects/synthetic-01", json=subject)
            assert response.status_code == 200, response.text
            first = {"source": "synthetic-device", "session_id": "s1", "revision": 1,
                     "subject_id": "synthetic-01", "start": "2026-10-01T01:00:00Z",
                     "end": "2026-10-01T03:00:00Z", "mode": "control", "quality": [
                         {"start": "2026-10-01T01:00:00Z", "end": "2026-10-01T01:30:00Z", "reason": "合成噪声"}]}
            second = {**first, "session_id": "s2", "start": "2026-10-01T02:00:00Z",
                      "end": "2026-10-01T04:00:00Z", "quality": []}
            batch = {"request_id": "demo-import", "sessions": [first, second]}
            receipt = client.post("/sessions/batches", json=batch)
            assert receipt.status_code == 200, receipt.text
            assert client.post("/sessions/batches", json=batch).json() == receipt.json()
            query = {"start": "2026-10-01T00:00:00Z", "end": "2026-10-02T00:00:00Z"}
            result = client.post("/analysis", json=query).json()
            assert result["total"]["usage_seconds"] == 9000
            assert result["total"]["overlap_seconds"] == 3600
            assert result["total"]["quality_lost_seconds"] == 1800
            request = {"request_id": "demo-release", "expected_epoch": result["epoch"],
                       "query": query, "label": "合成演练"}
            published = client.post("/releases", json=request)
            assert published.status_code == 201, published.text
            release_id = published.json()["release_id"]
            corrected = {**first, "revision": 2, "withdrawn": True}
            correction = client.post("/sessions/batches", json={"request_id": "demo-correction", "sessions": [corrected]})
            assert correction.status_code == 200, correction.text
            current = client.post("/analysis", json=query).json()
            assert current["total"]["usage_seconds"] == 7200
            frozen = client.get("/releases/" + release_id).json()
            assert frozen["result"]["total"]["usage_seconds"] == 9000
            assert client.get(f"/releases/{release_id}/verify").json()["valid"]
            assert client.get("/integrity").json()["ok"]
            print(json.dumps({"release_id": release_id, "frozen_seconds": 9000,
                              "current_seconds": 7200, "epoch": current["epoch"]}, ensure_ascii=False))


if __name__ == "__main__":
    run()
