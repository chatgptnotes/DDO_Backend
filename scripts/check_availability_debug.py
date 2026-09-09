"""Inspect the REMOTE supabase storage bucket + policies for patient-documents."""
import psycopg

REMOTE = "postgresql://postgres.uakqdjxuceckjssjdyui:Digidocoffice@aws-1-ap-northeast-2.pooler.supabase.com:5432/postgres"

conn = psycopg.connect(REMOTE, connect_timeout=15)
cur = conn.cursor()
cur.execute("SELECT id, name, public, file_size_limit FROM storage.buckets")
print("buckets:")
for r in cur.fetchall():
    print("  ", r)

cur.execute(
    """SELECT policyname, cmd, COALESCE(qual,''), COALESCE(with_check,'')
         FROM pg_policies
        WHERE schemaname = 'storage' AND tablename = 'objects'
        ORDER BY policyname"""
)
print("\nstorage.objects policies:")
for name, cmd, qual, with_check in cur.fetchall():
    print(f"  {name} [{cmd}]")
    if qual:
        print(f"     USING: {qual[:200]}")
    if with_check:
        print(f"     CHECK: {with_check[:200]}")
conn.close()
