"""
Upload course data from CSV to Supabase with proper column mapping
"""
import pandas as pd
from supabase import create_client
from dotenv import load_dotenv
import os
import sys
import re

# Load environment variables
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ ERROR: Missing Supabase credentials in .env file!")
    sys.exit(1)

print(f"✅ Supabase URL: {SUPABASE_URL}")
print(f"✅ API Key found (length: {len(SUPABASE_KEY)} chars)")

# Initialize Supabase
try:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("✅ Supabase client created successfully\n")
except Exception as e:
    print(f"❌ Failed to create Supabase client: {e}")
    sys.exit(1)

# Read CSV
csv_file = "ClassAverageCrowdSourcing.csv"

if not os.path.exists(csv_file):
    print(f"❌ ERROR: CSV file not found: {csv_file}")
    sys.exit(1)

print(f"📖 Reading CSV file: {csv_file}")
df = pd.read_csv(csv_file)
print(f"✅ Loaded {len(df)} courses from CSV\n")

print("Original CSV columns:")
print(df.columns.tolist())
print("\nFirst few rows:")
print(df.head())
print()

# Parse the CSV data and transform it to match database schema
print("🔄 Transforming data to match database schema...")

def parse_course_code(class_code):
    """
    Parse 'ACCT351-201601' into subject='ACCT', catalog='351', 
    section='201601' or term info
    """
    if pd.isna(class_code):
        return None, None, None
    
    # Split on hyphen: ACCT351-201601
    parts = str(class_code).split('-')
    if len(parts) < 2:
        return None, None, None
    
    course_part = parts[0]  # ACCT351
    term_part = parts[1] if len(parts) > 1 else None  # 201601
    
    # Extract subject (letters) and catalog (numbers)
    match = re.match(r'^([A-Z]+)(\d+)$', course_part)
    if match:
        subject = match.group(1)  # ACCT
        catalog = match.group(2)  # 351
        return subject, catalog, term_part
    
    return None, None, term_part

def grade_letter_to_number(grade_letter):
    """Convert letter grade to GPA number"""
    grade_map = {
        'A+': 4.0, 'A': 4.0, 'A-': 3.7,
        'B+': 3.3, 'B': 3.0, 'B-': 2.7,
        'C+': 2.3, 'C': 2.0, 'C-': 1.7,
        'D+': 1.3, 'D': 1.0, 'F': 0.0
    }
    if pd.isna(grade_letter):
        return None
    return grade_map.get(str(grade_letter).strip(), None)

# Create new dataframe with proper schema
transformed_records = []

for idx, row in df.iterrows():
    # Parse course code
    subject, catalog, term_code = parse_course_code(row.get('Class'))
    
    if not subject or not catalog:
        continue  # Skip invalid rows
    
    # Get term name
    term_name = row.get('Term Name', '')
    
    # Get course title (from 'Course' column which seems to be the code)
    # We'll use the course code as a placeholder title
    title = f"{subject} {catalog}"
    
    # Get average grade
    avg_grade_letter = row.get('Class Ave')
    avg_grade_number = grade_letter_to_number(avg_grade_letter)
    
    # Create record matching database schema
    record = {
        'subject': subject,
        'catalog': catalog,
        'title': title,
        'section': term_code,  # Using term code as section
        'term': term_name,
        'instructor': None,  # Not in CSV
        'average': avg_grade_number,
        'std_dev': None,  # Not in CSV
        'median': None,  # Not in CSV
        'num_students': None,  # Not in CSV
        'a_plus': 0,
        'a': 0,
        'a_minus': 0,
        'b_plus': 0,
        'b': 0,
        'b_minus': 0,
        'c_plus': 0,
        'c': 0,
        'd': 0,
        'f': 0
    }
    
    transformed_records.append(record)

print(f"✅ Transformed {len(transformed_records)} valid records\n")

if len(transformed_records) == 0:
    print("❌ No valid records to upload!")
    sys.exit(1)

# Show sample of transformed data
print("Sample of transformed data:")
print(pd.DataFrame(transformed_records[:5]))
print()

# Upload in batches
batch_size = 100
total_inserted = 0
failed_batches = []

print(f"📤 Uploading in batches of {batch_size}...\n")

for i in range(0, len(transformed_records), batch_size):
    batch = transformed_records[i:i+batch_size]
    batch_num = (i // batch_size) + 1
    total_batches = (len(transformed_records) + batch_size - 1) // batch_size
    
    try:
        response = supabase.table('courses').insert(batch).execute()
        total_inserted += len(batch)
        print(f"✅ Batch {batch_num}/{total_batches}: Inserted {len(batch)} courses (Total: {total_inserted})")
    except Exception as e:
        error_msg = str(e)
        failed_batches.append((batch_num, error_msg))
        print(f"❌ Batch {batch_num}/{total_batches} failed: {error_msg[:200]}")

print(f"\n{'='*60}")
print(f"📊 Upload Summary:")
print(f"{'='*60}")
print(f"Total records processed: {len(transformed_records)}")
print(f"Successfully inserted: {total_inserted}")
print(f"Failed batches: {len(failed_batches)}")

if failed_batches:
    print(f"\n⚠️  Some batches failed:")
    for batch_num, error in failed_batches[:5]:  # Show first 5
        print(f"   Batch {batch_num}: {error[:150]}...")
else:
    print("\n🎉 All courses uploaded successfully!")

# Verify
try:
    count_response = supabase.table('courses').select('id', count='exact').execute()
    total_count = count_response.count if hasattr(count_response, 'count') else len(count_response.data)
    print(f"\n✅ Total courses in database: {total_count}")
    
    # Show some examples
    sample = supabase.table('courses').select('*').limit(5).execute()
    print("\n📋 Sample of uploaded courses:")
    for course in sample.data:
        print(f"   {course['subject']} {course['catalog']}: {course['title']} (Avg: {course['average']})")
        
except Exception as e:
    print(f"\n⚠️  Could not verify: {e}")

print("\n✅ Upload script completed!")