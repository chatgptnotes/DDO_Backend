import os
import sys
import django

# Setup Django
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.dev')
django.setup()

from django.db import connection

# Read migration file
with open('../AiSurgeonPilot/supabase/migrations/20260816_create_patient_consent_preferences.sql', 'r', encoding='utf-8') as f:
    migration_sql = f.read()

# Execute migration using Django connection
cursor = connection.cursor()
try:
    cursor.execute(migration_sql)
    connection.commit()
    print('Migration applied successfully')
except Exception as e:
    print(f'Migration error: {e}')
    connection.rollback()
finally:
    cursor.close()
