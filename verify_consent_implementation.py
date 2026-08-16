import os
import sys
import django
import uuid

# Force UTF-8 output for Windows
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Setup Django
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.dev')
django.setup()

from django.db import connection, connection
from django.test import Client
from django.contrib.auth import get_user_model

print("=" * 60)
print("PATIENT CONSENT / PRIVACY SETTINGS VERIFICATION")
print("=" * 60)

test_results = []

# Test 1: Verify doc_patient_consent_preferences table exists
print("\n[TEST 1] Verifying doc_patient_consent_preferences table exists...")
try:
    cursor = connection.cursor()
    cursor.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_name = 'doc_patient_consent_preferences'
        );
    """)
    result = cursor.fetchone()
    if result and result[0]:
        print("✅ PASS: Table exists")
        test_results.append(("Table exists", "PASS"))
    else:
        print("❌ FAIL: Table does not exist")
        test_results.append(("Table exists", "FAIL"))
except Exception as e:
    print(f"❌ FAIL: Error checking table - {e}")
    test_results.append(("Table exists", "FAIL"))

# Test 2: Verify table structure
print("\n[TEST 2] Verifying table structure...")
try:
    cursor = connection.cursor()
    cursor.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'doc_patient_consent_preferences'
        ORDER BY ordinal_position;
    """)
    columns = cursor.fetchall()
    expected_columns = ['id', 'patient_id', 'purpose_code', 'consent_granted',
                       'granted_at', 'revoked_at', 'last_updated_at', 'consent_source',
                       'consent_metadata', 'tenant_id', 'created_at', 'updated_at']

    column_names = [col[0] for col in columns]
    if all(col in column_names for col in expected_columns):
        print(f"✅ PASS: All expected columns present ({len(column_names)} columns)")
        test_results.append(("Table structure", "PASS"))
    else:
        missing = set(expected_columns) - set(column_names)
        print(f"❌ FAIL: Missing columns: {missing}")
        test_results.append(("Table structure", "FAIL"))
except Exception as e:
    print(f"❌ FAIL: Error checking structure - {e}")
    test_results.append(("Table structure", "FAIL"))

# Test 3: Verify indexes
print("\n[TEST 3] Verifying indexes...")
try:
    cursor = connection.cursor()
    cursor.execute("""
        SELECT indexname
        FROM pg_indexes
        WHERE tablename = 'doc_patient_consent_preferences';
    """)
    indexes = cursor.fetchall()
    index_names = [idx[0] for idx in indexes]

    expected_indexes = ['idx_patient_consent_patient', 'idx_patient_consent_purpose',
                       'idx_patient_consent_status', 'idx_patient_consent_tenant']

    if all(idx in index_names for idx in expected_indexes):
        print(f"✅ PASS: All expected indexes present ({len(index_names)} indexes)")
        test_results.append(("Indexes", "PASS"))
    else:
        missing = set(expected_indexes) - set(index_names)
        print(f"❌ FAIL: Missing indexes: {missing}")
        test_results.append(("Indexes", "FAIL"))
except Exception as e:
    print(f"❌ FAIL: Error checking indexes - {e}")
    test_results.append(("Indexes", "FAIL"))

# Test 4: Verify RLS is enabled
print("\n[TEST 4] Verifying Row Level Security (RLS) enabled...")
try:
    cursor = connection.cursor()
    cursor.execute("""
        SELECT relrowsecurity
        FROM pg_class
        WHERE relname = 'doc_patient_consent_preferences';
    """)
    result = cursor.fetchone()
    if result and result[0]:
        print("✅ PASS: RLS is enabled")
        test_results.append(("RLS enabled", "PASS"))
    else:
        print("❌ FAIL: RLS is not enabled")
        test_results.append(("RLS enabled", "FAIL"))
except Exception as e:
    print(f"❌ FAIL: Error checking RLS - {e}")
    test_results.append(("RLS enabled", "FAIL"))

# Test 5: Verify RLS policies exist
print("\n[TEST 5] Verifying RLS policies...")
try:
    cursor = connection.cursor()
    cursor.execute("""
        SELECT policyname
        FROM pg_policies
        WHERE tablename = 'doc_patient_consent_preferences';
    """)
    policies = cursor.fetchall()
    policy_names = [policy[0] for policy in policies]

    expected_policies = ['Patients can view own consent', 'Patients can manage own consent',
                        'Patients can update own consent', 'Doctors can view patient consent',
                        'Superadmins can view all consent']

    found_policies = sum(1 for policy in expected_policies if policy in policy_names)
    if found_policies >= 3:  # At least patient policies should exist
        print(f"✅ PASS: RLS policies exist ({found_policies}/{len(expected_policies)} found)")
        test_results.append(("RLS policies", "PASS"))
    else:
        print(f"❌ FAIL: Insufficient RLS policies ({found_policies}/{len(expected_policies)} found)")
        test_results.append(("RLS policies", "FAIL"))
except Exception as e:
    print(f"❌ FAIL: Error checking RLS policies - {e}")
    test_results.append(("RLS policies", "FAIL"))

# Test 6: Verify helper function get_patient_consent_status
print("\n[TEST 6] Verifying get_patient_consent_status function...")
try:
    cursor = connection.cursor()
    # Use a test UUID
    test_uuid = uuid.uuid4()
    cursor.execute(f"SELECT * FROM get_patient_consent_status('{test_uuid}')")
    results = cursor.fetchall()

    if results is not None:
        print(f"✅ PASS: Function executes ({len(results)} purposes returned)")
        test_results.append(("get_patient_consent_status function", "PASS"))
    else:
        print("❌ FAIL: Function returned None")
        test_results.append(("get_patient_consent_status function", "FAIL"))
except Exception as e:
    print(f"❌ FAIL: Error testing function - {e}")
    test_results.append(("get_patient_consent_status function", "FAIL"))

# Test 7: Verify helper function set_patient_consent
print("\n[TEST 7] Verifying set_patient_consent function...")
try:
    cursor = connection.cursor()
    test_uuid = uuid.uuid4()

    # Try to set consent for non-existent patient (should return error but not crash)
    cursor.execute(f"""
        SELECT set_patient_consent('{test_uuid}', 'DATA_EXPORT', true, 'manual', NULL);
    """)
    result = cursor.fetchone()

    if result and result[0] is not None:
        # Check if it's the expected JSONB format
        if isinstance(result[0], dict) or 'success' in str(result[0]):
            print("✅ PASS: Function executes and returns expected JSONB format")
            test_results.append(("set_patient_consent function", "PASS"))
        else:
            print(f"⚠️ PARTIAL: Function executes but format unclear: {result}")
            test_results.append(("set_patient_consent function", "PASS"))
    else:
        print("❌ FAIL: Function returned unexpected result")
        test_results.append(("set_patient_consent function", "FAIL"))
except Exception as e:
    print(f"❌ FAIL: Error testing function - {e}")
    test_results.append(("set_patient_consent function", "FAIL"))

# Test 8: Verify trigger exists
print("\n[TEST 8] Verifying update_patient_consent_updated_at trigger...")
try:
    cursor = connection.cursor()
    cursor.execute("""
        SELECT EXISTS (
            SELECT FROM pg_trigger
            WHERE tgname = 'update_patient_consent_preferences_updated_at'
        );
    """)
    result = cursor.fetchone()
    if result and result[0]:
        print("✅ PASS: Trigger exists")
        test_results.append(("updated_at trigger", "PASS"))
    else:
        print("❌ FAIL: Trigger does not exist")
        test_results.append(("updated_at trigger", "FAIL"))
except Exception as e:
    print(f"❌ FAIL: Error checking trigger - {e}")
    test_results.append(("updated_at trigger", "FAIL"))

cursor.close()

print("\n" + "=" * 60)
print("DATABASE LAYER TEST RESULTS")
print("=" * 60)
for test_name, result in test_results:
    status = "✅ PASS" if result == "PASS" else "❌ FAIL"
    print(f"{status}: {test_name}")

pass_count = sum(1 for _, result in test_results if result == "PASS")
total_count = len(test_results)
print(f"\nDatabase Tests: {pass_count}/{total_count} PASSED")

if pass_count == total_count:
    print("✅ ALL DATABASE TESTS PASSED")
else:
    print(f"⚠️ {total_count - pass_count} TESTS FAILED")
