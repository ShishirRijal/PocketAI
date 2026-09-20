import pytest
from fastapi.testclient import TestClient

from pocket.main import create_app
from pocket.runtime import build_runtime
from pocket.services.demo import seed_demo

H = {"Authorization": "Bearer secret"}


@pytest.fixture
def client(settings, services):
    rt = build_runtime(settings, services=services)
    seed_demo(services.db, months=3, user_id=services.user_id)
    app = create_app(settings, runtime=rt, run_worker=False, run_scheduler=False)
    with TestClient(app) as c:
        yield c


def test_auth(client):
    assert client.get("/api/v1/me").status_code == 401
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/login"
    r = client.post("/app/login", data={"token": "wrong"}, follow_redirects=False)
    assert "error" in r.headers["location"]
    r = client.post("/app/login", data={"token": "secret"}, follow_redirects=False)
    assert "pocket_session" in r.cookies
    assert client.get("/api/v1/me").json()["base_currency"] == "EUR"  # cookie now set
    assert "<title>Pocket</title>" in client.get("/app").text
    client.cookies.set("pocket_session", "1.9999999999.forged")
    assert client.get("/api/v1/me").status_code == 401


def test_token_link_keeps_query(client):
    r = client.get("/app?token=secret&tab=transactions&period=last_month", follow_redirects=False)
    assert r.headers["location"] == "/app?tab=transactions&period=last_month"


def test_summary_consistent_with_transactions(client):
    s = client.get("/api/v1/summary?period=last_90_days", headers=H).json()
    t = client.get("/api/v1/transactions?period=last_90_days&page_size=500", headers=H).json()
    assert s["total_minor"] == t["total_minor"] == sum(x["amount_base_minor"] for x in t["items"])
    assert s["count"] == t["total"]
    assert sum(c["total_minor"] for c in s["by_category"]) == s["total_minor"]
    assert sum(p["total_minor"] for p in s["series"]["points"]) == s["total_minor"]
    assert len(s["months"]) == 12


def test_filters(client):
    facets = client.get("/api/v1/facets", headers=H).json()
    groceries = next(c for c in facets["categories"] if c["name"] == "Groceries")
    r = client.get(
        f"/api/v1/transactions?period=all_time&category={groceries['id']}&page_size=500", headers=H
    ).json()
    assert r["total"] > 0 and all(x["category"] == "Groceries" for x in r["items"])
    r = client.get("/api/v1/transactions?period=all_time&q=rimi&page_size=500", headers=H).json()
    assert all(
        "rimi" in ((x["merchant"] or "") + " ".join(t["name"] for t in x["tags"])).lower()
        for x in r["items"]
    )
    r = client.get("/api/v1/transactions?period=all_time&direction=income", headers=H).json()
    assert all(x["direction"] == "income" for x in r["items"])
    r = client.get(
        "/api/v1/transactions?period=all_time&min_amount=100&direction=all&page_size=500", headers=H
    ).json()
    assert all(x["amount_base_minor"] >= 10000 for x in r["items"])
    r = client.get(
        "/api/v1/transactions?period=all_time&sort=amount&order=desc&page_size=2", headers=H
    ).json()
    assert r["items"][0]["amount_base_minor"] >= r["items"][1]["amount_base_minor"]
    r = client.get("/api/v1/transactions?start=2026-09-01&end=2026-09-01", headers=H).json()
    assert "Sep 01, 2026" in r["range"]["label"]


def test_edit_delete_restore(client):
    t = client.get("/api/v1/transactions?period=all_time&page_size=1", headers=H).json()["items"][0]
    r = client.patch(
        f"/api/v1/transactions/{t['id']}",
        json={"amount": 99.5, "note": "edited", "tags": ["x", "y"]},
        headers=H,
    )
    assert r.status_code == 200
    assert r.json()["amount_minor"] == 9950 and r.json()["note"] == "edited"
    detail = client.get(f"/api/v1/transactions/{t['id']}", headers=H).json()
    assert any(v["reason"] == "dashboard edit" for v in detail["versions"])
    assert client.delete(f"/api/v1/transactions/{t['id']}", headers=H).json() == {"deleted": True}
    assert client.get(f"/api/v1/transactions/{t['id']}", headers=H).json()["deleted"] is True
    client.post(f"/api/v1/transactions/{t['id']}/restore", headers=H)
    assert client.get(f"/api/v1/transactions/{t['id']}", headers=H).json()["deleted"] is False


def test_export_csv(client):
    r = client.get("/api/v1/export.csv?period=all_time", headers=H)
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("id,date,amount,currency")
    assert len(lines) > 10


def test_system(client):
    r = client.get("/api/v1/system", headers=H).json()
    assert "chains" in r and "rules/v1" in r["chains"]["extract"]


def test_quick_log(client):
    r = client.post("/api/v1/say", json={"text": "coffee 6.50 and metro 2"}, headers=H).json()
    assert r["replies"][0]["text"].startswith("Two transactions?")
    assert [o["value"] for o in r["replies"][0]["options"]][:1] == ["yes"]
    r = client.post("/api/v1/say", json={"text": "yes"}, headers=H).json()
    assert "Saved 2 transactions" in r["replies"][0]["text"]
    assert client.post("/api/v1/say", json={"text": "  "}, headers=H).status_code == 400


def test_manifest(client):
    r = client.get("/app/static/manifest.webmanifest")
    assert r.headers["content-type"].startswith("application/manifest+json")
    assert client.get("/app/static/../../config.py").status_code == 404


def test_plans(client):
    facets = client.get("/api/v1/facets", headers=H).json()
    cafes = next(c for c in facets["categories"] if c["name"] == "Cafes")
    before = client.get("/api/v1/plans", headers=H).json()
    assert (
        client.post(
            "/api/v1/budgets", json={"category_id": cafes["id"], "amount": 95}, headers=H
        ).status_code
        == 200
    )
    client.post("/api/v1/say", json={"text": "every 3rd 7.77 newspaper"}, headers=H)
    client.post("/api/v1/say", json={"text": "lent 20 to kristjan"}, headers=H)
    p = client.get("/api/v1/plans", headers=H).json()
    cafe_budget = next(b for b in p["budgets"] if b["category"] == "Cafes")
    assert cafe_budget["limit_minor"] == 9500
    rule = next(r for r in p["recurring"] if r["amount_minor"] == 777)
    assert p["monthly_fixed_minor"] == before["monthly_fixed_minor"] + 777
    assert {"person": "kristjan", "owed_to_me_minor": 2000} in p["lending"]
    assert client.delete(f"/api/v1/recurring/{rule['id']}", headers=H).json() == {"stopped": True}
    assert client.delete(f"/api/v1/budgets/{cafe_budget['id']}", headers=H).json() == {
        "deleted": True
    }
    p = client.get("/api/v1/plans", headers=H).json()
    assert all(r["amount_minor"] != 777 for r in p["recurring"])
    assert all(b["category"] != "Cafes" for b in p["budgets"])


def test_category_admin(client):
    cats = {c["name"]: c for c in client.get("/api/v1/categories", headers=H).json()}
    assert cats["Groceries"]["count"] > 0
    r = client.patch(
        f"/api/v1/categories/{cats['Cafes']['id']}", json={"name": "Coffee"}, headers=H
    )
    assert r.json()["name"] == "Coffee"
    assert (
        client.patch(
            f"/api/v1/categories/{cats['Cafes']['id']}", json={"name": "Groceries"}, headers=H
        ).status_code
        == 409
    )
    moved = client.patch(
        f"/api/v1/categories/{cats['Cafes']['id']}",
        json={"merge_into": cats["Restaurants"]["id"]},
        headers=H,
    ).json()["moved"]
    assert moved > 0
    names = {c["name"] for c in client.get("/api/v1/categories", headers=H).json()}
    assert "Coffee" not in names
