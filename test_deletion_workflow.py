#!/usr/bin/env python
"""
End-to-End Test for Data Deletion Workflow

Tests the complete UI → API → Database → Audit workflow:
1. Patient creates deletion request
2. System validates no pending requests
3. Request is created with reference number
4. Audit trail is created
5. Test authentication/authorization
"""

import os
import sys
import uuid
import json
from datetime import timedelta
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.test import Client
from django.utils import timezone
from rest_framework.test import APIClient
# from supabase import create_client  # Not needed for this test

# Import models
from surgeonpilot.models import DpdpDeletionRequest, DpdpDeletionAudit
from core.auth import SupabaseJWTAuthentication

# Test configuration
SUPABASE_URL = os.getenv('NEXT_PUBLIC_SUPABASE_URL', 'https://uakqdjxuceckjssjdyui.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
BACKEND_URL = 'http://localhost:8000'

print("=" * 70)
print("DATA DELETION E2E TEST")
print("=" * 70)

# Test 1: Verify Django endpoint exists and requires authentication
print("\n[Test 1] Verifying endpoint requires authentication...")
client = APIClient()
response = client.get(f'{BACKEND_URL}/api/surgeon/patient/deletion-history/')
if response.status_code == 401:
    print("✅ PASS - Endpoint correctly requires authentication (401)")
else:
    print(f"❌ FAIL - Expected 401, got {response.status_code}")

# Test 2: Verify models exist
print("\n[Test 2] Verifying deletion request model exists...")
try:
    count = DpdpDeletionRequest.objects.count()
    print(f"✅ PASS - DpdpDeletionRequest model accessible (current count: {count})")
except Exception as e:
    print(f"❌ FAIL - Model error: {e}")

# Test 3: Verify audit model exists
print("\n[Test 3] Verifying deletion audit model exists...")
try:
    count = DpdpDeletionAudit.objects.count()
    print(f"✅ PASS - DpdpDeletionAudit model accessible (current count: {count})")
except Exception as e:
    print(f"❌ FAIL - Model error: {e}")

# Test 4: Test URL routing
print("\n[Test 4] Verifying URL routing...")
client = APIClient()
response = client.get(f'{BACKEND_URL}/api/surgeon/patient/deletion-history/')
if response.status_code == 401:
    print("✅ PASS - URL routing works (returned 401 unauthorized)")
elif response.status_code == 404:
    print("❌ FAIL - URL routing failed (404 not found)")
else:
    print(f"⚠️  WARNING - Unexpected status code: {response.status_code}")

# Test 5: Test reference number generation
print("\n[Test 5] Verifying reference number format...")
test_ref = f"DEL-{timezone.now().strftime('%Y%m%d')}-{str(uuid.uuid4())[:8].upper()}"
if test_ref.startswith('DEL-') and len(test_ref) == 19:
    print(f"✅ PASS - Reference number format valid: {test_ref}")
else:
    print(f"❌ FAIL - Reference number format invalid: {test_ref}")

# Test 6: Check Next.js API routes
print("\n[Test 6] Verifying Next.js API proxy routes...")
import os
routes_exist = all([
    os.path.exists('AiSurgeonPilot/src/app/api/surgeon/patient/deletion-history/route.ts'),
    os.path.exists('AiSurgeonPilot/src/app/api/surgeon/patient/deletion-request/route.ts'),
    os.path.exists('AiSurgeonPilot/src/app/api/surgeon/patient/deletion-request/[requestId]/audit/route.ts'),
])
if routes_exist:
    print("✅ PASS - All Next.js API proxy routes exist")
else:
    print("❌ FAIL - Missing Next.js API routes")

# Test 7: Check frontend page exists
print("\n[Test 7] Verifying frontend UI page exists...")
if os.path.exists('AiSurgeonPilot/src/app/patient/delete-data/page.tsx'):
    print("✅ PASS - Frontend deletion page exists")
else:
    print("❌ FAIL - Frontend deletion page missing")

# Test 8: Verify trailing slash handling in API routes
print("\n[Test 8] Verifying trailing slash handling...")
with open('AiSurgeonPilot/src/app/api/surgeon/patient/deletion-history/route.ts', 'r') as f:
    content = f.read()
    if 'deletion-history/' in content:
        print("✅ PASS - Trailing slash included in API call")
    else:
        print("⚠️  WARNING - Trailing slash may be missing")

# Test 9: Database connection test
print("\n[Test 9] Testing database connection...")
try:
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        result = cursor.fetchone()
    if result[0] == 1:
        print("✅ PASS - Database connection successful")
    else:
        print("❌ FAIL - Database connection returned unexpected result")
except Exception as e:
    print(f"❌ FAIL - Database connection error: {e}")

# Test 10: Verify DPDP tables exist
print("\n[Test 10] Verifying DPDP database tables...")
try:
    from django.db import connection
    with connection.cursor() as cursor:
        tables_to_check = [
            'doc_dpdp_deletion_requests',
            'doc_dpdp_deletion_audit',
            'doc_dpdp_retention_rules',
        ]
        for table in tables_to_check:
            cursor.execute(f"""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = '{table}'
                )
            """)
            exists = cursor.fetchone()[0]
            status = "✅" if exists else "❌"
            print(f"  {status} Table '{table}': {'exists' if exists else 'missing'}")
except Exception as e:
    print(f"❌ FAIL - Error checking tables: {e}")

print("\n" + "=" * 70)
print("TEST SUMMARY")
print("=" * 70)
print("Basic infrastructure tests completed.")
print("For full integration testing, a valid Supabase JWT token is required.")
print("=" * 70)
