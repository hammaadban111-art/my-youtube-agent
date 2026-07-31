import sys
from pathlib import Path

# Add the repo root directory to sys.path so 'from agent import ...' works cleanly
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))
