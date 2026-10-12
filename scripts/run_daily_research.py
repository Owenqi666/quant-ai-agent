"""Versioned daily CLI; hosted descriptors only, no arbitrary Python."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from paper_alpha.daily_workflow_v2 import main
if __name__=='__main__':sys.exit(main())
