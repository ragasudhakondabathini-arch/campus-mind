import os
import sys

# Ensure attendance_microservice root is in sys.path whenever imported as a package
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

