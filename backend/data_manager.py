# data_manager.py (direct Google Sheets access)
import pandas as pd
import requests
from io import StringIO

class McGillDataManager:
    def __init__(self):
        self.courses = {}
        print("McGill Data Manager initialized!")
    
    def load_from_share_url(self, share_url):
        """Load from Google Sheets share URL"""
        try:
            # Extract the sheet ID from the URL
            if '/d/' in share_url:
                sheet_id = share_url.split('/d/')[1].split('/')[0]
                csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
                
                print(f"Trying to load from: {csv_url}")
                
                # Download the CSV content
                response = requests.get(csv_url)
                response.raise_for_status()
                
                # Read into pandas
                df = pd.read_csv(StringIO(response.text))
                
                print(f"Loaded {len(df)} rows of data")
                print("Columns found:", df.columns.tolist())
                print("\nFirst 3 rows:")
                print(df.head(3))
                
                return df
                
        except Exception as e:
            print(f"Error: {e}")
            return None

# Test it
if __name__ == "__main__":
    dm = McGillDataManager()
    
    # Paste your Google Sheets share URL here
    share_url = "https://docs.google.com/spreadsheets/d/1Bi2FyamynyIFN_7ozqCZYo-9NGVKbO1xTgJerIC8vqU/edit?usp=sharing"
    
    df = dm.load_from_share_url(share_url)













































































# data_manager.py (debug version)
import pandas as pd
import requests
from io import StringIO

class McGillDataManager:
    def __init__(self):
        self.courses = {}
        print("McGill Data Manager initialized!")
    
    def load_and_examine_data(self, share_url):
        """Load data and examine it carefully"""
        try:
            if '/d/' in share_url:
                sheet_id = share_url.split('/d/')[1].split('/')[0]
                csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
                
                print(f"Loading from Google Sheets...")
                response = requests.get(csv_url)
                response.raise_for_status()
                
                df = pd.read_csv(StringIO(response.text))
                
                print(f"Total rows: {len(df)}")
                print(f"Columns: {df.columns.tolist()}")
                
                # Let's examine different parts of the data
                print("\n=== EXAMINING FIRST 20 ROWS ===")
                print(df.head(20).to_string())
                
                print("\n=== EXAMINING ROWS 50-70 ===")
                print(df.iloc[50:70].to_string())
                
                print("\n=== LOOKING FOR NON-NULL COURSE DATA ===")
                # Find rows where Course column is not null
                course_not_null = df[df['Course'].notna()]
                print(f"Rows with Course data: {len(course_not_null)}")
                if len(course_not_null) > 0:
                    print("Sample rows with Course data:")
                    print(course_not_null.head(10).to_string())
                
                print("\n=== CHECKING FOR DIFFERENT PATTERNS ===")
                # Check what types of values we have in Course column
                unique_courses = df['Course'].value_counts().head(20)
                print("Most common Course values:")
                print(unique_courses)
                
                return df
                
        except Exception as e:
            print(f"Error: {e}")
            return None

# Test it
if __name__ == "__main__":
    dm = McGillDataManager()
    
    share_url = "https://docs.google.com/spreadsheets/d/1Bi2FyamynyIFN_7ozqCZYo-9NGVKbO1xTgJerIC8vqU/edit"
    
    df = dm.load_and_examine_data(share_url)



















































































