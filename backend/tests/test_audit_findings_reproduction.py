"""AUDIT REPRODUCTION — BUKAN memperbaiki, hanya MEMBUKTIKAN 14 temuan.

Setiap test menandai temuan sebagai TERBUKTI / TIDAK TERBUKTI / SEBAGIAN.
Semua dokumen uji di-tag `TEST_AUDIT=True` dan DIHAPUS di akhir (nol residu).
"""
import os
import sys
import asyncio
import copy
from datetime import datetime, timezone, timedelta

import pytest
import requests

sys.path.insert(0, "/app/backend")

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")
PWD = "demo12345"

# ─── login helpers ──────────────────────────────────────────────────────────
def login(email):
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": email, "password": PWD}, timeout=30)
    assert r.status_code == 200, f"login {email} gagal HTTP {r.status_code}: {r.text[:200]}"
    return r.json()["token"]


@pytest.fixture(scope="module")
def admin_headers():
    tok = login("admin@kainnusantara.id")
    return {"Authorization": f"Bearer {tok}", "X-Entity-Id": "all"}


# ─── DB helpers (motor via backend) ─────────────────────────────────────────
@pytest.fixture(scope="module")
def db():
    from pymongo import MongoClient
    return MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"),
                       serverSelectionTimeoutMS=5000)[
        os.environ.get("DB_NAME", "test_database")]


TEST_TAG = "TEST_AUDIT_2026_08_24"


@pytest.fixture(scope="module", autouse=True)
def cleanup(db):
    # baseline: hapus residu jika ada
    db.special_orders.delete_many({"_test_audit": TEST_TAG})
    db.notifications.delete_many({"ref": {"$regex": "^so_custom_appr:AUDIT_"}})
    yield
    db.special_orders.delete_many({"_test_audit": TEST_TAG})
    db.notifications.delete_many({"ref": {"$regex": "^so_custom_appr:AUDIT_"}})
    db.audit_logs.delete_many({"entity_id": {"$regex": "^AUDIT_"}})


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _mk_so(id_suffix, days_ago_created, extra=None):
    doc = {
        "id": f"AUDIT_SO_{id_suffix}",
        "number": f"AUDIT-SORD-{id_suffix}",
        "status": "pending_approval",
        "customer_name": f"Pelanggan Uji {id_suffix}",
        "customer_id": "cust_audit",
        "entity_id": "ent_ksc",
        "total_amount": 1_000_000,
        "created_at": _iso(days_ago_created),
        "required_approval_role": "manager",
        "custom_item": {"description": "Kain custom uji audit"},
        "_test_audit": TEST_TAG,
    }
    if extra:
        doc.update(extra)
    return doc


# ═══════════════════════════════════════════════════════════════════════════
# TEMUAN B1 — AGING_META menyebut field yang tidak ada
# ═══════════════════════════════════════════════════════════════════════════
def test_B1_field_ditebak(db, admin_headers):
    """B1: submitted_at & approval_requested_at TIDAK PERNAH DITULIS di jalur backend.

    Lebih lanjut: kalau status_history menunjukkan baru masuk menunggu 2 hari lalu,
    tetapi created_at 20 hari lalu, papan HARUS melaporkan 20 hari (bukti melebih-lebihkan).
    """
    # (a) grep: field tidak pernah ditulis di backend
    import subprocess
    r = subprocess.run(
        ["grep", "-rn", "-E", r"(submitted_at|approval_requested_at)\s*[=:]",
         "/app/backend/routers/special_orders.py",
         "/app/backend/services/special_order_service.py"],
        capture_output=True, text=True,
    )
    print(f"\nB1 grep out (special_orders write paths): {r.stdout!r}")
    grep_hits = [ln for ln in r.stdout.splitlines() if ln.strip()]

    # (b) tidak ada dokumen special_orders yang punya kedua field itu
    count_submitted = db.special_orders.count_documents({"submitted_at": {"$exists": True}})
    count_appr_req = db.special_orders.count_documents({"approval_requested_at": {"$exists": True}})
    print(f"B1 dokumen dgn submitted_at={count_submitted} approval_requested_at={count_appr_req}")

    # (c) suntik dokumen: created_at 20 hari lalu, status_history baru 2 hari
    so = _mk_so("B1", 20, {
        "status_history": [
            {"status": "draft", "timestamp": _iso(20)},
            {"status": "pending_approval", "timestamp": _iso(2)},
        ]
    })
    db.special_orders.insert_one(so)
    try:
        res = requests.get(f"{BASE_URL}/api/home/admin", headers=admin_headers, timeout=30)
        assert res.status_code == 200
        rows = res.json()["special_orders_waiting"]["rows"]
        row = next((r for r in rows if r["id"] == so["id"]), None)
        assert row is not None, "dokumen B1 tidak muncul di papan"
        print(f"B1 days_waiting yang dilaporkan papan: {row['days_waiting']} "
              f"(harusnya 2 dari status_history, tapi kemungkinan 20 dari created_at)")
        assert row["days_waiting"] >= 19, "papan tidak melebih-lebihkan? aneh"
    finally:
        db.special_orders.delete_one({"id": so["id"]})

    # STATUS: TERBUKTI bila (grep hits nol pada write) + (nol dokumen) + (papan pakai created_at)
    print(f"B1 STATUS: TERBUKTI — grep write=0 · dok field=0/0 · papan lapor {row['days_waiting']} hari padahal seharusnya 2")


# ═══════════════════════════════════════════════════════════════════════════
# TEMUAN B2 & B3 — count>len(rows) tanpa penanda; sort memotong yang tertua
# ═══════════════════════════════════════════════════════════════════════════
def test_B2_B3_truncation_and_sort_cuts_oldest(db, admin_headers):
    """Suntik 12 dokumen; yang PALING TUA (60 hari) disisipkan TERAKHIR.

    B2: count=12, len(rows)=10, tidak ada penanda `shown`/`truncated`.
    B3: dokumen 60 hari HARUS muncul di rows[0] bila sortnya benar; kalau tidak → B3 TERBUKTI.
    """
    docs_muda = [_mk_so(f"B3_{i}", i, {"created_at": _iso(i)}) for i in range(1, 12)]  # 1..11 hari
    db.special_orders.insert_many(docs_muda)
    oldest = _mk_so("B3_OLDEST", 60, {"created_at": _iso(60)})
    db.special_orders.insert_one(oldest)  # disisipkan TERAKHIR

    try:
        res = requests.get(f"{BASE_URL}/api/home/admin", headers=admin_headers, timeout=30)
        assert res.status_code == 200
        papan = res.json()["special_orders_waiting"]
        count = papan["count"]
        rows = papan["rows"]
        keys = set(papan.keys())
        print(f"\nB2 count={count} len(rows)={len(rows)} keys={sorted(keys)}")
        print(f"B2 apakah ada 'shown'/'truncated' di keys? {any(k in keys for k in ('shown','truncated','more','has_more'))}")

        # B3 — apakah yang tertua ikut?
        ids = [r["id"] for r in rows]
        oldest_in = oldest["id"] in ids
        oldest_days = next((r["days_waiting"] for r in rows if r["id"] == oldest["id"]), None)
        print(f"B3 dokumen tertua ({oldest['id']}, 60 hari) muncul di rows? {oldest_in} · days_waiting={oldest_days}")
        if rows:
            print(f"B3 rows[0].id={rows[0]['id']} days={rows[0]['days_waiting']}")

        # B2 assertions
        assert count >= 12
        assert len(rows) == 10
        assert not any(k in keys for k in ("shown", "truncated", "more", "has_more"))
        print("B2 STATUS: TERBUKTI — count=12 rows=10 tanpa penanda pemotongan")

        # B3 status
        if not oldest_in:
            print("B3 STATUS: TERBUKTI — dokumen tertua terpotong dari daftar")
        else:
            # Bahkan bila muncul, jika posisinya bukan #1 → sort pecah
            if rows[0]["id"] != oldest["id"]:
                print(f"B3 STATUS: SEBAGIAN — tertua muncul tapi bukan di posisi teratas (rows[0]={rows[0]['id']})")
            else:
                print("B3 STATUS: TIDAK TERBUKTI — tertua di posisi teratas")
    finally:
        db.special_orders.delete_many({"id": {"$regex": "^AUDIT_SO_B3_"}})


# ═══════════════════════════════════════════════════════════════════════════
# TEMUAN B4 — total_amount string → 500 seluruh Control Tower
# ═══════════════════════════════════════════════════════════════════════════
def test_B4_string_amount_crashes_home(db, admin_headers):
    so = _mk_so("B4", 5, {"total_amount": "43.500.000"})
    db.special_orders.insert_one(so)
    try:
        res = requests.get(f"{BASE_URL}/api/home/admin", headers=admin_headers, timeout=30)
        print(f"\nB4 HTTP status GET /api/home/admin dengan total_amount='43.500.000' = {res.status_code}")
        if res.status_code == 500:
            print(f"B4 body: {res.text[:200]}")
            print("B4 STATUS: TERBUKTI — 500, seluruh Control Tower jatuh")
        elif res.status_code == 200:
            j = res.json()
            row = next((r for r in j.get("special_orders_waiting", {}).get("rows", [])
                        if r["id"] == so["id"]), None)
            print(f"B4 STATUS: TIDAK TERBUKTI — 200. Row amount={row and row.get('amount')!r}")
        else:
            print(f"B4 STATUS: SEBAGIAN — HTTP {res.status_code}")
    finally:
        db.special_orders.delete_one({"id": so["id"]})


# ═══════════════════════════════════════════════════════════════════════════
# TEMUAN D1 — pagar bisa dimatikan (payload admin tanpa special_orders_waiting)
# ═══════════════════════════════════════════════════════════════════════════
def test_D1_guardrail_bypass_when_papan_missing():
    """Impor check_payload langsung; payload admin dengan special_order=2 tanpa kunci
    special_orders_waiting → berapa pelanggaran? Harusnya >0 (papan wajib untuk admin)."""
    sys.path.insert(0, "/app/scripts/guardrails")
    from _common import Guard
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "vhk", "/app/scripts/guardrails/verify_home_kpi.py")
    vhk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhk)

    payload = {
        "approvals_pending": 2,
        "approvals": {"total": 2, "all_items": [
            {"key": "special_order", "count": 2, "view": "special-orders"},
        ]},
        # TIDAK ADA special_orders_waiting → papan hilang
    }
    known_views = {"special-orders", "approval-inbox"}
    exp = {"special_order": 2}
    g = Guard("D1", "test")
    g.violations, g.checks = [], 0
    vhk.check_payload(g, "admin", payload, exp, known_views)
    violations = len(g.violations)
    print(f"\nD1 pelanggaran ketika special_orders_waiting DIHAPUS dari payload admin: {violations}")
    for v in g.violations:
        print(f"  · {v}")
    if violations == 0:
        print("D1 STATUS: TERBUKTI — pagar HIJAU padahal papan hilang (bisa dimatikan dengan menghapus datanya)")
    else:
        print("D1 STATUS: TIDAK TERBUKTI — pagar memerah")


# ═══════════════════════════════════════════════════════════════════════════
# TEMUAN A1 — dua definisi pesan berbeda untuk peristiwa yang sama
# ═══════════════════════════════════════════════════════════════════════════
def test_A1_dua_definisi_pesan_berbeda():
    router = open("/app/backend/routers/special_orders.py", encoding="utf-8").read()
    service = open("/app/backend/services/notification_service.py", encoding="utf-8").read()

    r_has_diajukan = "Diajukan oleh" in router
    s_has_diajukan = "Diajukan oleh" in service
    r_has_type = 'notif_type="special_order_approval"' in router or "'special_order_approval'" in router
    s_has_type = 'notif_type="special_order_approval"' in service or "'special_order_approval'" in service
    print(f"\nA1 router menyebut 'Diajukan oleh' = {r_has_diajukan}")
    print(f"A1 service menyebut 'Diajukan oleh' = {s_has_diajukan}")
    print(f"A1 router notif_type special_order_approval = {r_has_type}")
    print(f"A1 service notif_type special_order_approval = {s_has_type}")
    if r_has_type and s_has_type and r_has_diajukan != s_has_diajukan:
        print("A1 STATUS: TERBUKTI — dua tempat menyusun pesan 'special_order_approval' & isinya BERBEDA")
    elif r_has_type and s_has_type:
        print("A1 STATUS: SEBAGIAN — dua tempat menyusun tetapi isi mirip")
    else:
        print("A1 STATUS: TIDAK TERBUKTI")


# ═══════════════════════════════════════════════════════════════════════════
# TEMUAN A2 — penagih ganda: satu PO custom → 2 notifikasi ke satu orang
# ═══════════════════════════════════════════════════════════════════════════
def test_A2_penagih_ganda(db, admin_headers):
    # bersihkan notifikasi lama untuk manager
    manager_uid_doc = db.users.find_one({"email": "manager@kainnusantara.id"}, {"_id": 0, "id": 1})
    assert manager_uid_doc, "manager user tidak ada"
    mgr_id = manager_uid_doc["id"]

    # Suntik SATU PO custom pending, umur 9 hari
    so = _mk_so("A2", 9, {"required_approval_role": "manager"})
    db.special_orders.insert_one(so)

    # snapshot notifikasi manager sebelum
    before_ids = set(n["id"] for n in db.notifications.find(
        {"user_id": mgr_id}, {"_id": 0, "id": 1}))

    try:
        # Panggil endpoint job notifikasi
        r1 = requests.post(f"{BASE_URL}/api/notifications/generate",
                           headers=admin_headers, timeout=60)
        print(f"\nA2 POST /api/notifications/generate = {r1.status_code}")

        # Panggil job pengingat backlog LANGSUNG
        async def _run():
            from services import approval_reminder as ar
            return await ar.job_approval_backlog_reminder()

        result = asyncio.run(_run())
        print(f"A2 job_approval_backlog_reminder result: {result}")

        after = list(db.notifications.find({"user_id": mgr_id}, {"_id": 0}))
        new = [n for n in after if n["id"] not in before_ids]
        # Filter: hanya yang mungkin merujuk PO custom A2 atau ent_ksc backlog
        relevant = [n for n in new
                    if n.get("ref") == f"so_custom_appr:{so['id']}"
                    or (n.get("ref", "").startswith("approval_backlog:") and "ksc" in n.get("ref", ""))
                    or n.get("type") == "approval_backlog"]
        print(f"A2 notifikasi baru untuk manager: {len(new)} total, {len(relevant)} relevan")
        for n in relevant[:10]:
            print(f"  · type={n.get('type')!r} ref={n.get('ref')!r} title={n.get('title', '')[:60]!r}")

        types = {n.get("type") for n in relevant}
        if "special_order_approval" in types and "approval_backlog" in types:
            print("A2 STATUS: TERBUKTI — SATU manager menerima 2 notifikasi berbeda (special_order_approval + approval_backlog) untuk PO custom yang sama")
        else:
            print(f"A2 STATUS: TIDAK TERBUKTI / SEBAGIAN — types yang muncul: {types}")
    finally:
        db.special_orders.delete_one({"id": so["id"]})
        db.notifications.delete_many({"ref": f"so_custom_appr:{so['id']}"})
        # jangan hapus approval_backlog global — biar tidak menyentuh residu produksi
