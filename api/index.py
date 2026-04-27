import sys
import os

# Add the parent directory (race/) to the Python path
# so that the imports in main.py work correctly when Vercel runs this from api/
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app
