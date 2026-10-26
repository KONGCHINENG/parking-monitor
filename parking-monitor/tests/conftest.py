import os
import sys

# make "import parking" work when running pytest from the project folder
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
